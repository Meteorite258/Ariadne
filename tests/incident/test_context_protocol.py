import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from tau_agent.loop import run_agent_loop
from tau_agent.request_context import RequestContext
from tau_agent.tool_history import MessageProtocolError, validate_tool_projection
from tau_ai import FakeProvider


def tool_group():
    return (
        AssistantMessage(
            content=[
                ToolCall(id="a", name="query", arguments={}),
                ToolCall(id="b", name="read", arguments={}),
            ]
        ),
        ToolResultMessage(tool_call_id="a", tool_name="query", content="failed", is_error=True),
        ToolResultMessage(tool_call_id="b", tool_name="read", content="data"),
    )


@pytest.mark.parametrize("mutation", ["split", "missing", "error", "identity"])
def test_projection_preserves_atomic_groups_and_error_identity(mutation):
    group = tool_group()
    if mutation == "split":
        projected = (AssistantMessage(content=[group[0].tool_calls[0]]), group[1])
    elif mutation == "missing":
        projected = group[:2]
    elif mutation == "error":
        projected = (group[0], group[1].model_copy(update={"is_error": False}), group[2])
    else:
        projected = (group[0], group[1].model_copy(update={"tool_name": "other"}), group[2])
    with pytest.raises(MessageProtocolError):
        validate_tool_projection(group, projected)
    validate_tool_projection(group, ())
    validate_tool_projection(group, group)


@pytest.mark.anyio
async def test_request_hook_mutates_only_copy():
    original = UserMessage(content=[TextContent(text="original")])
    messages = [original]
    provider = FakeProvider([[assistant_done(AssistantMessage(content="done"))]])

    async def project(context):
        context.messages[0].content[0].text = "projected"
        return RequestContext(system="projected system", messages=context.messages)

    _ = [
        event
        async for event in run_agent_loop(
            provider=provider,
            model="fake",
            system="sys",
            messages=messages,
            tools=[],
            request_context_hook=project,
        )
    ]
    assert original.text == "original"
    assert provider.calls[0][2][0].text == "projected"
    assert provider.calls[0][1] == "projected system"


@pytest.mark.anyio
async def test_invalid_projection_does_not_call_provider():
    messages = [UserMessage(content="start"), *tool_group()]
    provider = FakeProvider([])

    async def project(context):
        return RequestContext(system=context.system, messages=context.messages[:-1])

    with pytest.raises(MessageProtocolError, match="missing tool results"):
        _ = [
            event
            async for event in run_agent_loop(
                provider=provider,
                model="fake",
                system="sys",
                messages=messages,
                tools=[],
                request_context_hook=project,
            )
        ]
    assert provider.calls == []
    assert messages[-1].tool_call_id == "b"
