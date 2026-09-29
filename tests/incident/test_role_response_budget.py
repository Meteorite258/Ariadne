import json

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AgentTool, AgentToolResult, AssistantMessage, ToolCall
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.planner import PlanOutput

from .test_dispatch_wait import queue_task
from .test_investigation import runner


@pytest.mark.anyio
@pytest.mark.parametrize("task_bound", [False, True])
async def test_role_continues_beyond_old_eight_response_and_period_limits(
    runtime, case, clock, task_bound
):
    limits = RunLimits(format_repairs=0, output_tokens=512)
    lease = runtime.acquire_owner("case", limits)
    provider = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=[ToolCall(id=f"read-{index}", name="telemetry_query", arguments={})]
                    )
                )
            ]
            for index in range(12)
        ]
        + [
            [
                assistant_done(
                    AssistantMessage(
                        content=json.dumps({"choice": "progress", "reason": "gap remains"})
                    )
                )
            ],
        ]
    )
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    if task_bound:
        queue_task(runtime, case, "task")
        attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
        view = role.builder.build_task(attempt)
    else:
        attempt = None
        view = role.builder.build_decision(
            "case", tools=("telemetry_query",), sources="fixture", budget_limits={}
        )
    parent = runtime.execution.start(
        "planning",
        case_id="case",
        command_id="response-budget",
        runtime_generation=lease.generation,
        attempt_id=attempt.attempt_id if attempt else None,
    )

    async def inspect(*args):
        clock.advance(26)
        runtime.renew_owner(30)
        return AgentToolResult(content="partial evidence")

    tool = AgentTool(
        name="telemetry_query",
        label="Inspect",
        description="inspect",
        parameters={"type": "object", "properties": {}},
        execute_fn=inspect,
    )
    result = await role.run(PlanOutput, view=view, tools=[tool], parent=parent, instructions="plan")
    assert result.reason == "gap remains"
    assert len(provider.calls) == 13
    assert clock.now.second == 12  # 312 seconds of controlled role time, beyond the old 300.
    assert all(len(call[3]) == 1 for call in provider.calls)
    assert all("responses remaining" not in call[1] for call in provider.calls)
    requests = [
        r for r in runtime.store.executions(case_id="case") if r.operation_kind == "model_request"
    ]
    snapshots = [
        json.loads(
            runtime.store.artifacts.read(runtime.store.request("case", r.request_id).provider_input)
        )
        for r in requests
    ]
    assert {snapshot["system"] for snapshot in snapshots} == {call[1] for call in provider.calls}
    assert [len(snapshot["tools"]) for snapshot in snapshots] == [1] * 13


@pytest.mark.anyio
async def test_unoffered_tool_call_cannot_execute(runtime, case, clock, monkeypatch):
    from tau_incident.budget import StepCheckpoint
    from tau_incident.execution.provider import RecordedProvider

    original = RecordedProvider.project

    async def without_tools(self, request, **kwargs):
        return await original(self, request, force_no_tools=True)

    monkeypatch.setattr(RecordedProvider, "project", without_tools)

    limits = RunLimits(checkpoint_steps=1, format_repairs=0, output_tokens=512)
    lease = runtime.acquire_owner("case", limits)
    provider = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(content=[ToolCall(id="late", name="inspect", arguments={})])
                )
            ]
        ]
    )
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    view = role.builder.build_decision(
        "case", tools=("inspect",), sources="fixture", budget_limits={}
    )
    parent = runtime.execution.start(
        "planning", case_id="case", command_id="unoffered", runtime_generation=lease.generation
    )
    executions = 0

    async def execute(*args):
        nonlocal executions
        executions += 1
        return AgentToolResult(content="unexpected")

    tool = AgentTool(
        name="inspect",
        label="Inspect",
        description="inspect",
        parameters={"type": "object", "properties": {}},
        execute_fn=execute,
    )
    with pytest.raises(StepCheckpoint):
        await role.run(PlanOutput, view=view, tools=[tool], parent=parent, instructions="plan")
    assert provider.calls[0][3] == []
    assert executions == 0
