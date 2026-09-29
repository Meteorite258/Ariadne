"""Local operation recording. A completed span is never a substitute for a receipt."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from time import monotonic
from uuid import uuid4

from tau_incident.models import (
    ExecutionLink,
    ExecutionRecord,
    ExecutionStatus,
    OperationKind,
    VersionRef,
)
from tau_incident.store import CaseStore


class ExecutionRecorder:
    def __init__(self, store: CaseStore, *, clock: Callable[[], datetime]) -> None:
        self.store = store
        self.clock = clock
        self._starts: dict[str, float] = {}
        self.on_finished: Callable[[ExecutionRecord], None] | None = None

    def start(
        self,
        kind: OperationKind,
        *,
        case_id: str | None,
        command_id: str,
        parent: ExecutionRecord | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        request_id: str | None = None,
        tool_call_id: str | None = None,
        trace_id: str | None = None,
        runtime_generation: int = 0,
    ) -> ExecutionRecord:
        if parent is not None and parent.case_id != case_id:
            raise ValueError("parent operation must belong to this case")
        record = ExecutionRecord(
            operation_id=uuid4().hex,
            trace_id=parent.trace_id if parent else trace_id or uuid4().hex,
            span_id=uuid4().hex[:16],
            parent_operation_id=parent.operation_id if parent else None,
            parent_span_id=parent.span_id if parent else None,
            case_id=case_id,
            task_id=task_id or (parent.task_id if parent else None),
            attempt_id=attempt_id or (parent.attempt_id if parent else None),
            request_id=request_id,
            tool_call_id=tool_call_id,
            command_id=command_id,
            operation_kind=kind,
            started_at=self.clock(),
            runtime_generation=parent.runtime_generation if parent else runtime_generation,
        )
        started = monotonic()
        saved = self.store.add_execution(record)
        self._starts[record.operation_id] = started
        return saved

    def finish(
        self,
        operation_id: str,
        *,
        status: ExecutionStatus,
        result: str,
        error_category: str | None = None,
        detail: str | None = None,
        references: tuple[VersionRef, ...] = (),
        event_ids: tuple[str, ...] = (),
        artifact_ids: tuple[str, ...] = (),
        receipt_id: str | None = None,
    ) -> ExecutionRecord:
        if status == "running":
            raise ValueError("finish requires a terminal operation status")
        started = self._starts.get(operation_id)
        duration = None if started is None else max(0.0, (monotonic() - started) * 1000)

        def update(record: ExecutionRecord) -> ExecutionRecord:
            if record.status != "running":
                raise ValueError("operation already finished")
            data = record.model_dump()
            data.update(
                finished_at=self.clock(),
                duration_ms=duration,
                status=status,
                result=result,
                error_category=error_category,
                detail=detail,
                references=tuple(dict.fromkeys((*record.references, *references))),
                event_ids=event_ids,
                artifact_ids=tuple(dict.fromkeys((*record.artifact_ids, *artifact_ids))),
                receipt_id=receipt_id,
            )
            return ExecutionRecord.model_validate(data)

        saved = self.store.update_execution(operation_id, update)
        self._starts.pop(operation_id, None)
        if self.on_finished is not None:
            self.on_finished(saved)
        return saved

    def link(self, operation_id: str, target_operation_id: str, relation: str) -> ExecutionRecord:
        target = self.store.execution(target_operation_id)
        link = ExecutionLink(operation_id=target_operation_id, relation=relation)

        def update(record: ExecutionRecord) -> ExecutionRecord:
            if target.case_id != record.case_id or target.operation_id == record.operation_id:
                raise ValueError("links must target a different operation in the same case")
            if link in record.links:
                return record
            return record.model_copy(update={"links": (*record.links, link)})

        return self.store.update_execution(operation_id, update)
