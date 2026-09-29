import json

import httpx
import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.planner import PlanOutput

from .test_investigation import runner


def role_setup(runtime, clock, provider, limits):
    lease = runtime.acquire_owner("case", limits)
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    view = role.builder.build_decision("case", tools=(), sources="fixture", budget_limits={})
    parent = runtime.execution.start(
        "planning", case_id="case", command_id="test-role", runtime_generation=lease.generation
    )
    return role, view, parent


@pytest.mark.anyio
@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_transport_retries_have_new_ids_links_and_unknown_usage(
    runtime, case, clock, failures
):
    class Interrupted(FakeProvider):
        attempts = 0

        async def stream_response(self, **kwargs):
            self.attempts += 1
            if self.attempts <= failures:
                raise httpx.ConnectError("temporary network failure")
            async for event in super().stream_response(**kwargs):
                yield event

    provider = Interrupted(
        [
            [
                assistant_done(
                    AssistantMessage(content=json.dumps({"choice": "pause", "reason": "done"}))
                )
            ]
        ]
    )
    role, view, parent = role_setup(runtime, clock, provider, RunLimits(output_tokens=128))
    if failures == 3:
        with pytest.raises(httpx.ConnectError):
            await role.run(PlanOutput, view=view, tools=[], parent=parent, instructions="plan")
    else:
        result = await role.run(PlanOutput, view=view, tools=[], parent=parent, instructions="plan")
        assert result.choice == "pause"
    requests = runtime.store.executions(case_id="case", operation_kind="model_request")
    assert len(requests) == min(failures + 1, 3)
    assert len({r.request_id for r in requests}) == len(requests)
    for index, record in enumerate(requests):
        assert runtime.store.request_usage("case", record.request_id)["charged_tokens"] > 0
        if index:
            assert any(
                link.relation == "retry_of"
                and link.operation_id == requests[index - 1].operation_id
                for link in record.links
            )
    assert all(
        runtime.store.request_usage("case", r.request_id)["state"] == "unknown"
        for r in requests[:failures]
    )


@pytest.mark.anyio
async def test_truncated_output_enters_bounded_format_repair(runtime, case, clock):
    provider = FakeProvider(
        [
            [assistant_done(AssistantMessage(content='{"choice":'), finish_reason="length")],
            [
                assistant_done(
                    AssistantMessage(
                        content='{"choice":"pause","reason":"complete repaired object"}'
                    )
                )
            ],
        ]
    )
    role, view, parent = role_setup(runtime, clock, provider, RunLimits(format_repairs=1))
    result = await role.run(PlanOutput, view=view, tools=[], parent=parent, instructions="plan")
    assert result.choice == "pause"
    assert len(provider.calls) == 2
    assert "truncated" in provider.calls[1][2][-1].content
    assert runtime.store.usage_totals("case")[0] == 2
