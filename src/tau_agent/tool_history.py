"""Provider-safe repair of malformed tool-call message history."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)

_INTERRUPTED_TOOL_RESULT = "Tool call interrupted by user"


class MessageProtocolError(ValueError):
    """A projected transcript is invalid; it must not be silently repaired."""


def validate_tool_history(messages: tuple[AgentMessage, ...]) -> None:
    pending: dict[str, str] = {}
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolResultMessage):
            if pending.get(message.tool_call_id) != message.tool_name:
                raise MessageProtocolError("orphan, duplicate or mismatched tool result")
            del pending[message.tool_call_id]
            continue
        if pending:
            raise MessageProtocolError("tool result group is incomplete or non-adjacent")
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if not call.id or call.id in seen:
                    raise MessageProtocolError("tool call IDs must be nonempty and unique")
                seen.add(call.id)
                pending[call.id] = call.name
    if pending:
        raise MessageProtocolError("missing tool results")


def validate_tool_projection(
    original: tuple[AgentMessage, ...], projected: tuple[AgentMessage, ...]
) -> None:
    """Projection may omit a whole call group, but cannot split or forge one."""
    validate_tool_history(projected)
    groups = {
        call.id: message
        for message in original
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
    }
    results = {
        message.tool_call_id: message
        for message in original
        if isinstance(message, ToolResultMessage)
    }
    for message in projected:
        if isinstance(message, AssistantMessage) and message.tool_calls:
            first = message.tool_calls[0]
            source = groups.get(first.id)
            if source is None or message.tool_calls != source.tool_calls:
                raise MessageProtocolError("projection split or altered a tool-call group")
        if isinstance(message, ToolResultMessage):
            result = results.get(message.tool_call_id)
            if result is None or (result.tool_name, result.is_error) != (
                message.tool_name,
                message.is_error,
            ):
                raise MessageProtocolError(
                    "projection altered tool result identity or error status"
                )


@dataclass(frozen=True, slots=True)
class ToolHistoryRepair:
    """A provider-safe transcript plus a summary of deterministic repairs."""

    messages: tuple[AgentMessage, ...]
    changed: bool = False
    synthesized_results: int = 0
    dropped_orphan_results: int = 0
    dropped_duplicate_results: int = 0
    reordered_results: int = 0

    def diagnostic_data(self) -> dict[str, int]:
        """Return JSON-safe counters for durable session diagnostics."""
        return {
            "synthesizedResults": self.synthesized_results,
            "droppedOrphanResults": self.dropped_orphan_results,
            "droppedDuplicateResults": self.dropped_duplicate_results,
            "reorderedResults": self.reordered_results,
        }


def repair_tool_history(messages: tuple[AgentMessage, ...]) -> ToolHistoryRepair:
    """Return history where every tool call has exactly one adjacent result.

    Existing result messages are moved beside their calls. Missing results get a
    deterministic interruption error. Results with no call are omitted because
    a missing call's arguments cannot be reconstructed safely. When duplicate
    results exist, a real result is preferred over Tau's synthetic interruption.
    """
    call_occurrences: list[tuple[tuple[int, int], ToolCall, int]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        for call_offset, call in enumerate(message.tool_calls, start=1):
            call_occurrences.append(
                ((message_index, call_offset), call, message_index + call_offset)
            )

    results_by_id: dict[str, list[tuple[int, ToolResultMessage]]] = defaultdict(list)
    for message_index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            results_by_id[message.tool_call_id].append((message_index, message))

    selected_results: dict[tuple[int, int], tuple[int | None, ToolResultMessage]] = {}
    used_result_positions: set[int] = set()

    # Reserve already-adjacent pairs first. This keeps valid repeated IDs paired
    # with their own turn rather than letting an earlier occurrence consume them.
    for occurrence, call, expected_position in call_occurrences:
        if expected_position >= len(messages):
            continue
        candidate = messages[expected_position]
        if (
            isinstance(candidate, ToolResultMessage)
            and candidate.tool_call_id == call.id
            and expected_position not in used_result_positions
        ):
            selected_results[occurrence] = (expected_position, candidate)
            used_result_positions.add(expected_position)

    synthesized_results = 0
    for occurrence, call, _expected_position in call_occurrences:
        if occurrence in selected_results:
            continue
        candidates = [
            candidate
            for candidate in results_by_id.get(call.id, [])
            if candidate[0] not in used_result_positions
        ]
        if candidates:
            after_call = [candidate for candidate in candidates if candidate[0] > occurrence[0]]
            candidate_pool = after_call or candidates
            selected = next(
                (
                    candidate
                    for candidate in candidate_pool
                    if not _is_interruption_result(candidate[1])
                ),
                candidate_pool[0],
            )
            selected_results[occurrence] = selected
            used_result_positions.add(selected[0])
            continue
        selected_results[occurrence] = (
            None,
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text=_INTERRUPTED_TOOL_RESULT)],
                is_error=True,
            ),
        )
        synthesized_results += 1

    # If every call occurrence is already paired and a real extra result remains,
    # prefer it over a selected synthetic interruption for the same ID.
    for occurrence, call, _expected_position in call_occurrences:
        selected_position, selected_result = selected_results[occurrence]
        if selected_position is None or not _is_interruption_result(selected_result):
            continue
        replacement = next(
            (
                candidate
                for candidate in results_by_id.get(call.id, [])
                if candidate[0] not in used_result_positions
                and not _is_interruption_result(candidate[1])
            ),
            None,
        )
        if replacement is None:
            continue
        used_result_positions.remove(selected_position)
        used_result_positions.add(replacement[0])
        selected_results[occurrence] = replacement

    repaired: list[AgentMessage] = []
    reordered_results = 0
    for message_index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            continue
        repaired.append(message)
        if not isinstance(message, AssistantMessage):
            continue
        for call_offset, _call in enumerate(message.tool_calls, start=1):
            result_position, result = selected_results[(message_index, call_offset)]
            repaired.append(result)
            if result_position is not None and result_position != message_index + call_offset:
                reordered_results += 1

    call_ids = {call.id for _occurrence, call, _expected in call_occurrences}
    unused_results = [
        result
        for results in results_by_id.values()
        for position, result in results
        if position not in used_result_positions
    ]
    orphan_results = sum(result.tool_call_id not in call_ids for result in unused_results)
    dropped_duplicate_results = sum(result.tool_call_id in call_ids for result in unused_results)
    repaired_messages = tuple(repaired)
    return ToolHistoryRepair(
        messages=repaired_messages,
        changed=repaired_messages != messages,
        synthesized_results=synthesized_results,
        dropped_orphan_results=orphan_results,
        dropped_duplicate_results=dropped_duplicate_results,
        reordered_results=reordered_results,
    )


def _is_interruption_result(message: ToolResultMessage) -> bool:
    return message.is_error and message.text == _INTERRUPTED_TOOL_RESULT
