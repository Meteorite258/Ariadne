"""Validate a real exported dataset using independent replay queries, without a model."""

import argparse
import asyncio
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

from tau_incident.telemetry import FixtureProvider, QueryResult, TelemetryQuery
from tau_incident.telemetry.dataset import DatasetManifest, load_dataset


async def main(directory: Path) -> None:
    manifest = DatasetManifest.model_validate_json((directory / "manifest.json").read_bytes())
    data = load_dataset(directory)
    now = manifest.exported_at
    provider = FixtureProvider(data, clock=lambda: now, source="export-validation")
    counts = Counter(row.kind for row in data.rows)
    for signal in ("logs", "metrics", "traces"):
        assert counts[signal] > 0, f"missing actual {signal} rows"
    assert all(row.environment == manifest.scope.environment for row in data.rows)
    assert all(row.entity in manifest.scope.entities for row in data.rows)
    assert all(row.source for row in data.rows)
    assert data.gaps == manifest.gaps
    text = (directory / "data.json").read_text()
    for forbidden in ("paymentUnreachable", "paymentFailure", "control/scenarios", "amadeus-agent"):
        assert forbidden not in text, f"control/agent data leaked: {forbidden}"
    for page in manifest.pages:
        result = QueryResult.model_validate_json((directory / (page.sha256 + ".json")).read_bytes())
        assert result.result != "failed", (page.query.kind, result.note)
    queries = 0
    for signal in provider.capabilities():
        for entity in manifest.scope.entities:
            scope = manifest.scope.model_copy(update={"entities": (entity,)})
            expected = tuple(
                row for row in data.rows if row.kind == signal and row.entity == entity
            )
            rows = []
            offset = 0
            while True:
                result = await provider.query(
                    TelemetryQuery(kind=signal, scope=scope, offset=offset, limit=7)
                )
                queries += 1
                assert result.result != "failed"
                rows.extend(result.rows)
                if result.next_offset is None:
                    break
                assert result.next_offset > offset
                offset = result.next_offset
            assert Counter(row.model_dump_json() for row in rows) == Counter(
                row.model_dump_json() for row in expected
            )
    if data.rows:
        now = min(row.available_at for row in data.rows) - timedelta(microseconds=1)
        for signal in provider.capabilities():
            result = await provider.query(TelemetryQuery(kind=signal, scope=manifest.scope))
            assert not result.rows, "replay exposed data before availability"
    print(
        json.dumps(
            {
                "rows": counts,
                "replay_queries": queries,
                "data_sha256": manifest.data_sha256,
                "gaps_preserved": len(data.gaps),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.directory))
