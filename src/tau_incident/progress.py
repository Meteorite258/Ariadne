"""Persist nonterminal task work through the same fenced command boundary as findings."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import Field

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from tau_agent.types import JSONValue
from tau_incident.events import Command, UpdateTaskProgress
from tau_incident.models import (
    ExecutionRecord,
    IncidentCase,
    TaskAttempt,
    TaskProgress,
    WorkContent,
)

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime
    from tau_incident.store import CaseStore


def validate_work_facts(
    store: CaseStore, case: IncidentCase, attempt: TaskAttempt, work: WorkContent
) -> str | None:
    """Only execution records and authorized immutable artifacts can establish performed work."""
    from tau_incident.context import evidence_ref

    rows = store._connection.execute(
        "SELECT body FROM executions WHERE case_id=? AND attempt_id=?",
        (case.case_id, attempt.attempt_id),
    ).fetchall()
    records = [ExecutionRecord.model_validate_json(row[0]) for row in rows]
    completed = {
        r.operation_id for r in records if r.status == "succeeded" and r.operation_kind == "tool"
    }
    if not set(work.checked_operations) <= completed:
        return "work cannot invent completed checks"
    allowed = set(attempt.basis) | set(attempt.reads)
    artifacts = {identity for r in records for identity in r.artifact_ids}
    artifacts.update(
        e.artifact.artifact_id
        for e in case.observations
        if e.attempt_id == attempt.attempt_id or evidence_ref(e) in allowed
    )
    if any(a.artifact_id not in artifacts for a in work.artifacts):
        return "work cannot invent script or output artifacts"
    for artifact in work.artifacts:
        store.artifacts.read(artifact)
    if any(r.kind != "evidence" for r in (*work.observations, *work.counterevidence)):
        return "observations and counterevidence must reference evidence"
    if isinstance(work, TaskProgress) and work.execution_cursor != max(
        (r.cursor for r in records if r.operation_kind == "tool" and r.status != "running"),
        default=0,
    ):
        return "progress execution cursor must match recorded facts"
    return None


class ProgressInput(WorkContent):
    expected_progress_version: int = Field(ge=0)


def progress_tool(
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
        import sqlite3

        from tau_incident.coordinator import LocalRecordingError
        from tau_incident.store import CommitUnknown

        try:
            parsed = ProgressInput.model_validate(arguments)
            records = runtime.store._connection.execute(
                (
                    "SELECT body FROM executions WHERE case_id=? AND attempt_id=? AND "
                    "operation_kind='tool'"
                ),
                (attempt.case_id, attempt.attempt_id),
            ).fetchall()
            progress = TaskProgress(
                **parsed.model_dump(exclude={"expected_progress_version"}),
                version=parsed.expected_progress_version + 1,
                attempt_id=attempt.attempt_id,
                execution_cursor=max(
                    (
                        record.cursor
                        for row in records
                        if (record := ExecutionRecord.model_validate_json(row[0])).status
                        != "running"
                    ),
                    default=0,
                ),
            )
            receipt = runtime.execute(
                Command(
                    command_id=f"progress:{attempt.attempt_id}:{tool_call_id}",
                    case_id=attempt.case_id,
                    payload=UpdateTaskProgress(
                        scope=attempt.scope,
                        task_id=attempt.task_id,
                        contract_version=attempt.contract_version,
                        expected_progress_version=parsed.expected_progress_version,
                        attempt_id=attempt.attempt_id,
                        execution_token=attempt.execution_token,
                        progress=progress,
                    ),
                ),
                parent=parent,
            )
            return AgentToolResult(content=[TextContent(text=receipt.model_dump_json())])
        except (LocalRecordingError, CommitUnknown, sqlite3.Error, OSError) as exc:
            if fatal_errors is not None:
                fatal_errors.append(exc)
            raise

    return AgentTool(
        name="save_progress",
        label="Save progress",
        description=(
            "Save semantic task work without finishing the attempt. Use the "
            "current progress version; cite only supplied references and "
            "recorded successful operation IDs. A receipt acknowledges "
            "persistence."
        ),
        parameters=ProgressInput.model_json_schema(),
        execute_fn=execute,
        execution_mode="sequential",
    )
