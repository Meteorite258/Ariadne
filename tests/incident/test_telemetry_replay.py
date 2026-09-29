from datetime import timedelta

import pytest

from tau_incident.telemetry import (
    Coverage,
    FixtureProvider,
    ReplayData,
    TelemetryQuery,
    TelemetryRow,
)
from tau_incident.telemetry.catalog import EnvironmentChange, EnvironmentHistory, VersionedCatalog
from tau_incident.telemetry.dataset import DatasetExporter, load_dataset


def fixture(clock, scope, **updates):
    rows = tuple(
        TelemetryRow(
            kind="logs",
            environment=scope.environment,
            entity="checkout",
            data_at=clock(),
            available_at=clock() + timedelta(seconds=i),
            body={"message": f"failure {i}"},
            units="events",
            source="backend",
        )
        for i in range(3)
    )
    data = ReplayData(
        name="test",
        rows=rows,
        coverage=(
            Coverage(kind="logs", scope=scope, available_at=clock(), note="captured window"),
        ),
    )
    return FixtureProvider(data.model_copy(update=updates), clock=clock, source="test-fixture")


@pytest.mark.anyio
async def test_late_rows_scope_pagination_and_coverage(clock, case):
    provider = fixture(clock, case.scope)
    query = TelemetryQuery(kind="logs", scope=case.scope)
    early = await provider.query(query)
    assert len(early.rows) == 1
    assert early.result == "partial"
    assert early.actual_coverage is None
    clock.advance(3)
    page = await provider.query(query.model_copy(update={"limit": 2}))
    assert page.truncated and page.next_offset == 2
    assert page.result == "partial"
    final = await provider.query(query.model_copy(update={"offset": 2}))
    assert len(final.rows) == 1 and final.next_offset is None
    assert final.rows[0].units == "events"
    complete = await provider.query(query)
    assert complete.result == "complete" and complete.actual_coverage == case.scope
    assert (
        await provider.query(query.model_copy(update={"contains": "absent"}))
    ).result == "no_match"
    assert (await provider.query(query.model_copy(update={"kind": "metrics"}))).result == "failed"
    other = case.scope.model_copy(update={"environment": "other"})
    assert not (await provider.query(query.model_copy(update={"scope": other}))).rows


@pytest.mark.anyio
async def test_export_replay_new_query_and_tamper_detection(clock, case, tmp_path):
    provider = fixture(clock, case.scope)
    clock.advance(3)
    path = tmp_path / "dataset"
    manifest = await DatasetExporter(provider, provider, clock=clock).export(case.scope, path)
    assert len(manifest.pages) == 1
    replay = FixtureProvider(load_dataset(path), clock=clock, source="export")
    query = TelemetryQuery(kind="logs", scope=case.scope, contains="failure 2")
    assert (await replay.query(query)).rows == (await provider.query(query)).rows
    raw = path / (manifest.pages[0].sha256 + ".json")
    raw.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="response hash"):
        load_dataset(path)


@pytest.mark.anyio
async def test_recorded_deployment_retains_source_through_export_and_replay(clock, case, tmp_path):
    catalog = VersionedCatalog(tmp_path / "environment.json", clock=clock)
    catalog.save(
        EnvironmentHistory(
            changes=(
                EnvironmentChange(
                    change_id="deployment-1",
                    kind="deployment",
                    environment=case.scope.environment,
                    entity="checkout",
                    data_at=clock(),
                    available_at=clock(),
                    before_revision="before",
                    after_revision="after",
                    before={"PAYMENT_ADDR": "payment:50051"},
                    after={"PAYMENT_ADDR": "payment-old:50051"},
                    applied_receipt="sha256:applied",
                ),
            )
        )
    )
    provider = catalog.provider()
    query = TelemetryQuery(kind="deployments", scope=case.scope)
    live = await provider.query(query)
    assert len(live.rows) == 1
    assert live.rows[0].source == live.source

    path = tmp_path / "deployment-dataset"
    await DatasetExporter(provider, catalog, clock=clock).export(case.scope, path)
    replay = FixtureProvider(load_dataset(path), clock=clock, source="export")
    result = await replay.query(query)
    assert result.rows == live.rows
    assert result.rows[0].source.startswith("environment-history:")


@pytest.mark.anyio
async def test_failed_capture_never_becomes_successful_empty_query(clock, case):
    query = TelemetryQuery(kind="logs", scope=case.scope)
    provider = fixture(clock, case.scope, rows=(), failed_queries=(query,))
    result = await provider.query(query)
    assert result.result == "failed"
    assert result.actual_coverage is None
