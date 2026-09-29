"""Versioned public environment history, separate from private scenario controls."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, model_validator

from tau_agent.types import JSONValue
from tau_incident.models import Model, Scope, WaitCondition
from tau_incident.telemetry import (
    CatalogSnapshot,
    FixtureProvider,
    QueryResult,
    ReplayData,
    SignalKind,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRow,
)


class EnvironmentChange(Model):
    change_id: str
    kind: Literal["deployment", "configuration"]
    environment: str
    entity: str
    data_at: AwareDatetime
    available_at: AwareDatetime
    before_revision: str
    after_revision: str
    before: dict[str, JSONValue]
    after: dict[str, JSONValue]
    applied_receipt: str

    @model_validator(mode="after")
    def real_change(self) -> EnvironmentChange:
        if not self.applied_receipt or self.before == self.after:
            raise ValueError("a deployment/configuration record requires an applied change receipt")
        if self.before_revision == self.after_revision:
            raise ValueError("change revisions must differ")
        return self


class EnvironmentHistory(Model):
    schema_version: Literal[1] = 1
    catalog: tuple[CatalogSnapshot, ...] = ()
    changes: tuple[EnvironmentChange, ...] = ()


class VersionedCatalog:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        self.path, self.clock = path, clock

    def history(self) -> EnvironmentHistory:
        return EnvironmentHistory.model_validate_json(self.path.read_bytes())

    def save(self, history: EnvironmentHistory) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".history-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(history.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def observe(self, result: QueryResult) -> None:
        if result.rows and result.query.kind == "traces":
            snapshot = observed_snapshot(result.rows, result.query.scope, result.collected_at)
            self.append(snapshot=snapshot)

    def append(
        self, *, snapshot: CatalogSnapshot | None = None, change: EnvironmentChange | None = None
    ) -> None:
        lock = self.path.with_suffix(".lock")
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            history = self.history()
            if snapshot is not None:
                history = history.model_copy(update={"catalog": (*history.catalog, snapshot)})
            if change is not None:
                if any(c.change_id == change.change_id for c in history.changes):
                    raise ValueError("change ID already recorded")
                history = history.model_copy(update={"changes": (*history.changes, change)})
            self.save(history)
        finally:
            os.close(descriptor)
            lock.unlink()

    def provider(self) -> FixtureProvider:
        history = self.history()
        source = (
            "environment-history:" + hashlib.sha256(history.model_dump_json().encode()).hexdigest()
        )
        rows = tuple(
            TelemetryRow(
                kind="deployments" if c.kind == "deployment" else "configuration",
                environment=c.environment,
                entity=c.entity,
                data_at=c.data_at,
                available_at=c.available_at,
                body=c.model_dump(mode="json"),
                source=source,
            )
            for c in history.changes
        )
        return FixtureProvider(
            ReplayData(name="environment-history", rows=rows, catalog=history.catalog),
            clock=self.clock,
            source=source,
        )

    async def snapshots(self, scope: Scope) -> tuple[CatalogSnapshot, ...]:
        from tau_incident.submission import scope_contains

        return tuple(
            c
            for c in self.history().catalog
            if c.available_at <= self.clock()
            and (
                c.data_at is None
                or scope.time_window.end is None
                or c.data_at <= scope.time_window.end
            )
            and c.scope.environment == scope.environment
            and (not scope.entities or set(scope.entities) <= set(c.entities))
            and (
                scope_contains(c.scope, scope)
                or (
                    c.data_at is not None
                    and (scope.time_window.start is None or c.data_at >= scope.time_window.start)
                    and (scope.time_window.end is None or c.data_at <= scope.time_window.end)
                )
            )
        )

    async def snapshot(self, scope: Scope) -> CatalogSnapshot | None:
        return await self.provider().snapshot(scope)


class EnvironmentProvider:
    def __init__(self, live: TelemetryProvider, catalog: VersionedCatalog) -> None:
        self.live, self.catalog = live, catalog
        self.source = str(getattr(live, "source", "live:telemetry"))

    def capabilities(self) -> tuple[SignalKind, ...]:
        return (*self.live.capabilities(), "deployments", "configuration")

    def describe(self) -> dict[str, JSONValue]:
        describe = getattr(self.live, "describe", None)
        details: dict[str, JSONValue] = describe() if describe else {}
        return {
            **details,
            "source": self.source,
            "signals": list(self.capabilities()),
            "history": "recorded deployment/configuration; feature controls excluded",
        }

    async def query(self, query: TelemetryQuery) -> QueryResult:
        if query.kind in {"deployments", "configuration"}:
            provider = self.catalog.provider()
            result = await provider.query(query)
            if not any(r.kind == query.kind for r in provider.data.rows):
                return result.model_copy(
                    update={
                        "result": "no_match",
                        "note": "No recorded changes; history coverage unknown",
                    }
                )
            return result
        result = await self.live.query(query)
        try:
            self.catalog.observe(result)
        except OSError:
            result = result.model_copy(update={"note": result.note + "; topology persistence gap"})
        return result

    async def check_wait(self, condition: WaitCondition) -> bool:
        if condition.condition.get("source") not in {self.source, getattr(self.live, "source", "")}:
            raise ValueError("wait source mismatch")
        result = await self.query(
            TelemetryQuery.model_validate(
                {
                    "kind": condition.condition["signal"],
                    "scope": condition.scope.model_dump(),
                    "limit": 200,
                }
            )
        )
        watermark = datetime.fromisoformat(str(condition.condition["watermark"]))
        return any(row.data_at >= watermark for row in result.rows)


def observed_snapshot(
    rows: tuple[TelemetryRow, ...], scope: Scope, at: datetime
) -> CatalogSnapshot:
    spans = {
        (str(r.body.get("traceID")), str(r.body.get("spanID"))): r
        for r in rows
        if r.kind == "traces" and r.environment == scope.environment
    }
    edges: set[tuple[str, str]] = set()
    for row in spans.values():
        refs = row.body.get("references", [])
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict) and ref.get("refType") == "CHILD_OF":
                    parent = spans.get((str(ref.get("traceID")), str(ref.get("spanID"))))
                    if parent is not None and parent.entity != row.entity:
                        edges.add((parent.entity, row.entity))
    version = hashlib.sha256(
        "".join(r.model_dump_json() for r in spans.values()).encode()
    ).hexdigest()
    return CatalogSnapshot(
        version=version,
        scope=scope,
        available_at=at,
        data_at=max((r.data_at for r in spans.values()), default=at),
        entities=tuple(sorted({r.entity for r in spans.values()})),
        edges=tuple(sorted(edges)),
        origin="observed",
        configuration={"coverage": "sampled/unknown; edges are observations, not causality"},
    )
