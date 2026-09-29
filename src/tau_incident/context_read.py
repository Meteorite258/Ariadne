"""Bounded retrieval of durable domain records omitted from task projections."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import Field

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from tau_agent.types import JSONValue
from tau_incident.events import Command, RecordRead
from tau_incident.models import ExecutionRecord, IncidentCase, Model, TaskAttempt, VersionRef

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime


def lookup(case: IncidentCase, reference: VersionRef) -> Model:
    collections = {
        "task": (case.tasks, "task_id", "contract_version"),
        "claim": (case.claims, "claim_id", "version"),
        "finding": (case.findings, "finding_id", None),
        "review": (case.review_issues, "review_id", "version"),
        "report": (case.reports, "report_id", "version"),
        "evidence": (case.observations, "evidence_id", "version"),
    }
    if reference.case_id != case.case_id or reference.kind not in collections:
        raise ValueError("unsupported domain reference")
    values, identity, version = collections[reference.kind]
    result = next(
        (
            value
            for value in values
            if getattr(value, identity) == reference.object_id
            and (getattr(value, version) if version else 1) == reference.version
        ),
        None,
    )
    if result is None:
        raise ValueError("domain reference is absent or stale; use the current version")
    return result


class ContextRead(Model):
    reference: VersionRef | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=1, le=12000)


def context_read_tool(
    runtime: IncidentRuntime,
    attempt: TaskAttempt,
    parent: ExecutionRecord,
    fatal_errors: list[Exception] | None,
) -> AgentTool:
    async def execute(
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        from tau_incident.coordinator import LocalRecordingError
        from tau_incident.store import CommitUnknown

        operation = runtime.execution.start(
            "tool",
            case_id=attempt.case_id,
            command_id=parent.command_id,
            parent=parent,
            tool_call_id=tool_call_id,
        )
        try:
            parsed = ContextRead.model_validate(arguments)
            current = runtime.get_case(attempt.case_id)
            if parsed.reference is None:
                from tau_incident.context import ContextBuilder, relevant
                from tau_incident.models import Scope

                index = []
                for reference in ContextBuilder._refs(current):
                    if reference.kind not in {
                        "task",
                        "claim",
                        "finding",
                        "review",
                        "report",
                        "evidence",
                    }:
                        continue
                    value = lookup(current, reference)
                    data = value.model_dump(mode="json")
                    if reference not in attempt.basis and not relevant(
                        Scope.model_validate(data["scope"]), attempt.scope
                    ):
                        continue
                    summary = next(
                        (
                            data[k]
                            for k in ("summary", "statement", "goal", "gap", "explanation")
                            if data.get(k)
                        ),
                        "",
                    )
                    index.append(
                        {
                            "reference": reference.model_dump(mode="json"),
                            "summary": str(summary)[:200],
                        }
                    )
                raw = json.dumps(index, ensure_ascii=False)
            else:
                value = lookup(current, parsed.reference)
                receipt = runtime.execute(
                    Command(
                        command_id=f"context-read:{attempt.attempt_id}:{tool_call_id}",
                        case_id=attempt.case_id,
                        payload=RecordRead(
                            scope=attempt.scope,
                            attempt_id=attempt.attempt_id,
                            reference=parsed.reference,
                        ),
                    ),
                    parent=operation,
                )
                if receipt.status != "accepted":
                    raise ValueError(receipt.reason)
                raw = value.model_dump_json()
            end = min(len(raw), parsed.offset + parsed.limit)
            body = json.dumps(
                {
                    "reference": parsed.reference.model_dump(mode="json")
                    if parsed.reference
                    else None,
                    "text": raw[parsed.offset : end],
                    "next_offset": end if end < len(raw) else None,
                    "total_characters": len(raw),
                }
            )
            artifact = runtime.store.artifacts.put(body.encode(), media_type="application/json")
            runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result="read",
                references=(parsed.reference,) if parsed.reference else (),
                artifact_ids=(artifact.artifact_id,),
            )
            return AgentToolResult(content=[TextContent(text=body)])
        except BaseException as exc:
            if (
                isinstance(exc, (LocalRecordingError, CommitUnknown, sqlite3.Error, OSError))
                and fatal_errors is not None
            ):
                fatal_errors.append(exc)
            runtime.execution.finish(
                operation.operation_id, status="failed", result="read_failed", detail=str(exc)
            )
            raise

    return AgentTool(
        name="context_read",
        label="Read work record",
        parameters=ContextRead.model_json_schema(),
        description=(
            "Retrieve a current task, claim, finding, review or report by its "
            "exact supplied reference. Returns bounded JSON text with "
            "next_offset. Raw evidence is available with evidence_read."
            " Omit reference to browse the paginated index of authorized case materials. "
            "Index entries are discovery pointers; fetch a record before relying on it."
        ),
        execute_fn=execute,
        execution_mode="sequential",
    )
