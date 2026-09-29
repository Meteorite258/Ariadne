import json

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.context import evidence_ref
from tau_incident.events import Command, RecordReviews
from tau_incident.models import ClaimRevision, Finding, Source, VersionRef
from tau_incident.review import Reviewer, schedule
from tau_incident.submission import SubmissionService
from tau_incident.telemetry import FixtureProvider, ReplayData

from .test_dispatch_wait import queue_task
from .test_investigation import runner
from .test_review_repair import observation


@pytest.mark.anyio
async def test_tentative_claims_wait_for_adoption_then_share_one_review(runtime, case, clock):
    lease = runtime.acquire_owner("case", RunLimits())
    evidence = observation(runtime, case, "signal")
    for identity, kind in (("explore", "interpretation"), ("synthesis", "diagnosis")):
        task = queue_task(runtime, case, identity)
        attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
        source = Source(kind="runtime", actor="worker", reference=attempt.attempt_id)
        claims = tuple(
            ClaimRevision(
                claim_id=name,
                case_id="case",
                version=1,
                statement=name,
                scope=case.scope,
                source=source,
                support=(evidence_ref(evidence),),
                judgment=kind,
                premises=tuple(
                    VersionRef(case_id="case", kind="claim", object_id=parent, version=1)
                    for parent in ("latency", "deployment")
                )
                if kind == "diagnosis"
                else (),
                revision_reason="fixture",
                validity="needs_review",
            )
            for name in (("latency", "deployment") if kind == "interpretation" else ("diagnosis",))
        )
        finding = Finding(
            finding_id=identity,
            case_id="case",
            attempt_id=attempt.attempt_id,
            scope=case.scope,
            source=source,
            observations=(evidence_ref(evidence),),
            proposed_claims=claims,
            completion="complete",
            satisfied_conditions=task.condition_ids,
        )
        receipt = SubmissionService(runtime).submit(
            finding, execution_token=attempt.execution_token
        )
        assert receipt.status == "accepted", receipt.reason
        if kind == "interpretation":
            assert runtime.get_case("case").review_issues == ()
            assert not schedule(runtime, "case")

    assert len(runtime.get_case("case").review_issues) == 3
    assert schedule(runtime, "case")
    assert not schedule(runtime, "case")  # Synthesis must wait for both premises.
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    state = runtime.get_case("case")
    task = next(t for t in state.tasks if t.task_id == attempt.task_id)
    assert {r.object_id for r in task.related_claims} == {"latency", "deployment"}
    assigned = [i for i in state.review_issues if i.target in task.related_claims]
    provider = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=json.dumps(
                            {
                                "reviews": {
                                    i.review_id: {
                                        "disposition": "accepted",
                                        "assessment": "supported by recorded signal",
                                        "gap": "none",
                                        "impact": "causal input",
                                        "required_action": "none",
                                        "basis": [
                                            r.model_dump(mode="json") for r in (i.target, *i.basis)
                                        ],
                                    }
                                    for i in assigned
                                }
                            }
                        )
                    )
                )
            ]
        ]
    )
    role = runner(runtime, clock, provider, RunLimits())
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    proposed = await Reviewer(runtime, role, telemetry, telemetry).review(attempt)
    assert isinstance(proposed, RecordReviews)
    command = Command(command_id="batch", case_id="case", payload=proposed)
    receipt = runtime.execute(command)
    assert receipt.status == "accepted", receipt.reason
    assert runtime.execute(command) == receipt
    final = runtime.get_case("case")
    assert all(c.validity == "current" for c in final.claims if c.claim_id != "diagnosis")
    assert final.attempts[-1].status == "succeeded"
    assert len(provider.calls) == 1
    assert runtime.store.case_at_version("case", final.version) == final
    # Accepted premises have new revisions. The synthesis must repair its stale
    # references, then receive its own review; it must not wait forever.
    assert schedule(runtime, "case")
    repair_attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    current = runtime.get_case("case")
    repair_task = next(t for t in current.tasks if t.task_id == repair_attempt.task_id)
    assert repair_task.kind == "repair"
    diagnosis = next(c for c in current.claims if c.claim_id == "diagnosis")
    repaired = diagnosis.model_copy(
        update={
            "version": diagnosis.version + 1,
            "dependencies": tuple(
                d.model_copy(update={"reference": d.reference.model_copy(update={"version": 2})})
                if d.relation == "derived_from"
                else d
                for d in diagnosis.dependencies
            ),
            "source": Source(kind="runtime", actor="repair", reference=repair_attempt.attempt_id),
            "revision_reason": "Rebase synthesis onto reviewed premise revisions",
        }
    )
    finding = Finding(
        finding_id="repaired-synthesis",
        case_id="case",
        attempt_id=repair_attempt.attempt_id,
        scope=case.scope,
        source=repaired.source,
        observations=(evidence_ref(evidence),),
        proposed_claims=(repaired,),
        completion="complete",
        satisfied_conditions=repair_task.condition_ids,
    )
    receipt = SubmissionService(runtime).submit(
        finding, execution_token=repair_attempt.execution_token
    )
    assert receipt.status == "accepted", receipt.reason
    from tau_incident.reporting import ReportBuilder

    from .test_review_repair import review

    accepted = await review(runtime, case, clock, lease, "accepted")
    receipt = runtime.execute(Command(command_id="final-review", case_id="case", payload=accepted))
    assert receipt.status == "accepted", receipt.reason
    builder = ReportBuilder(runtime)
    report = builder.commit(builder.build("case"))
    assert report.kind == "diagnosis"
    assert runtime.get_case("case").investigation_status == "completed"
