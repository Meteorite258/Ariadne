"""Portable queryable datasets with raw response hashes and explicit coverage gaps."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, Field

from tau_incident.models import Model, Scope
from tau_incident.telemetry import (
    CatalogSnapshot,
    Coverage,
    QueryResult,
    ReplayData,
    ServiceCatalog,
    SignalKind,
    TelemetryProvider,
    TelemetryQuery,
    TelemetryRow,
)


class ExportPage(Model):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    query: TelemetryQuery
    source: str
    collected_at: AwareDatetime
    result: str
    note: str


class DatasetManifest(Model):
    schema_version: Literal[1] = 1
    data_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Scope
    exported_at: AwareDatetime
    pages: tuple[ExportPage, ...]
    gaps: tuple[str, ...]


class DatasetExporter:
    def __init__(
        self,
        provider: TelemetryProvider,
        catalog: ServiceCatalog,
        *,
        clock: Callable[[], datetime],
        max_pages: int = 100,
    ) -> None:
        if not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        self.provider, self.catalog, self.clock = provider, catalog, clock
        self.max_pages = max_pages

    async def export(self, query_scope: Scope, destination: Path) -> DatasetManifest:
        destination.mkdir(parents=True, exist_ok=False)
        rows: dict[str, TelemetryRow] = {}
        pages: list[ExportPage] = []
        coverage: list[Coverage] = []
        gaps: list[str] = []
        sampling: dict[SignalKind, bool | None] = {}
        failures: list[TelemetryQuery] = []
        # Manifest is published last; an interrupted directory is not a valid dataset.
        for kind in self.provider.capabilities():
            offset = 0
            for _ in range(self.max_pages):
                result = await self.provider.query(
                    TelemetryQuery(kind=kind, scope=query_scope, offset=offset, limit=200)
                )
                raw = result.model_dump_json().encode()
                sampling[kind] = result.sampled
                if result.result == "failed":
                    failures.append(result.query)
                digest = hashlib.sha256(raw).hexdigest()
                (destination / (digest + ".json")).write_bytes(raw)
                pages.append(
                    ExportPage(
                        sha256=digest,
                        query=result.query,
                        source=result.source,
                        collected_at=result.collected_at,
                        result=result.result,
                        note=result.note,
                    )
                )
                for row in result.rows:
                    # Preserve earliest availability across export pages.
                    key = row.model_dump_json(exclude={"available_at"})
                    if key not in rows or row.available_at < rows[key].available_at:
                        rows[key] = row
                if result.actual_coverage is not None and result.result != "failed":
                    coverage.append(
                        Coverage(
                            kind=kind,
                            scope=result.actual_coverage,
                            available_at=result.collected_at,
                            sampled=result.sampled is not False,
                            note=result.note,
                        )
                    )
                else:
                    gaps.append(f"{kind} offset {offset}: {result.note}")
                if result.next_offset is None:
                    break
                if result.next_offset <= offset:
                    raise ValueError("provider returned a non-advancing cursor")
                offset = result.next_offset
            else:
                gaps.append(f"{kind}: export page limit reached")
        snapshot = await self.catalog.snapshot(query_scope)
        snapshots: tuple[CatalogSnapshot, ...] = () if snapshot is None else (snapshot,)
        history = getattr(self.catalog, "snapshots", None)
        if history is not None:
            snapshots = await history(query_scope)
        data = ReplayData(
            name=destination.name,
            rows=tuple(rows.values()),
            coverage=tuple(coverage),
            catalog=snapshots,
            signals=self.provider.capabilities(),
            gaps=tuple(dict.fromkeys(gaps)),
            sampling=sampling,
            failed_queries=tuple(failures),
        )
        content = data.model_dump_json().encode()
        (destination / "data.json").write_bytes(content)
        manifest = DatasetManifest(
            data_sha256=hashlib.sha256(content).hexdigest(),
            scope=query_scope,
            exported_at=self.clock(),
            pages=tuple(pages),
            gaps=tuple(dict.fromkeys(gaps)),
        )
        (destination / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
        return manifest


def load_dataset(path: Path) -> ReplayData:
    manifest = DatasetManifest.model_validate_json((path / "manifest.json").read_bytes())
    content = (path / "data.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != manifest.data_sha256:
        raise ValueError("dataset hash mismatch")
    for page in manifest.pages:
        file = path / (page.sha256 + ".json")
        if file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != page.sha256:
            raise ValueError("dataset response hash mismatch")
        QueryResult.model_validate_json(file.read_bytes())
    return ReplayData.model_validate_json(content)
