"""Queryable, time-aware product replay data and provider-neutral telemetry contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field

from tau_agent.types import JSONValue
from tau_incident.models import Model, ResultKind, Scope, WaitCondition

SignalKind = Literal["metrics", "logs", "traces", "deployments", "configuration"]


class TelemetryQuery(Model):
    kind: SignalKind
    scope: Scope
    contains: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class TelemetryRow(Model):
    kind: SignalKind
    environment: str
    entity: str
    data_at: AwareDatetime
    available_at: AwareDatetime
    body: dict[str, JSONValue]
    units: str | None = None
    source: str | None = None


class Coverage(Model):
    kind: SignalKind
    scope: Scope
    available_at: AwareDatetime
    sampled: bool = False
    note: str


class CatalogSnapshot(Model):
    version: str
    scope: Scope
    available_at: AwareDatetime
    entities: tuple[str, ...]
    edges: tuple[tuple[str, str], ...] = ()
    configuration: dict[str, JSONValue] = Field(default_factory=dict)
    origin: Literal["declared", "observed"] = "declared"
    data_at: AwareDatetime | None = None


class ReplayData(Model):
    schema_version: Literal[1] = 1
    name: str
    rows: tuple[TelemetryRow, ...]
    coverage: tuple[Coverage, ...] = ()
    catalog: tuple[CatalogSnapshot, ...] = ()
    signals: tuple[SignalKind, ...] = ()
    gaps: tuple[str, ...] = ()
    sampling: dict[SignalKind, bool | None] = Field(default_factory=dict)
    failed_queries: tuple[TelemetryQuery, ...] = ()


class QueryResult(Model):
    query: TelemetryQuery
    rows: tuple[TelemetryRow, ...]
    result: ResultKind
    actual_coverage: Scope | None
    sampled: bool | None
    truncated: bool
    next_offset: int | None
    note: str
    source: str
    collected_at: AwareDatetime
    actual_query: dict[str, JSONValue] = Field(default_factory=dict)
    raw: JSONValue = None


class TelemetryProvider(Protocol):
    def capabilities(self) -> tuple[SignalKind, ...]: ...
    async def query(self, query: TelemetryQuery) -> QueryResult: ...


class ServiceCatalog(Protocol):
    async def snapshot(self, scope: Scope) -> CatalogSnapshot | None: ...


class FixtureProvider:
    def __init__(self, data: ReplayData, *, clock: Callable[[], datetime], source: str) -> None:
        self.data, self.clock, self.source = data, clock, source

    @classmethod
    def from_file(cls, path: Path, *, clock: Callable[[], datetime]) -> FixtureProvider:
        content = path.read_bytes()
        return cls(
            ReplayData.model_validate_json(content),
            clock=clock,
            source=f"fixture:{path.name}:sha256:{hashlib.sha256(content).hexdigest()}",
        )

    def capabilities(self) -> tuple[SignalKind, ...]:
        return tuple(
            sorted(
                {r.kind for r in self.data.rows}
                | {c.kind for c in self.data.coverage}
                | set(self.data.signals)
            )
        )

    def describe(self) -> dict[str, JSONValue]:
        return {
            "source": self.source,
            "signals": list(self.capabilities()),
            "metric_names": list[JSONValue](
                sorted(
                    {
                        str(r.body.get("metric"))
                        for r in self.data.rows
                        if r.kind == "metrics" and r.body.get("metric")
                    }
                )
            ),
            "query": "entity/time/contains; contains is a literal row substring",
        }

    async def check_wait(self, condition: WaitCondition) -> bool:
        """Inspect the replay data watermark without querying a model or recording evidence."""
        if condition.condition.get("source") not in {self.source, self.data.name}:
            raise ValueError("wait source does not match the configured fixture")
        watermark = datetime.fromisoformat(str(condition.condition["watermark"]))
        scope = condition.scope
        return any(
            row.kind == condition.condition["signal"]
            and row.environment == scope.environment
            and (not scope.entities or row.entity in scope.entities)
            and row.available_at <= self.clock()
            and row.data_at >= watermark
            and (scope.time_window.start is None or row.data_at >= scope.time_window.start)
            and (scope.time_window.end is None or row.data_at <= scope.time_window.end)
            for row in self.data.rows
        )

    async def query(self, query: TelemetryQuery) -> QueryResult:
        from tau_incident.submission import scope_contains

        now = self.clock()
        if query.kind not in self.capabilities():
            return QueryResult(
                query=query,
                rows=(),
                result="failed",
                actual_coverage=None,
                sampled=None,
                truncated=False,
                next_offset=None,
                note="Unsupported signal kind",
                source=self.source,
                collected_at=now,
            )
        window = query.scope.time_window
        matches = tuple(
            row
            for row in self.data.rows
            if (
                row.kind == query.kind
                and row.environment == query.scope.environment
                and row.available_at <= now
                and (not query.scope.entities or row.entity in query.scope.entities)
                and (window.start is None or row.data_at >= window.start)
                and (window.end is None or row.data_at <= window.end)
                and (query.contains is None or query.contains in row.model_dump_json())
            )
        )
        matches = tuple(sorted(matches, key=lambda r: (r.data_at, r.entity, r.model_dump_json())))
        page = matches[query.offset : query.offset + query.limit]
        truncated = query.offset + len(page) < len(matches)
        coverage = next(
            (
                c
                for c in self.data.coverage
                if c.kind == query.kind
                and c.available_at <= now
                and scope_contains(c.scope, query.scope)
            ),
            None,
        )
        delayed = any(
            r.kind == query.kind
            and r.environment == query.scope.environment
            and r.available_at > now
            and (not query.scope.entities or r.entity in query.scope.entities)
            and (window.start is None or r.data_at >= window.start)
            and (window.end is None or r.data_at <= window.end)
            for r in self.data.rows
        )
        complete = (
            coverage is not None
            and not coverage.sampled
            and not truncated
            and query.offset == 0
            and not delayed
        )
        result: ResultKind = "no_match" if not page else "complete" if complete else "partial"
        failed_capture = any(
            q.kind == query.kind and scope_contains(q.scope, query.scope)
            for q in self.data.failed_queries
        )
        if failed_capture:
            complete = False
            result = "partial" if page else "failed"
        return QueryResult(
            query=query,
            rows=page,
            result=result,
            actual_coverage=query.scope if complete else None,
            sampled=coverage.sampled if coverage else self.data.sampling.get(query.kind),
            truncated=truncated,
            next_offset=query.offset + len(page) if truncated else None,
            note=(coverage.note if coverage else "Coverage unknown")
            + ("; data is not yet available" if delayed else "")
            + ("; page only" if query.offset or truncated else "")
            + ("; export failed in this scope" if failed_capture else "")
            + ("; export gaps: " + "; ".join(self.data.gaps[:10]) if self.data.gaps else ""),
            source=self.source,
            collected_at=now,
        )

    async def snapshots(self, scope: Scope) -> tuple[CatalogSnapshot, ...]:
        from tau_incident.submission import scope_contains

        return tuple(
            c
            for c in self.data.catalog
            if c.available_at <= self.clock()
            and scope_contains(c.scope, scope)
            and (
                c.data_at is None
                or scope.time_window.end is None
                or c.data_at <= scope.time_window.end
            )
        )

    async def snapshot(self, scope: Scope) -> CatalogSnapshot | None:
        choices = await self.snapshots(scope)
        if not choices:
            return None
        selected = max(choices, key=lambda c: c.available_at)
        entities = tuple(e for e in selected.entities if not scope.entities or e in scope.entities)
        return selected.model_copy(
            update={
                "scope": scope,
                "entities": entities,
                "edges": tuple(edge for edge in selected.edges if all(e in entities for e in edge)),
            }
        )
