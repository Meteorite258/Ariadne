"""Explicit worker-only read tools; every query registers runtime evidence."""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field

from tau_agent.messages import TextContent
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken, ToolUpdateCallback
from tau_agent.types import JSONValue
from tau_incident.analysis import AnalysisRequest
from tau_incident.context import evidence_ref, relevant
from tau_incident.events import AddObservation, Command, RecordRead
from tau_incident.models import (
    ExecutionRecord,
    Model,
    ObservationInput,
    Source,
    TaskAttempt,
    TimeWindow,
)
from tau_incident.store import CommitUnknown
from tau_incident.submission import scope_contains
from tau_incident.telemetry import (
    CatalogSnapshot,
    ServiceCatalog,
    TelemetryProvider,
    TelemetryQuery,
)

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime


TOOL_NAMES = (
    "telemetry_query",
    "telemetry_capabilities",
    "service_catalog",
    "evidence_read",
    "python_analysis",
)


class EvidenceRead(Model):
    evidence_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12000, ge=1, le=24000)


class Empty(Model):
    pass


def worker_tools(
    runtime: IncidentRuntime,
    attempt: TaskAttempt,
    parent: ExecutionRecord,
    telemetry: TelemetryProvider,
    catalog: ServiceCatalog,
    fatal_errors: list[Exception] | None = None,
) -> list[AgentTool]:
    task = next(t for t in runtime.get_case(attempt.case_id).tasks if t.task_id == attempt.task_id)

    def make(name: str, schema: type[Model], description: str) -> AgentTool:
        async def execute_recorded(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            operation = runtime.execution.start(
                "tool",
                case_id=attempt.case_id,
                command_id=parent.command_id,
                parent=parent,
                tool_call_id=tool_call_id,
            )
            parsed: Model | None = None
            try:
                from tau_incident.store.control import require_owner

                lease = runtime.owner
                require_owner(
                    runtime.store,
                    attempt.case_id,
                    lease.owner_id if lease else None,
                    attempt.runtime_generation,
                )
                current = runtime.get_case(attempt.case_id)
                if current.investigation_status in {"paused", "completed"} or not any(
                    a.attempt_id == attempt.attempt_id and a.status == "running"
                    for a in current.attempts
                ):
                    raise asyncio.CancelledError()
                query_artifact = runtime.evidence.artifacts.put(
                    json.dumps({"tool": name, "arguments": dict(arguments)}).encode("utf-8"),
                    media_type="application/json",
                )
                runtime.store.update_execution(
                    operation.operation_id,
                    lambda record: record.model_copy(
                        update={"artifact_ids": (query_artifact.artifact_id,)}
                    ),
                )
                if signal is not None and signal.is_cancelled():
                    raise asyncio.CancelledError()
                parsed = schema.model_validate(dict(arguments))
                source = Source(kind="tool", actor=name, reference=operation.operation_id)
                if isinstance(parsed, EvidenceRead):
                    evidence = runtime.store.evidence(attempt.case_id, parsed.evidence_id)
                    if not relevant(evidence.scope, task.scope):
                        raise ValueError("evidence outside authorized task scope")
                    raw = runtime.store.read_evidence(attempt.case_id, parsed.evidence_id).decode(
                        "utf-8"
                    )
                    ref = evidence_ref(evidence)
                    receipt = runtime.execute(
                        Command(
                            command_id=uuid4().hex,
                            case_id=attempt.case_id,
                            payload=RecordRead(
                                scope=task.scope, attempt_id=attempt.attempt_id, reference=ref
                            ),
                        ),
                        parent=operation,
                    )
                    if receipt.status != "accepted":
                        raise ValueError(receipt.reason)
                    output = json.dumps(
                        {
                            "evidence": evidence.model_dump(mode="json"),
                            "raw_text": raw[parsed.offset : parsed.offset + parsed.limit],
                            "next_offset": parsed.offset + parsed.limit
                            if len(raw) > parsed.offset + parsed.limit
                            else None,
                        }
                    )
                    runtime.execution.finish(
                        operation.operation_id,
                        status="succeeded",
                        result="read",
                        references=(ref,),
                        artifact_ids=(evidence.artifact.artifact_id,),
                    )
                    return AgentToolResult(content=[TextContent(text=output)])
                if isinstance(parsed, AnalysisRequest):
                    if runtime.analysis is None or runtime.analysis_limits is None:
                        raise ValueError("isolated analysis is not configured")
                    inputs = tuple(
                        runtime.store.evidence(attempt.case_id, eid) for eid in parsed.evidence_ids
                    )
                    if any(not scope_contains(task.scope, e.scope) for e in inputs):
                        raise ValueError("analysis evidence exceeds task scope")
                    refs = tuple(evidence_ref(e) for e in inputs)
                    for ref in refs:
                        read_receipt = runtime.execute(
                            Command(
                                command_id=uuid4().hex,
                                case_id=attempt.case_id,
                                payload=RecordRead(
                                    scope=task.scope, attempt_id=attempt.attempt_id, reference=ref
                                ),
                            ),
                            parent=operation,
                        )
                        if read_receipt.status != "accepted":
                            raise ValueError(read_receipt.reason)
                    limits = runtime.analysis_limits
                    script_ref = runtime.evidence.artifacts.put(
                        parsed.script.encode(), media_type="text/x-python"
                    )
                    runtime.store.update_execution(
                        operation.operation_id,
                        lambda record: record.model_copy(
                            update={
                                "artifact_ids": (*record.artifact_ids, script_ref.artifact_id),
                                "references": refs,
                            }
                        ),
                    )
                    running = asyncio.current_task()

                    async def watch_cancel() -> None:
                        while signal is not None and not signal.is_cancelled():
                            await asyncio.sleep(0.05)
                        if signal is not None and running is not None:
                            running.cancel()

                    watcher = asyncio.create_task(watch_cancel())
                    try:
                        analysis = await runtime.analysis.run(
                            script_ref, parsed.evidence_ids, limits, case_id=attempt.case_id
                        )
                    finally:
                        watcher.cancel()
                        await asyncio.gather(watcher, return_exceptions=True)
                    result_ref = runtime.evidence.artifacts.put(
                        analysis.model_dump_json().encode(), media_type="application/json"
                    )
                    runtime.store.update_execution(
                        operation.operation_id,
                        lambda record: record.model_copy(
                            update={
                                "artifact_ids": (
                                    *record.artifact_ids,
                                    result_ref.artifact_id,
                                    *(a.artifact_id for a in analysis.outputs.values()),
                                )
                            }
                        ),
                    )
                    derived: dict[str, JSONValue] = {}
                    for filename, output_ref in analysis.outputs.items():
                        content = runtime.evidence.artifacts.read(output_ref)
                        output_source = Source(
                            kind="tool",
                            actor="python_analysis_output",
                            reference=operation.operation_id,
                        )
                        output_observation = ObservationInput(
                            summary="Python output: " + filename,
                            scope=task.scope,
                            source=output_source,
                            raw_text=json.dumps(
                                {
                                    "filename": filename,
                                    "encoding": "base64",
                                    "content": base64.b64encode(content).decode(),
                                    "artifact": output_ref.model_dump(mode="json"),
                                }
                            ),
                            result="complete" if analysis.status == "succeeded" else "failed",
                            inputs=refs,
                            method="Python script " + script_ref.artifact_id,
                            source_revision=analysis.image,
                            actual_query={"script": script_ref.artifact_id, "output": filename},
                        )
                        output_receipt = runtime.execute(
                            Command(
                                command_id=uuid4().hex,
                                case_id=attempt.case_id,
                                payload=AddObservation(
                                    observation=output_observation, attempt_id=attempt.attempt_id
                                ),
                            ),
                            parent=operation,
                        )
                        if output_receipt.status != "accepted":
                            raise ValueError(output_receipt.reason)
                        output_evidence = next(
                            e
                            for e in runtime.get_case(attempt.case_id).observations
                            if e.source == output_source
                            and e.actual_query.get("output") == filename
                        )
                        derived[filename] = evidence_ref(output_evidence).model_dump(mode="json")
                    observation = ObservationInput(
                        summary=f"Isolated Python analysis: {analysis.status}",
                        raw_text=json.dumps(
                            {
                                "analysis": analysis.model_dump(mode="json"),
                                "derived_evidence": derived,
                            }
                        ),
                        scope=task.scope,
                        source=source,
                        actual_query={
                            "script": script_ref.artifact_id,
                            "image": analysis.image,
                            "limits": limits.model_dump(mode="json"),
                        },
                        result="complete" if analysis.status == "succeeded" else "failed",
                        truncated=analysis.truncated,
                        inputs=refs,
                        method="Isolated Python script " + script_ref.artifact_id,
                        completeness_note=analysis.reason,
                        source_revision=analysis.image,
                    )
                elif isinstance(parsed, TelemetryQuery):
                    if not scope_contains(task.scope, parsed.scope):
                        raise ValueError("query exceeds task scope")
                    result = await telemetry.query(parsed)
                    observation = ObservationInput(
                        summary=f"{parsed.kind} query: {result.result}; {len(result.rows)} rows",
                        raw_text=result.model_dump_json(),
                        scope=parsed.scope,
                        source=source,
                        actual_query={
                            "query": parsed.model_dump(mode="json"),
                            "backend": result.actual_query,
                        },
                        data_time=TimeWindow(
                            start=min((r.data_at for r in result.rows), default=None),
                            end=max((r.data_at for r in result.rows), default=None),
                        ),
                        available_at=max((r.available_at for r in result.rows), default=None),
                        collected_at=result.collected_at,
                        actual_coverage=result.actual_coverage,
                        result=result.result,
                        sampled=result.sampled,
                        truncated=result.truncated,
                        completeness_note=result.note + "; " + result.source,
                        units=",".join(sorted({r.units for r in result.rows if r.units})) or None,
                    )
                else:
                    value = (
                        (getattr(telemetry, "describe", telemetry.capabilities)())
                        if name == "telemetry_capabilities"
                        else await catalog.snapshot(task.scope)
                    )
                    raw_value = value.model_dump(mode="json") if isinstance(value, Model) else value
                    snapshots = getattr(catalog, "snapshots", None)
                    if name == "service_catalog" and snapshots is not None:
                        raw_value = {
                            "selected": raw_value,
                            "versions": [
                                item.model_dump(mode="json") for item in await snapshots(task.scope)
                            ],
                        }
                    observation = ObservationInput(
                        summary=f"{name}: read-only catalog snapshot",
                        raw_text=json.dumps(raw_value, ensure_ascii=False),
                        scope=task.scope,
                        source=source,
                        actual_query={
                            "operation": name,
                            "scope": task.scope.model_dump(mode="json"),
                        },
                        result="no_match" if value is None else "complete",
                        completeness_note="Catalog metadata; telemetry coverage remains unknown.",
                        source_revision=value.version
                        if isinstance(value, CatalogSnapshot)
                        else None,
                        available_at=value.available_at
                        if isinstance(value, CatalogSnapshot)
                        else None,
                    )
                receipt = runtime.execute(
                    Command(
                        command_id=uuid4().hex,
                        case_id=attempt.case_id,
                        payload=AddObservation(
                            observation=observation, attempt_id=attempt.attempt_id
                        ),
                    ),
                    parent=operation,
                )
                if receipt.status != "accepted":
                    raise ValueError(receipt.reason)
                evidence = next(
                    e for e in runtime.get_case(attempt.case_id).observations if e.source == source
                )
                from tau_incident.information import evidence_signature

                signature = evidence_signature(runtime.store, evidence)
                repeated = any(
                    old.evidence_id != evidence.evidence_id
                    and old.actual_query == evidence.actual_query
                    and evidence_signature(runtime.store, old) == signature
                    for old in runtime.get_case(attempt.case_id).observations
                )
                ref = evidence_ref(evidence)
                runtime.execution.finish(
                    operation.operation_id,
                    status="failed" if evidence.result == "failed" else "succeeded",
                    result=evidence.result,
                    references=(ref,),
                    artifact_ids=(evidence.artifact.artifact_id,),
                    receipt_id=receipt.receipt_id,
                )
                return AgentToolResult(
                    content=[
                        TextContent(
                            text=json.dumps(
                                {
                                    "reference": ref.model_dump(mode="json"),
                                    "evidence": evidence.model_dump(mode="json"),
                                    "data": observation.raw_text[:24000],
                                    "data_truncated": len(observation.raw_text) > 24000,
                                    "information": "No new information: same query scope, "
                                    "source version, coverage and content. "
                                    "Consider a different check or a wait."
                                    if repeated
                                    else "New recorded information",
                                },
                                ensure_ascii=False,
                            )
                        )
                    ]
                )
            except BaseException as exc:
                cancelled_result = getattr(exc, "result", None)
                if cancelled_result is not None and isinstance(parsed, AnalysisRequest):
                    artifact = runtime.evidence.artifacts.put(
                        cancelled_result.model_dump_json().encode(), media_type="application/json"
                    )
                    runtime.store.update_execution(
                        operation.operation_id,
                        lambda record: record.model_copy(
                            update={"artifact_ids": (*record.artifact_ids, artifact.artifact_id)}
                        ),
                    )
                runtime.execution.finish(
                    operation.operation_id,
                    status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                    result="tool_failed",
                    error_category="telemetry_or_validation",
                    detail=str(exc),
                )
                raise

        async def execute(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            from tau_incident.coordinator import LocalRecordingError

            try:
                return await execute_recorded(tool_call_id, arguments, signal, on_update)
            except (LocalRecordingError, CommitUnknown, sqlite3.Error, OSError) as exc:
                if fatal_errors is not None:
                    fatal_errors.append(exc)
                raise

        return AgentTool(
            name=name,
            label=name,
            description=description,
            parameters=schema.model_json_schema(),
            execute_fn=execute,
            execution_mode="sequential",
        )

    schemas: dict[str, type[Model]] = {
        "telemetry_query": TelemetryQuery,
        "evidence_read": EvidenceRead,
        "telemetry_capabilities": Empty,
        "service_catalog": Empty,
        "python_analysis": AnalysisRequest,
    }
    if not set(task.allowed_tools) <= set(TOOL_NAMES):
        raise ValueError("task contains an unknown tool capability")
    descriptions = {
        "telemetry_query": (
            "Query read-only incident rows. Narrow the scope to one entity and use small "
            "pages or a contains filter when investigating a specific failure. A partial "
            "page is not complete coverage; use next_offset to continue."
        ),
        "python_analysis": (
            "Run Python 3 in an isolated container. The script can read "
            "/inputs/manifest.json, a JSON map from each requested evidence_id to its "
            "read-only input path. Use load_evidence(evidence_id) and "
            "describe_evidence(evidence_id) "
            "helpers to decode data and inspect bounded schema and row samples. "
            "Write derived JSON files under /outputs "
            "or print a bounded summary to stdout. Only requested evidence IDs are staged."
        ),
        "evidence_read": (
            "Read an authorized evidence artifact. raw_text contains the requested "
            "offset/limit slice of the original data; next_offset indicates more data. "
            "Use small slices when large results exceed the request context."
        ),
    }
    from tau_incident.context_read import context_read_tool
    from tau_incident.progress import progress_tool

    return [
        progress_tool(runtime, attempt, parent, fatal_errors),
        context_read_tool(runtime, attempt, parent, fatal_errors),
        *[
            make(
                name,
                schemas[name],
                descriptions.get(
                    name,
                    "Read authorized incident data; preserve evidence references and coverage.",
                ),
            )
            for name in task.allowed_tools
            if name != "python_analysis" or runtime.analysis is not None
        ],
    ]
