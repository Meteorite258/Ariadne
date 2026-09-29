"""Lease-fenced recovery and lightweight durable wait checks, without model calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from tau_incident.events import Command, RecordWait
from tau_incident.models import ExecutionRecord, WaitCondition
from tau_incident.store.control import require_owner

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime

WaitProbe = Callable[[WaitCondition], Awaitable[bool]]


def recover(
    runtime: IncidentRuntime, owner_generation: int, *, active_parent: ExecutionRecord | None = None
) -> None:
    lease = runtime.owner
    if lease is None or lease.generation != owner_generation:
        raise ValueError("recovery requires the acquired generation")
    require_owner(runtime.store, lease.case_id, lease.owner_id, lease.generation)
    operation = runtime.execution.start(
        "recovery",
        case_id=lease.case_id,
        command_id=uuid4().hex,
        runtime_generation=lease.generation,
    )
    try:
        receipt = None
        case = runtime.get_case(lease.case_id)
        pending_usage = runtime.store._connection.execute(
            "SELECT COUNT(*) FROM request_usage "
            "WHERE case_id=? AND state='reserved' AND generation<?",
            (lease.case_id, lease.generation),
        ).fetchone()[0]
        if (
            any(
                a.status == "running" and a.runtime_generation < lease.generation
                for a in case.attempts
            )
            or pending_usage
        ):
            receipt = runtime.lifecycle(
                lease.case_id, "recover", "superseded coordinator generation"
            )
            if receipt.status != "accepted":
                raise ValueError(receipt.reason)
        # Page first, then mutate: updating cursors during pagination would revisit records.
        records: list[ExecutionRecord] = []
        cursor = 0
        while page := runtime.store.executions(
            case_id=lease.case_id, after_cursor=cursor, limit=1000
        ):
            records.extend(page)
            cursor = page[-1].cursor
        for record in records:
            if active_parent is not None and record.operation_id == active_parent.operation_id:
                continue
            if record.runtime_generation >= owner_generation or record.status != "running":
                continue
            authoritative = runtime.store.receipt(record.command_id)
            refs = tuple(
                e.evidence_id
                for e in runtime.get_case(lease.case_id).observations
                if e.source_operation_id == record.operation_id
            )
            result = "interrupted_no_terminal_record"
            status = "interrupted"
            detail = (
                "Recovered metadata at "
                + runtime.clock().isoformat()
                + "; original end time unknown."
            )
            if authoritative and record.operation_kind in {"submission", "manual_command"}:
                status, result = "succeeded", "receipt_reconciled:" + authoritative.status
            elif refs:
                status, result = "succeeded", "registered_evidence_reconciled"
                detail += " Evidence: " + ",".join(refs)
            elif record.request_id and record.operation_kind == "model_request":
                result, status = "request_result_unknown", "unknown"
                usage = runtime.store.request_usage(lease.case_id, record.request_id)
                detail += "; accounting=" + str(usage["state"])
                response = runtime.store.request_result(record.request_id)
                if response is not None:
                    result = "response_reconciled:" + str(response["stop_reason"])
                    status = (
                        "failed"
                        if response["is_error"] or response["stop_reason"] in {"error", "aborted"}
                        else "succeeded"
                    )
                    detail += "; response=" + str(response["artifact"])
            # No fabricated normal duration or completion timestamp: annotate recovery time.
            replacement = record.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "detail": detail,
                    "receipt_id": authoritative.receipt_id if authoritative else None,
                }
            )

            def replace_record(
                old: ExecutionRecord, value: ExecutionRecord = replacement
            ) -> ExecutionRecord:
                return value if old.status == "running" else old

            runtime.store.update_execution(record.operation_id, replace_record)
            runtime.execution.link(operation.operation_id, record.operation_id, "reconciled")
        runtime.execution.finish(
            operation.operation_id,
            status="succeeded",
            result="recovered",
            receipt_id=receipt.receipt_id if receipt else None,
        )
    except BaseException as exc:
        runtime.execution.finish(
            operation.operation_id, status="failed", result="recovery_failed", detail=str(exc)
        )
        raise


async def check_wait(
    runtime: IncidentRuntime,
    condition: WaitCondition,
    clock: Callable[[], datetime],
    probe: WaitProbe | None = None,
) -> bool:
    """Return whether a due condition was checked. Every check is a new bounded span."""
    now = clock()
    if condition.status != "pending" or (
        now < condition.next_check_at and now < condition.deadline
    ):
        return False
    lease = runtime.owner
    if lease is None:
        raise ValueError("wait check requires ownership")
    require_owner(runtime.store, condition.case_id, lease.owner_id, lease.generation)
    op = runtime.execution.start(
        "wait_check",
        case_id=condition.case_id,
        command_id=uuid4().hex,
        runtime_generation=lease.generation,
        task_id=condition.task_id,
    )
    if condition.operation_id:
        runtime.execution.link(op.operation_id, condition.operation_id, "next_check")
    error = None
    satisfied = False
    try:
        if now < condition.deadline:
            kind = condition.condition["kind"]
            if kind == "time":
                satisfied = now >= condition.next_check_at
            elif kind == "evidence":
                from tau_incident.context import relevant

                cutoff = condition.condition.get("after")
                after = datetime.fromisoformat(str(cutoff)) if cutoff else None
                satisfied = any(
                    relevant(e.scope, condition.scope)
                    and e.result in {"complete", "partial"}
                    and (after is None or e.collected_at > after)
                    for e in runtime.get_case(condition.case_id).observations
                )
            elif probe is not None:
                async with asyncio.timeout(10):
                    satisfied = await probe(condition)
            else:
                error = "no lightweight probe configured for source"
    except asyncio.CancelledError:
        runtime.execution.finish(
            op.operation_id,
            status="cancelled",
            result="wait_check_cancelled",
            detail="Condition remains persisted for a later check.",
        )
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    status = (
        "timed_out" if clock() >= condition.deadline else "satisfied" if satisfied else "pending"
    )
    updated = condition.model_copy(
        update={
            "version": condition.version + 1,
            "status": status,
            "next_check_at": min(
                clock() + timedelta(seconds=condition.interval_seconds), condition.deadline
            ),
            "operation_id": op.operation_id,
            "last_error": error,
        }
    )
    receipt = runtime.execute(
        Command(
            command_id=op.command_id,
            case_id=condition.case_id,
            payload=RecordWait(scope=condition.scope, wait=updated),
        ),
        parent=op,
    )
    runtime.execution.finish(
        op.operation_id,
        status="failed" if error else "succeeded",
        result=status if receipt.status == "accepted" else receipt.status,
        detail=error or receipt.reason,
        receipt_id=receipt.receipt_id,
    )
    if receipt.status != "accepted":
        raise ValueError(f"wait update rejected: {receipt.reason}")
    return True
