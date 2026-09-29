import hashlib
import json
import sqlite3

import pytest

from tau_incident.history import HistoricalCases


def test_history_opens_without_migration_or_writes(runtime, case, tmp_path):
    path = tmp_path / "cases.sqlite"
    runtime.store._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    all_before = {p.name: p.read_bytes() for p in tmp_path.glob("cases.sqlite*")}
    with HistoricalCases(path, tmp_path / "artifacts") as history:
        assert history.case("case")["symptoms"] == case.symptoms
        assert history.events("case", 0, 100)[0]["case_version"] == 1
        with pytest.raises(Exception, match="readonly"):
            history.connection.execute("DELETE FROM cases")
        exported = history.export("case")
        assert json.loads(exported["case.json"])["case_id"] == "case"
        manifest = json.loads(exported["manifest.json"])
        assert manifest["schema_version"] == 2
        assert all(
            hashlib.sha256(exported[name]).hexdigest() == digest
            for name, digest in manifest["files"].items()
        )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert {p.name: p.read_bytes() for p in tmp_path.glob("cases.sqlite*")} == all_before


def test_missing_history_does_not_create_files(tmp_path):
    with HistoricalCases(tmp_path / "absent.sqlite", tmp_path / "artifacts") as history:
        assert not history.contains("case")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.anyio
async def test_frontend_archive_reads_original_shapes_and_rejects_all_writes(tmp_path, case, clock):
    from tau_coding.incident.actions import IncidentAction, IncidentQuery
    from tau_coding.incident.config import IncidentConfig
    from tau_coding.incident.host import IncidentHost
    from tau_incident.events import Command, ExtendBudget

    root = tmp_path / "archive"
    root.mkdir()
    database = root / "cases.sqlite3"
    old = case.model_dump(mode="json")
    old["project_key"] = "test-project"
    old["tasks"] = [{"task_id": "legacy-task", "budget": {"calls": 8}, "status": "needs_review"}]
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE cases(case_id TEXT PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO cases VALUES (?,?)", ("case", json.dumps(old)))
        connection.execute("PRAGMA user_version=5")
    original = database.read_bytes()
    with IncidentHost(
        IncidentConfig(tmp_path, "test", "test-project", root / "v2"), clock=clock
    ) as host:
        for view in ("case", "brief", "report", "handoff", "budget", "export"):
            result = host.query(IncidentQuery(case_id="case", view=view))
            assert result["historical_readonly"]
        bound = await host.dispatch(
            IncidentAction(request_id="bind", operation="bind", case_id="case")
        )
        assert bound["case"]["tasks"] == old["tasks"]
        for operation in ("run", "resume"):
            with pytest.raises(ValueError, match="historical.*read-only"):
                await host.dispatch(
                    IncidentAction(request_id=operation, operation=operation, case_id="case")
                )
        with pytest.raises(ValueError, match="historical.*read-only"):
            host.execute(
                Command(
                    command_id="policy",
                    case_id="case",
                    payload=ExtendBudget(
                        scope=case.scope,
                        clear_token_limit=True,
                        reason="not permitted",
                    ),
                )
            )
        assert host.store._connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert database.read_bytes() == original
    assert set(p.name for p in root.iterdir()) == {"cases.sqlite3", "v2"}
