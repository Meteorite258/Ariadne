import pytest

from tau_incident.context import ContextBuilder
from tau_incident.events import Command, SetTaskDisposition
from tau_incident.readiness import diagnosis_readiness
from tau_incident.reporting import ReportBuilder

from .test_dispatch_wait import queue_task
from .test_review_repair import candidate, observation, review


@pytest.mark.anyio
async def test_nonblocking_branch_requires_review_and_new_evidence_reopens(runtime, case, clock):
    lease, evidence = candidate(runtime, case)
    branch = queue_task(runtime, case, "unrelated-followup")
    reason = "Separate payment dashboard improvement; no bearing on checkout cause"
    receipt = runtime.execute(
        Command(
            command_id="disposition",
            case_id="case",
            payload=SetTaskDisposition(
                scope=case.scope,
                task_id=branch.task_id,
                contract_version=branch.contract_version,
                nonblocking=True,
                reason=reason,
            ),
        )
    )
    assert receipt.status == "accepted", receipt.reason
    assert any(
        "Unreviewed branch disposition" in b
        for b in diagnosis_readiness(runtime.get_case("case")).blockers
    )
    result = await review(runtime, case, clock, lease, "accepted")
    attempt = runtime.get_case("case").attempts[-1]
    context = ContextBuilder(runtime.store, estimate=len, input_limit=100000).build_task(attempt)
    assert reason in context.premises
    assert any(
        r.kind == "task" and r.object_id == branch.task_id and r.version == 2 for r in result.basis
    )
    assert (
        runtime.execute(Command(command_id="review", case_id="case", payload=result)).status
        == "accepted"
    )
    readiness = diagnosis_readiness(runtime.get_case("case"))
    assert readiness.ready, readiness.blockers
    assert any(reason in text for text in readiness.followups)
    builder = ReportBuilder(runtime)
    report = builder.commit(builder.build("case"))
    assert report.kind == "diagnosis" and report.recommendations == readiness.followups
    observation(runtime, case, "independent contradiction after delivery")
    current = runtime.get_case("case")
    assert current.reports[-1].state == "stale"
    assert current.investigation_status != "completed"
    assert not diagnosis_readiness(current).ready
    assert evidence == current.observations[0]
