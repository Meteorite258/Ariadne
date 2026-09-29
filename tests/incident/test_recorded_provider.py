import asyncio
import json

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage, UserMessage
from tau_agent.request_context import RequestContext
from tau_agent.tools import AgentTool, AgentToolResult
from tau_ai import FakeProvider
from tau_incident.budget import RequestBudget, RunLimits
from tau_incident.context import ContextBuilder, ContextInsufficient
from tau_incident.execution.provider import RecordedProvider
from tau_incident.store.control import reserve_role


def recorded(runtime, clock, provider, *, input_limit=28000, limits=None, tools=None):
    limits = limits or RunLimits(output_tokens=128)
    tools = tools or []
    lease = runtime.acquire_owner("case", limits)
    budget = RequestBudget(runtime.store, limits, clock)
    budget.owner_id, budget.owner_generation = lease.owner_id, lease.generation
    parent = runtime.execution.start(
        "planning", case_id="case", command_id="plan", runtime_generation=lease.generation
    )
    reserve_role(runtime.store, "case", parent.operation_id, lease.owner_id, lease.generation)
    builder = ContextBuilder(
        runtime.store, estimate=lambda text: len(text) // 4, input_limit=input_limit
    )
    view = builder.build_decision("case", tools=(), sources="fixture", budget_limits={})
    return RecordedProvider(
        provider,
        builder=builder,
        view=view,
        budget=budget,
        recorder=runtime.execution,
        parent=parent,
        configuration={},
        tools=tools,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "checkpoint_steps,force_no_tools,visible_tools", [(1, False, 1), (2, False, 1), (2, True, 0)]
)
async def test_checkpoint_preserves_tools_and_explicit_tool_projection_is_recorded(
    runtime, case, clock, checkpoint_steps, force_no_tools, visible_tools
):
    async def execute(*args, **kwargs):
        return AgentToolResult(content="unused")

    tool = AgentTool(
        name="inspect", label="inspect", description="Read data", parameters={}, execute_fn=execute
    )
    provider = FakeProvider([[assistant_done(AssistantMessage(content="done"))]])
    wrapped = recorded(
        runtime,
        clock,
        provider,
        limits=RunLimits(output_tokens=128, checkpoint_steps=checkpoint_steps),
        tools=[tool],
    )
    projection = await wrapped.project(
        RequestContext(system="test", messages=(UserMessage(content="Finish."),)),
        force_no_tools=force_no_tools,
    )
    _ = [
        event
        async for event in wrapped.stream_response(
            model="fake", system=projection.system, messages=list(projection.messages), tools=[tool]
        )
    ]
    assert len(provider.calls[0][3]) == visible_tools
    request = next(
        r for r in runtime.store.executions(case_id="case") if r.operation_kind == "model_request"
    )
    snapshot = runtime.store.request("case", request.request_id)
    body = runtime.store.artifacts.read(snapshot.provider_input)
    assert len(snapshot.tool_definitions) == visible_tools
    assert len(json.loads(body)["tools"]) == visible_tools


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["overflow", "artifact", "snapshot"])
async def test_context_and_storage_failure_never_call_model(
    runtime, case, clock, monkeypatch, failure
):
    provider = FakeProvider([])
    wrapped = recorded(runtime, clock, provider, input_limit=1 if failure == "overflow" else 28000)

    def fail(*args, **kwargs):
        raise OSError("injected storage failure")

    if failure == "artifact":
        monkeypatch.setattr(runtime.store.artifacts, "put", fail)
    elif failure == "snapshot":
        monkeypatch.setattr(runtime.store, "save_request", fail)
    with pytest.raises(ContextInsufficient if failure == "overflow" else OSError):
        projection = await wrapped.project(
            RequestContext(system="test", messages=(UserMessage(content="go"),))
        )
        _ = [
            event
            async for event in wrapped.stream_response(
                model="fake", system=projection.system, messages=list(projection.messages), tools=[]
            )
        ]
    assert provider.calls == []
    records = runtime.store.executions(case_id="case")
    assert not any(r.operation_kind == "model_request" for r in records)
    assert next(r for r in records if r.operation_kind == "context").status == "failed"
    assert runtime.store._connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    assert runtime.store.usage_totals("case")[1] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["cancel", "missing_terminal"])
async def test_interrupted_stream_retains_unknown_usage(runtime, case, clock, failure):
    class Interrupted:
        async def stream_response(self, **kwargs):
            if failure == "cancel":
                raise asyncio.CancelledError()
            if False:
                yield

    wrapped = recorded(runtime, clock, Interrupted())
    projection = await wrapped.project(
        RequestContext(system="test", messages=(UserMessage(content="go"),))
    )
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
        _ = [
            event
            async for event in wrapped.stream_response(
                model="fake", system=projection.system, messages=list(projection.messages), tools=[]
            )
        ]
    request = next(
        r for r in runtime.store.executions(case_id="case") if r.operation_kind == "model_request"
    )
    usage = runtime.store.request_usage("case", request.request_id)
    assert usage["state"] == "unknown" and usage["charged_tokens"] > 128
    assert request.status == ("cancelled" if failure == "cancel" else "failed")
    assert runtime.store.request("case", request.request_id).provider_input


@pytest.mark.anyio
async def test_format_repairs_are_bounded_and_charged(runtime, case, clock):
    from tau_incident.executor import OutputInvalid, RoleRunner
    from tau_incident.planner import PlanOutput

    provider = FakeProvider([[assistant_done(AssistantMessage(content="invalid"))]] * 3)
    wrapped = recorded(runtime, clock, provider)
    # Release the helper's reservation; RoleRunner owns its own role lifecycle.
    from tau_incident.store.control import release_role

    release_role(runtime.store, wrapped.parent.operation_id)
    runner = RoleRunner(
        provider=provider,
        model="fake",
        configuration={},
        builder=wrapped.builder,
        budget=wrapped.budget,
        recorder=runtime.execution,
    )
    with pytest.raises(OutputInvalid, match="structured output rejected"):
        await runner.run(
            PlanOutput, view=wrapped.view, tools=[], parent=wrapped.parent, instructions="plan"
        )
    assert len(provider.calls) == 3
    requests = [
        r for r in runtime.store.executions(case_id="case") if r.operation_kind == "model_request"
    ]
    assert len(requests) == 3
    assert all(
        runtime.store.request_usage("case", r.request_id)["charged_tokens"] > 0 for r in requests
    )
    repair_prompt = provider.calls[1][2][-1]
    assert isinstance(repair_prompt, UserMessage)
    assert "Rebuild the entire JSON object" in repair_prompt.content
    assert "exactly one complete top-level object" in repair_prompt.content
    assert runner.active is None
