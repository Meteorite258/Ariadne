import sqlite3

import pytest

from tau_incident.coordinator import LocalRecordingError
from tau_incident.events import AddExplanation, AddObservation, Command
from tau_incident.models import ObservationInput, Source, VersionRef
from tau_incident.store import IdempotencyConflict


def observation(case, command_id="observe"):
    return Command(
        command_id=command_id,
        case_id=case.case_id,
        payload=AddObservation(
            observation=ObservationInput(
                summary="error count",
                raw_text="3 errors",
                scope=case.scope,
                source=Source(kind="human", actor="operator"),
                result="partial",
                units="errors",
            )
        ),
    )


def test_command_idempotency_conflict_and_replay(runtime, create_command, case):
    first = runtime.store.receipt("create")
    assert runtime.execute(create_command) == first
    changed = create_command.model_copy(
        update={"payload": create_command.payload.model_copy(update={"symptoms": "different"})}
    )
    with pytest.raises(IdempotencyConflict):
        runtime.execute(changed)
    assert runtime.get_case("case") == case
    assert len(runtime.store.events("case")) == 1
    assert runtime.store.case_at_version("case", 1) == case


def test_observation_is_durable_separate_from_explanation(runtime, case):
    command = observation(case)
    receipt = runtime.execute(command)
    assert receipt.status == "accepted"
    assert runtime.execute(command) == receipt
    state = runtime.get_case("case")
    assert len(state.observations) == 1
    evidence = state.observations[0]
    assert runtime.store.read_evidence("case", evidence.evidence_id) == b"3 errors"
    assert evidence.actual_coverage is None
    assert evidence.result == "partial"
    assert not state.claims
    runtime.execute(
        Command(
            command_id="explain",
            case_id="case",
            payload=AddExplanation(
                text="payment may be unavailable",
                scope=case.scope,
                source=Source(kind="human", actor="operator"),
            ),
        )
    )
    state = runtime.get_case("case")
    assert len(state.candidate_explanations) == 1
    assert not state.claims
    assert runtime.store.case_at_version("case", state.version) == state


def test_stale_version_conflicts_without_event(runtime, case):
    runtime.execute(observation(case))
    stale = VersionRef(case_id="case", kind="case", object_id="case", version=1)
    result = runtime.execute(observation(case, "stale"), expected_versions=(stale,))
    assert result.status == "conflict"
    assert runtime.get_case("case").version == 2
    assert len(runtime.store.events("case")) == 2


def test_artifact_failure_cannot_commit_evidence(runtime, case, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.evidence.artifacts, "put", fail)
    with pytest.raises(OSError, match="disk full"):
        runtime.execute(observation(case))
    assert runtime.get_case("case") == case
    assert runtime.store.receipt("observe") is None
    assert runtime.store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_sql_failure_rolls_back_event_receipt_and_projection(runtime, case):
    runtime.store._connection.execute("""CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts
        BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        runtime.execute(observation(case))
    assert runtime.get_case("case") == case
    assert runtime.store.receipt("observe") is None
    assert len(runtime.store.events("case")) == 1
    assert runtime.store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


def test_postcommit_record_failure_can_reconcile_receipt(runtime, case, monkeypatch):
    finish = runtime.execution.finish

    def fail_once(operation_id, **kwargs):
        monkeypatch.setattr(runtime.execution, "finish", finish)
        raise OSError("record unavailable")

    monkeypatch.setattr(runtime.execution, "finish", fail_once)
    command = observation(case)
    with pytest.raises(LocalRecordingError) as caught:
        runtime.execute(command)
    assert caught.value.receipt == runtime.store.receipt("observe")
    assert runtime.execute(command) == caught.value.receipt
    assert len(runtime.get_case("case").observations) == 1


def test_artifact_write_runs_outside_database_transaction(runtime, case, monkeypatch):
    put = runtime.evidence.artifacts.put

    def checked(content, **kwargs):
        assert not runtime.store._connection.in_transaction
        return put(content, **kwargs)

    monkeypatch.setattr(runtime.evidence.artifacts, "put", checked)
    assert runtime.execute(observation(case)).status == "accepted"


def test_artifact_hash_and_path_validation(runtime):
    artifacts = runtime.evidence.artifacts
    reference = artifacts.put(b"original")
    (artifacts.root / reference.artifact_id).write_bytes(b"modified")
    with pytest.raises(ValueError, match="size/hash"):
        artifacts.read(reference)
    with pytest.raises(ValueError, match="invalid artifact ID"):
        artifacts._path("../outside")
