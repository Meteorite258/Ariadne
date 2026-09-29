import json

import pytest

from tau_incident.budget import RunLimits
from tau_incident.context import ContextBuilder
from tau_incident.events import Command, UpdateTaskProgress
from tau_incident.models import TaskProgress

from .test_dispatch_wait import queue_task


def test_progress_is_nonterminal_versioned_fenced_and_replayable(runtime, case):
    lease = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "work")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    command = Command(
        command_id="progress-one",
        case_id="case",
        payload=UpdateTaskProgress(
            scope=case.scope,
            task_id="work",
            contract_version=1,
            expected_progress_version=0,
            attempt_id=attempt.attempt_id,
            execution_token=attempt.execution_token,
            progress=TaskProgress(
                version=1,
                attempt_id=attempt.attempt_id,
                summary="Need to compare deployment and latency",
                next_actions=("read deployment",),
            ),
        ),
    )
    receipt = runtime.execute(command)
    assert receipt.status == "accepted", receipt.reason
    assert runtime.execute(command) == receipt
    state = runtime.get_case("case")
    assert state.tasks[0].status == "running"
    assert state.attempts[0].status == "running"
    assert not state.findings
    assert runtime.store.case_at_version("case", state.version) == state
    stale = command.model_copy(update={"command_id": "stale"})
    assert runtime.execute(stale).status == "rejected"
    assert runtime.pause("case", "operator checkpoint").status == "accepted"
    assert runtime.execute(command.model_copy(update={"command_id": "late"})).status == "rejected"
    assert runtime.resume("case").status == "accepted"
    successor = runtime.claim_ready_tasks(lease.generation, 1)[0]
    assert successor.predecessor_attempt_id == attempt.attempt_id
    view = ContextBuilder(runtime.store, estimate=len, input_limit=100000).build_task(successor)
    assert view.task.progress.summary == "Need to compare deployment and latency"
    from tau_incident.events import ReviseTask

    current = runtime.get_case("case").tasks[0]
    revised = current.model_copy(
        update={
            "contract_version": 2,
            "goal": "narrow deployment comparison",
            "status": "ready",
            "active_attempt_id": None,
            "progress": TaskProgress(),
        }
    )
    changed = runtime.execute(
        Command(
            command_id="narrow",
            case_id="case",
            payload=ReviseTask(
                scope=case.scope,
                task=revised,
                reason="narrow context without losing accepted work",
            ),
        )
    )
    assert changed.status == "accepted", changed.reason
    assert runtime.get_case("case").tasks[0].progress == current.progress


def test_progress_cannot_invent_executed_checks(runtime, case):
    lease = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "work")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    receipt = runtime.execute(
        Command(
            command_id="invented",
            case_id="case",
            payload=UpdateTaskProgress(
                scope=case.scope,
                task_id="work",
                contract_version=1,
                expected_progress_version=0,
                attempt_id=attempt.attempt_id,
                execution_token=attempt.execution_token,
                progress=TaskProgress(
                    version=1, attempt_id=attempt.attempt_id, checked_operations=("imaginary",)
                ),
            ),
        )
    )
    assert receipt.status == "rejected"
    assert "invent" in receipt.reason


def test_partial_finding_preserves_work_without_review_or_completion(runtime, case):
    from tau_incident.models import Finding, Source
    from tau_incident.submission import SubmissionService

    lease = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "partial")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    finding = Finding(
        finding_id="partial",
        case_id="case",
        attempt_id=attempt.attempt_id,
        scope=case.scope,
        source=Source(kind="runtime", actor="worker", reference=attempt.attempt_id),
        summary="Need deployment comparison",
        completion="partial",
        next_actions=("compare deployment",),
    )
    receipt = SubmissionService(runtime).submit(finding, execution_token=attempt.execution_token)
    assert receipt.status == "accepted", receipt.reason
    state = runtime.get_case("case")
    assert state.tasks[0].status == "ready"
    assert state.tasks[0].progress.summary == finding.summary
    assert state.review_issues == ()
    assert state.attempts[0].status == "succeeded"
    resumed = runtime.claim_ready_tasks(lease.generation, 1)[0]
    assert resumed.predecessor_attempt_id == attempt.attempt_id
    assert runtime.get_case("case").tasks[0].progress.next_actions == ("compare deployment",)


def test_projection_rebuild_does_not_rebuild_or_clear_usage(runtime, case, clock):
    from .test_budget_recovery import setup_budget

    _, budget = setup_budget(runtime, clock, RunLimits(output_tokens=128))
    budget.reserve("request", "case", 100, None, role_operation_id="role")
    expected = runtime.get_case("case")
    totals = runtime.store.usage_totals("case")
    runtime.store._connection.execute("UPDATE cases SET body='{}' WHERE case_id='case'")
    assert runtime.store.rebuild_case_projection("case") == expected
    assert runtime.get_case("case") == expected
    assert runtime.store.usage_totals("case") == totals


def test_dispatch_does_not_reserve_a_whole_context_window(runtime, case, clock):
    from tau_incident.budget import RequestBudget

    limits = RunLimits(token_limit=500, output_tokens=128, context_tokens=32768)
    lease = runtime.acquire_owner("case", limits)
    queue_task(runtime, case, "small")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    assert runtime.store.budget_summary("case")["attempt_reserved_tokens"] == 0
    budget = RequestBudget(runtime.store, limits, clock)
    budget.owner_id, budget.owner_generation = lease.owner_id, lease.generation
    budget.reserve("small-request", "case", 100, attempt)
    assert runtime.store.usage_totals("case")[1] == 228
    assert runtime.store.usage_totals("case", task_id="small") == (1, 228, 0)
    assert runtime.store.usage_totals("case", attempt.attempt_id) == (1, 228, 0)
    runtime.pause("case", "checkpoint")
    runtime.resume("case")
    successor = runtime.claim_ready_tasks(lease.generation, 1)[0]
    budget.reserve("successor-request", "case", 100, successor)
    assert runtime.store.usage_totals("case", task_id="small") == (2, 456, 1)
    assert runtime.store.usage_totals("case", successor.attempt_id) == (1, 228, 0)


@pytest.mark.anyio
async def test_save_progress_tool_records_runtime_facts_and_is_idempotent(runtime, case):
    from tau_incident.progress import progress_tool

    lease = runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "work")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    parent = runtime.execution.start(
        "investigation",
        case_id="case",
        command_id="role",
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        runtime_generation=lease.generation,
    )
    checked = runtime.execution.start("tool", case_id="case", command_id="check", parent=parent)
    checked = runtime.execution.finish(checked.operation_id, status="succeeded", result="recorded")
    tool = progress_tool(runtime, attempt, parent, [])
    arguments = {
        "expected_progress_version": 0,
        "summary": "accepted intermediate work",
        "checked_operations": [checked.operation_id],
    }
    first = await tool.execute("save", arguments)
    second = await tool.execute("save", arguments)
    assert first == second
    assert json.loads(first.text)["status"] == "accepted"
    state = runtime.get_case("case")
    assert state.tasks[0].progress.execution_cursor == checked.cursor
    assert state.tasks[0].progress.version == 1
    assert state.tasks[0].status == "running"
    assert state.tasks[0].status_source_version == state.version
