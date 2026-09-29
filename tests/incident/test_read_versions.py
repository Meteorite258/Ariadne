import pytest

from tau_incident.budget import RunLimits
from tau_incident.context import evidence_ref
from tau_incident.events import AddObservation, Command
from tau_incident.models import Finding, ObservationInput, Source
from tau_incident.submission import SubmissionService

from .test_dispatch_wait import queue_task


@pytest.mark.parametrize("same_source", [False, True])
def test_finding_merges_unrelated_updates_but_reviews_changed_read_source(
    runtime, case, same_source
):
    owner = runtime.acquire_owner("case", RunLimits())

    def add(identity, actor, revision):
        receipt = runtime.execute(
            Command(
                command_id=identity,
                case_id="case",
                payload=AddObservation(
                    observation=ObservationInput(
                        scope=case.scope,
                        source=Source(kind="human", actor=actor),
                        summary=identity,
                        raw_text=identity,
                        result="complete",
                        actual_coverage=case.scope,
                        source_revision=revision,
                    )
                ),
            )
        )
        assert receipt.status == "accepted", receipt.reason
        return runtime.get_case("case").observations[-1]

    original = add("original", "configuration", "v1")
    queue_task(runtime, case, "inspect")
    attempt = runtime.claim_ready_tasks(owner.generation, 1)[0]
    assert evidence_ref(original) in attempt.basis
    added = add("new", "configuration" if same_source else "independent-source", "v2")
    finding = Finding(
        finding_id="final",
        case_id="case",
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=Source(kind="runtime", actor="worker", reference=attempt.attempt_id),
        observations=(evidence_ref(original),),
        completion="complete",
        satisfied_conditions=next(
            t.condition_ids
            for t in runtime.get_case(case.case_id).tasks
            if t.task_id == attempt.task_id
        ),
    )
    receipt = SubmissionService(runtime).submit(finding, execution_token=attempt.execution_token)
    assert receipt.status == "accepted", receipt.reason
    state = runtime.get_case("case")
    assert state.observations == (original, added)
    assert state.findings[0] == finding
    manifest = runtime.store.read_manifest("case", attempt.attempt_id)
    if same_source:
        assert state.tasks[0].status == "ready"
        assert evidence_ref(original) in manifest.changed
        assert any(issue.problem == "basis_changed" for issue in state.review_issues)
    else:
        assert state.tasks[0].status == "completed"
        assert not manifest.changed and not state.review_issues
