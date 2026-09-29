import json

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.context import evidence_ref
from tau_incident.events import AddObservation, Command, ReviseEvidence
from tau_incident.models import (
    ClaimRevision,
    EvidenceApplicability,
    Finding,
    ObservationInput,
    Source,
    VersionRef,
)
from tau_incident.reporting import ReportBuilder
from tau_incident.review import Reviewer, schedule
from tau_incident.submission import SubmissionService
from tau_incident.telemetry import FixtureProvider, ReplayData

from .test_dispatch_wait import queue_task
from .test_investigation import runner


def observation(runtime, case, identity):
    receipt = runtime.execute(
        Command(
            command_id=identity,
            case_id=case.case_id,
            payload=AddObservation(
                observation=ObservationInput(
                    summary=identity,
                    raw_text="independent measurement",
                    scope=case.scope,
                    actual_coverage=case.scope,
                    result="complete",
                    source=Source(kind="human", actor="operator"),
                )
            ),
        )
    )
    assert receipt.status == "accepted", receipt.reason
    return runtime.get_case(case.case_id).observations[-1]


def test_evidence_reference_cannot_be_used_as_claim_premise(runtime, case):
    evidence = observation(runtime, case, "premise-misread")
    with pytest.raises(ValueError, match="derivation requires a claim"):
        ClaimRevision(
            claim_id="misread",
            case_id=case.case_id,
            version=1,
            statement="direct observation",
            scope=case.scope,
            source=Source(kind="runtime", actor="investigator"),
            support=(evidence_ref(evidence),),
            premises=(evidence_ref(evidence),),
            revision_reason="new evidence",
            validity="needs_review",
        )


def candidate(runtime, case, *, opposition=()):
    lease = runtime.acquire_owner("case", RunLimits(max_repair_rounds=1))
    evidence = observation(runtime, case, "measurement")
    queue_task(runtime, case, "investigate")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    source = Source(kind="runtime", actor="investigator", reference=attempt.attempt_id)
    claim = ClaimRevision(
        claim_id="diagnosis",
        case_id="case",
        version=1,
        statement="dependency failure caused checkout errors",
        scope=case.scope,
        source=source,
        support=(evidence_ref(evidence),),
        opposition=opposition,
        validity="needs_review",
        judgment="diagnosis",
        revision_reason="initial candidate",
    )
    finding = Finding(
        finding_id="finding",
        case_id="case",
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=source,
        observations=(evidence_ref(evidence), *opposition),
        proposed_claims=(claim,),
        completion="complete",
        satisfied_conditions=("investigate:1:c1",),
    )
    receipt = SubmissionService(runtime).submit(finding, execution_token=attempt.execution_token)
    assert receipt.status == "accepted", receipt.reason
    return lease, evidence


async def review(runtime, case, clock, lease, disposition):
    assert schedule(runtime, "case")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    state = runtime.get_case("case")
    task = next(t for t in state.tasks if t.task_id == attempt.task_id)
    issue = next(i for i in state.review_issues if i.review_id == task.review_id)
    basis = (issue.target, *issue.basis)
    output = dict(
        disposition=disposition,
        assessment="reviewed the cited measurement",
        gap="need a distinguishing measurement" if disposition == "repair" else "none identified",
        impact="causal attribution",
        required_action="inspect the dependency configuration",
        basis=[r.model_dump(mode="json") for r in basis],
    )
    provider = FakeProvider([[assistant_done(AssistantMessage(content=json.dumps(output)))]])
    role = runner(runtime, clock, provider, RunLimits(output_tokens=512, format_repairs=0))
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    result = await Reviewer(runtime, role, telemetry, telemetry).review(attempt)
    return result


@pytest.mark.anyio
async def test_diagnosis_requires_review_and_invalidation_preserves_observation(
    runtime, case, clock
):
    lease, evidence = candidate(runtime, case)
    builder = ReportBuilder(runtime)
    assert builder.build("case").kind == "progress"
    result = await review(runtime, case, clock, lease, "accepted")
    receipt = runtime.execute(Command(command_id="review", case_id="case", payload=result))
    assert receipt.status == "accepted", receipt.reason
    state = runtime.get_case("case")
    assert state.claims[0].validity == "current" and state.claims[0].version == 2
    report = builder.commit(builder.build("case"))
    assert report.kind == "diagnosis" and report.verification == "unverified"
    change = ReviseEvidence(
        scope=case.scope,
        change=EvidenceApplicability(
            evidence=evidence_ref(evidence),
            version=1,
            applicable=False,
            reason="source correction",
            source=Source(kind="human", actor="operator"),
        ),
    )
    receipt = runtime.execute(Command(command_id="invalidate", case_id="case", payload=change))
    assert receipt.status == "accepted", receipt.reason
    updated = runtime.get_case("case")
    assert updated.observations == state.observations
    assert updated.claims[0].validity == "needs_review"
    assert updated.claims[0].version == 3
    assert updated.reports[-1].state == "stale"
    assert updated.investigation_status != "completed"


