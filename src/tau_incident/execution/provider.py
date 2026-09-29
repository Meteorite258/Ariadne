"""Snapshot the actual neutral provider input before opening the model stream."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from tau_agent.messages import AgentMessage, Usage
from tau_agent.provider import CancellationToken, ModelProvider
from tau_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent, AssistantMessageEvent
from tau_agent.request_context import RequestContext
from tau_agent.tool_history import validate_tool_history, validate_tool_projection
from tau_agent.tools import AgentTool
from tau_agent.types import JSONValue
from tau_incident.budget import RequestBudget
from tau_incident.context import ContextBuilder, DecisionContext, RequestSelection, TaskContext
from tau_incident.execution import ExecutionRecorder
from tau_incident.models import ExecutionRecord, RequestSnapshot, Source, TaskAttempt


def tool_definitions(tools: list[AgentTool]) -> tuple[dict[str, JSONValue], ...]:
    return tuple(
        {"name": t.name, "description": t.description, "parameters": dict(t.parameters)}
        for t in tools
    )


class RecordedProvider:
    def __init__(
        self,
        provider: ModelProvider,
        *,
        builder: ContextBuilder,
        view: DecisionContext | TaskContext,
        budget: RequestBudget,
        recorder: ExecutionRecorder,
        parent: ExecutionRecord,
        configuration: dict[str, JSONValue],
        tools: list[AgentTool],
    ) -> None:
        self.provider, self.builder, self.view, self.budget = provider, builder, view, budget
        self.recorder, self.parent, self.configuration = recorder, parent, configuration
        self.tools = tools
        self.pending: tuple[RequestSelection, ExecutionRecord, list[AgentTool]] | None = None
        self.offered_tool_names: frozenset[str] = frozenset()
        self.failure: BaseException | None = None
        self.retry_of: str | None = None
        self.last_operation_id: str | None = None

    async def project(
        self, request: RequestContext, *, force_no_tools: bool = False
    ) -> RequestContext:
        if self.pending is not None:
            raise RuntimeError("unconsumed request projection")
        from uuid import uuid4

        request_id = uuid4().hex
        operation = self.recorder.start(
            "context",
            case_id=self.parent.case_id,
            command_id=self.parent.command_id,
            parent=self.parent,
            request_id=request_id,
        )
        try:
            case_id = self.parent.case_id
            if case_id is None:
                raise ValueError("model requests require a case-owned execution")
            effective_tools = [] if force_no_tools else self.tools
            projected, selection = self.builder.project_request(
                request,
                self.view,
                tool_tokens=self.builder.estimate(json.dumps(tool_definitions(effective_tools))),
            )
            validate_tool_projection(request.messages, projected.messages)
            selection = RequestSelection(
                request_id,
                selection.selection,
                selection.omissions,
                selection.memory_selection,
                selection.memory_retrieval,
            )
            self.pending = selection, operation, effective_tools
            return projected
        except BaseException as exc:
            self.failure = exc
            self.recorder.finish(
                operation.operation_id,
                status="failed",
                result="context_rejected",
                error_category="context_or_protocol",
                detail=str(exc),
            )
            raise

    async def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        if self.pending is None:
            raise RuntimeError("recorded provider requires the request context hook")
        selection, context_op, effective_tools = self.pending
        self.pending = None
        store = self.recorder.store
        attempt: TaskAttempt | None = (
            self.view.attempt if isinstance(self.view, TaskContext) else None
        )
        reserved = False
        try:
            case_id = self.parent.case_id
            if case_id is None:
                raise ValueError("model requests require a case-owned execution")
            validate_tool_history(tuple(messages))
            if tool_definitions(tools) != tool_definitions(self.tools):
                raise ValueError("worker tool authorization changed")
            if signal is not None and signal.is_cancelled():
                raise asyncio.CancelledError()
            body = json.dumps(
                {
                    "model": model,
                    "system": system,
                    "messages": [m.model_dump(mode="json") for m in messages],
                    "tools": tool_definitions(effective_tools),
                    "session_id": session_id,
                },
                ensure_ascii=False,
            )
            self.budget.reserve(
                selection.request_id,
                case_id,
                self.builder.estimate(body),
                attempt,
                role_operation_id=self.parent.operation_id,
            )
            reserved = True
            artifact = store.artifacts.put(body.encode("utf-8"), media_type="application/json")
            scope = (
                self.view.task.scope
                if isinstance(self.view, TaskContext)
                else self.view.brief.case.scope
            )
            snapshot = RequestSnapshot(
                request_id=selection.request_id,
                case_id=case_id,
                attempt_id=attempt.attempt_id if attempt else None,
                scope=scope,
                source=Source(kind="runtime", actor="recorded_provider"),
                model=model,
                configuration={
                    **self.configuration,
                    "effective_budget": store.budget_summary(case_id),
                },
                provider_input=artifact,
                tool_definitions=tool_definitions(effective_tools),
                selection=selection.selection,
                memory_selection=selection.memory_selection,
                memory_retrieval=selection.memory_retrieval,
                omissions=selection.omissions,
                runtime_generation=self.budget.owner_generation or 0,
            )
            store.save_request(snapshot)
            self.recorder.finish(
                context_op.operation_id,
                status="succeeded",
                result="snapshotted",
                artifact_ids=(artifact.artifact_id,),
                references=selection.selection,
            )
        except BaseException as exc:
            self.failure = exc
            if reserved:
                store.abandon_request(selection.request_id)
            if store.execution(context_op.operation_id).status == "running":
                self.recorder.finish(
                    context_op.operation_id,
                    status="failed",
                    result="request_not_sent",
                    error_category="context_budget_or_storage",
                    detail=str(exc),
                )
            raise
        try:
            operation = self.recorder.start(
                "model_request",
                case_id=self.parent.case_id,
                command_id=self.parent.command_id,
                parent=self.parent,
                request_id=selection.request_id,
            )
        except BaseException:
            store.abandon_request(selection.request_id)
            raise
        usage: Usage | None = None
        self.last_operation_id = operation.operation_id
        if self.retry_of is not None:
            self.recorder.link(operation.operation_id, self.retry_of, "retry_of")
            self.retry_of = None
        artifacts: list[str] = []
        terminal = False
        status = "succeeded"
        result = "completed"
        detail: str | None = None
        self.offered_tool_names = frozenset(tool.name for tool in effective_tools)
        try:
            async for event in self.provider.stream_response(
                model=model,
                system=system,
                messages=messages,
                tools=effective_tools,
                signal=signal,
                session_id=session_id,
            ):
                if terminal:
                    raise ValueError("provider emitted events after terminal response")
                if isinstance(event, (AssistantDoneEvent, AssistantErrorEvent)):
                    terminal = True
                    message = (
                        event.message if isinstance(event, AssistantDoneEvent) else event.error
                    )
                    # Tau's default empty usage cannot establish that a call was free.
                    usage = message.usage if message.usage.total_tokens > 0 else None
                    artifacts.append(
                        store.save_response(
                            selection.request_id, message, isinstance(event, AssistantErrorEvent)
                        )
                    )
                    if isinstance(event, AssistantErrorEvent) or message.stop_reason in {
                        "error",
                        "aborted",
                    }:
                        status = "cancelled" if message.stop_reason == "aborted" else "failed"
                        result, detail = message.stop_reason, message.error_message
                yield event
            if not terminal:
                raise ValueError("provider stream ended without terminal response")
        except BaseException as exc:
            self.failure = exc
            status = (
                "cancelled"
                if isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                else "failed"
            )
            result, detail = "provider_exception", str(exc)
            raise
        finally:
            self.budget.settle(
                selection.request_id,
                usage.total_tokens if usage else None,
                usage.model_dump_json() if usage else None,
            )
            from typing import cast

            from tau_incident.models import ExecutionStatus

            self.recorder.finish(
                operation.operation_id,
                status=cast(ExecutionStatus, status),
                result=result,
                detail=detail,
                error_category=None if status == "succeeded" else "model",
                artifact_ids=tuple(artifacts),
                references=selection.selection,
            )
