"""Upgrade a populated incident store while retaining committed case history."""

import sqlite3

import pytest

from tau_incident.coordinator import IncidentRuntime
from tau_incident.evidence import ArtifactStore, EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.history import HistoricalCases
from tau_incident.store import CaseStore


def _prior_database(path, version):
    """Remove tables introduced after an earlier schema version."""
    with sqlite3.connect(path) as connection:
        for table in ("incident_actions", "alert_inbox", "alert_updates", "external_incidents"):
            connection.execute(f"DROP TABLE {table}")
        if version <= 2:
            for table in (
                "owners",
                "policies",
                "request_results",
                "role_slots",
                "attempt_reservations",
            ):
                connection.execute(f"DROP TABLE {table}")
            if version == 2:
                connection.execute("ALTER TABLE request_usage DROP COLUMN generation")
        if version == 1:
            connection.execute("DROP TABLE requests")
            connection.execute("DROP TABLE request_usage")
        connection.execute(f"PRAGMA user_version = {version}")


def _populated_store(tmp_path, clock, create_command):
    artifacts = ArtifactStore(tmp_path / "artifacts")
    path = tmp_path / "cases.sqlite"
    store = CaseStore(path, artifacts=artifacts, clock=clock)
    runtime = IncidentRuntime(
        store, EvidenceRecorder(artifacts), ExecutionRecorder(store, clock=clock), clock=clock
    )
    receipt = runtime.execute(create_command)
    assert receipt.status == "accepted"
    case = store.get_case(create_command.case_id)
    store.close()
    return path, artifacts, case, receipt


@pytest.mark.parametrize("prior_version", [1, 2, 3, 4])
def test_prior_store_is_readonly_and_preserves_case_and_receipt(
    tmp_path, clock, create_command, prior_version
):
    path, artifacts, case, receipt = _populated_store(tmp_path, clock, create_command)
    _prior_database(path, prior_version)
    before = path.read_bytes()
    for _ in range(2):
        with pytest.raises(ValueError, match="historical case store is read-only"):
            CaseStore(path, artifacts=artifacts, clock=clock)
        with HistoricalCases(path, artifacts.root) as history:
            assert history.case(case.case_id) == case.model_dump(mode="json")
            assert history.query(case.case_id, "receipt", receipt.command_id)[
                "receipt"
            ] == receipt.model_dump(mode="json")
            assert len(history.events(case.case_id, 0, 100)) == 1
            assert history.connection.execute("PRAGMA user_version").fetchone()[0] == prior_version
    assert path.read_bytes() == before


def test_conflicting_old_schema_is_never_migrated(tmp_path, clock, create_command):
    path, artifacts, case, receipt = _populated_store(tmp_path, clock, create_command)
    _prior_database(path, 4)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alert_inbox (conflicting_schema TEXT)")

    with pytest.raises(ValueError, match="historical case store is read-only"):
        CaseStore(path, artifacts=artifacts, clock=clock)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute(
            "SELECT body FROM cases WHERE case_id=?", (case.case_id,)
        ).fetchone()
        assert connection.execute(
            "SELECT body FROM receipts WHERE command_id=?", (receipt.command_id,)
        ).fetchone()
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_actions'"
            ).fetchone()
            is None
        )
