from datetime import timedelta

import pytest

from tau_incident.budget import RunLimits
from tau_incident.events import AddObservation, Command, RecordPlan, RecordWait, SubmitFinding
from tau_incident.models import (
    Decision,
    Finding,
    InvestigationTask,
    ObservationInput,
    Source,
    VersionRef,
    WaitCondition,
)
from tau_incident.recovery import check_wait, recover


def queue_task(runtime, case, task_id, prerequisites=(), *, allowed_tools=("telemetry_query",)):
    source = Source(kind="runtime", actor="planner")
    task = InvestigationTask(
        task_id=task_id,
        case_id=case.case_id,
        contract_version=1,
        kind="explore",
        goal="inspect logs",
        scope=case.scope,
        source=source,
        completion_conditions=("inspect",),
        allowed_tools=allowed_tools,
        prerequisites=prerequisites,
    )
    decision = Decision(
        decision_id="plan:" + task_id,
        case_id=case.case_id,
        version=1,
        scope=case.scope,
        source=source,
        choice="investigate",
        reason="inspect",
        basis=(),
    )
    receipt = runtime.execute(
        Command(
            command_id="plan:" + task_id,
            case_id=case.case_id,
            payload=RecordPlan(scope=case.scope, decision=decision, task=task),
        )
    )
    assert receipt.status == "accepted", receipt.reason
    return task


def submit(runtime, case, attempt, command_id):
    observations = tuple(
        e for e in runtime.get_case(case.case_id).observations if e.attempt_id == attempt.attempt_id
    )
    refs = tuple(
        VersionRef(
            case_id=case.case_id, kind="evidence", object_id=e.evidence_id, version=e.version
        )
        for e in observations
    )
    finding = Finding(
        finding_id="finding:" + attempt.attempt_id,
        case_id=case.case_id,
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=Source(kind="runtime", actor="investigator", reference=attempt.attempt_id),
        observations=refs,
        completion="complete",
        satisfied_conditions=next(
            t.condition_ids
            for t in runtime.get_case(case.case_id).tasks
            if t.task_id == attempt.task_id
        ),
    )
    return runtime.execute(
        Command(
            command_id=command_id,
            case_id=case.case_id,
            payload=SubmitFinding(
                scope=case.scope, finding=finding, execution_token=attempt.execution_token
            ),
        )
    )


def test_dispatch_two_tasks_dependency_and_duplicate_final(runtime, case):
    owner = runtime.acquire_owner("case", RunLimits(concurrency=2))
    queue_task(runtime, case, "a")
    queue_task(runtime, case, "b")
    queue_task(
        runtime, case, "c", (VersionRef(case_id="case", kind="task", object_id="a", version=1),)
    )
    attempts = runtime.claim_ready_tasks(owner.generation, 10)
    assert {a.task_id for a in attempts} == {"a", "b"}
    assert len({a.trace_id for a in attempts}) == 2
    assert not runtime.claim_ready_tasks(owner.generation, 10)
    tool = runtime.execution.start(
        "tool",
        case_id="case",
        command_id="data",
        attempt_id=attempts[0].attempt_id,
        task_id=attempts[0].task_id,
        trace_id=attempts[0].trace_id,
        tool_call_id="query",
        runtime_generation=owner.generation,
    )
    receipt = runtime.execute(
        Command(
            command_id="data",
            case_id="case",
            payload=AddObservation(
                attempt_id=attempts[0].attempt_id,
                observation=ObservationInput(
                    summary="query output",
                    raw_text="failure",
                    result="partial",
                    scope=case.scope,
                    source=Source(kind="tool", actor="telemetry", reference=tool.operation_id),
                ),
            ),
        ),
        parent=tool,
    )
    runtime.execution.finish(tool.operation_id, status="succeeded", result=receipt.status)
    assert receipt.status == "accepted", receipt.reason
    first = submit(runtime, case, attempts[0], "final-a")
    assert first.status == "accepted", first.reason
    assert submit(runtime, case, attempts[0], "final-a") == first
    assert submit(runtime, case, attempts[0], "different-final").status == "rejected"
    next_attempts = runtime.claim_ready_tasks(owner.generation, 10)
    assert [a.task_id for a in next_attempts] == ["c"]


def test_takeover_fences_late_finding_and_retry_has_new_identity(runtime, case, clock):
    old = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "a")
    original = runtime.claim_ready_tasks(old.generation, 1)[0]
    clock.advance(31)
    new = runtime.acquire_owner("case", RunLimits())
    recover(runtime, new.generation)
    assert submit(runtime, case, original, "late").status == "rejected"
    replacement = runtime.claim_ready_tasks(new.generation, 1)[0]
    assert replacement.attempt_id != original.attempt_id
    assert replacement.predecessor_attempt_id == original.attempt_id
    state = runtime.get_case("case")
    assert state.attempts[0].status == "interrupted"
    assert state.tasks[0].active_attempt_id == replacement.attempt_id


@pytest.mark.anyio
@pytest.mark.parametrize("timeout_action", ["resume", "pause", "cancel"])
async def test_wait_does_not_block_other_task_and_timeout_is_explicit(
    runtime, case, clock, timeout_action
):
    owner = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "waiting")
    queue_task(runtime, case, "ready")
    wait = WaitCondition(
        wait_id="wait",
        case_id="case",
        version=1,
        scope=case.scope,
        source=Source(kind="runtime", actor="planner"),
        task_id="waiting",
        condition={"kind": "evidence"},
        next_check_at=clock() + timedelta(seconds=2),
        deadline=clock() + timedelta(seconds=5),
        on_timeout=timeout_action,
    )
    receipt = runtime.execute(
        Command(command_id="wait", case_id="case", payload=RecordWait(scope=case.scope, wait=wait))
    )
    assert receipt.status == "accepted", receipt.reason
    attempts = runtime.claim_ready_tasks(owner.generation, 2)
    assert [a.task_id for a in attempts] == ["ready"]
    assert not await check_wait(runtime, wait, clock)
    clock.advance(5)
    assert await check_wait(runtime, wait, clock)
    state = runtime.get_case("case")
    assert state.waits[0].status == "timed_out"
    waiting = next(t for t in state.tasks if t.task_id == "waiting")
    if timeout_action == "pause":
        assert state.investigation_status == "paused"
    else:
        assert waiting.status == ("ready" if timeout_action == "resume" else "cancelled")