@pytest.mark.anyio
async def test_repair_without_new_evidence_stops_and_fresh_evidence_reopens(runtime, case, clock):
    lease, evidence = candidate(runtime, case)
    result = await review(runtime, case, clock, lease, "repair")
    receipt = runtime.execute(Command(command_id="review", case_id="case", payload=result))
    assert receipt.status == "accepted", receipt.reason
    assert schedule(runtime, "case")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    task = next(t for t in runtime.get_case("case").tasks if t.task_id == attempt.task_id)
    assert task.kind == "repair"
    finding = Finding(
        finding_id="no-progress",
        case_id="case",
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=Source(kind="runtime", actor="repair", reference=attempt.attempt_id),
        observations=(evidence_ref(evidence),),
        completion="complete",
        satisfied_conditions=task.condition_ids,
    )
    receipt = SubmissionService(runtime).submit(finding, execution_token=attempt.execution_token)
    assert receipt.status == "accepted", receipt.reason
    assert schedule(runtime, "case")
    issue = runtime.get_case("case").review_issues[-1]
    assert issue.status == "unresolved" and "no progress" in issue.stop_reason
    assert not schedule(runtime, "case")
    observation(runtime, case, "fresh-measurement")
    assert schedule(runtime, "case")
    reopened = runtime.get_case("case").review_issues[-1]
    assert reopened.cycle == issue.cycle + 1 and reopened.repair_rounds == 0


@pytest.mark.anyio
async def test_review_commit_rechecks_late_evidence(runtime, case, clock):
    lease, _ = candidate(runtime, case)
    result = await review(runtime, case, clock, lease, "accepted")
    observation(runtime, case, "late-counterevidence")
    receipt = runtime.execute(Command(command_id="late-review", case_id="case", payload=result))
    assert receipt.status == "rejected"
    assert "new relevant evidence" in receipt.reason
    assert runtime.get_case("case").claims[0].validity == "needs_review"


@pytest.mark.anyio
async def test_dependency_revision_propagates_without_invalidating_independent_claim(
    runtime, case, clock
):
    independent = observation(runtime, case, "independent")
    lease, original = candidate(runtime, case)
    accepted = await review(runtime, case, clock, lease, "accepted")
    assert (
        runtime.execute(Command(command_id="root-review", case_id="case", payload=accepted)).status
        == "accepted"
    )
    queue_task(runtime, case, "followup")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    source = Source(kind="runtime", actor="worker", reference=attempt.attempt_id)
    root_ref = VersionRef(case_id="case", kind="claim", object_id="diagnosis", version=2)
    claims = tuple(
        ClaimRevision(
            claim_id=identity,
            case_id="case",
            version=1,
            statement=identity,
            judgment="exclusion",
            scope=case.scope,
            source=source,
            support=(evidence_ref(independent),),
            premises=(root_ref,) if identity == "derived" else (),
            revision_reason="followup",
            validity="needs_review",
        )
        for identity in ("derived", "independent")
    )
    finding = Finding(
        finding_id="followup",
        case_id="case",
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=source,
        observations=(evidence_ref(independent),),
        proposed_claims=claims,
        completion="complete",
        satisfied_conditions=("followup:1:c1",),
    )
    assert (
        SubmissionService(runtime).submit(finding, execution_token=attempt.execution_token).status
        == "accepted"
    )
    for index in range(2):
        accepted = await review(runtime, case, clock, lease, "accepted")
        assert (
            runtime.execute(
                Command(command_id=f"followup-review-{index}", case_id="case", payload=accepted)
            ).status
            == "accepted"
        )
    before = runtime.get_case("case")
    assert all(c.validity == "current" for c in before.claims)
    change = ReviseEvidence(
        scope=case.scope,
        change=EvidenceApplicability(
            evidence=evidence_ref(original),
            version=1,
            applicable=False,
            reason="source corrected",
            source=Source(kind="human", actor="operator"),
        ),
    )
    assert (
        runtime.execute(
            Command(command_id="invalidate-root", case_id="case", payload=change)
        ).status
        == "accepted"
    )
    after = runtime.get_case("case")
    indexed = {claim.claim_id: claim for claim in after.claims}
    assert indexed["diagnosis"].validity == indexed["derived"].validity == "needs_review"
    assert indexed["independent"] == next(c for c in before.claims if c.claim_id == "independent")
    assert after.observations == before.observations
    assert runtime.store.case_at_version("case", after.version) == after
