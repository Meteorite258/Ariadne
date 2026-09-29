"""Case-owned coordination and the shared idempotent domain-command boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from tau_incident.events import (
    AddObservation,
    Command,
    DomainEvent,
    FinishAttempt,
    Receipt,
    RecordRead,
    RecordReview,
    RecordReviews,
    SubmitFinding,
    UpdateTaskProgress,
)
from tau_incident.evidence import EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.models import (
    ExecutionRecord,
    ExecutionStatus,
    IncidentCase,
    Observation,
    OwnerLease,
    TaskAttempt,
    VersionRef,
)
from tau_incident.reporting import CaseBrief, ProgressReport, case_brief, progress_report
from tau_incident.store import CaseStore, CommitUnknown, IdempotencyConflict

if TYPE_CHECKING:
    import asyncio

    from tau_incident.analysis import AnalysisExecutor, AnalysisLimits
    from tau_incident.budget import RunLimits
    from tau_incident.executor import RoleRunner
    from tau_incident.investigation import RunnerFactory, RunResult
    from tau_incident.recovery import WaitProbe
    from tau_incident.telemetry import ServiceCatalog, TelemetryProvider


class LocalRecordingError(RuntimeError):
    """Domain receipt may already exist even though local execution recording failed."""

    def __init__(self, command_id: str, receipt: Receipt | None, detail: str) -> None:
        self.command_id = command_id
        self.receipt = receipt
        super().__init__(
            f"local execution recording failed for {command_id}: {detail}; "
            "query receipt before retry"
        )


class IncidentRuntime:
    def __init__(
        self,
        store: CaseStore,
        evidence: EvidenceRecorder,
        execution: ExecutionRecorder,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.evidence = evidence
        self.execution = execution
        self.clock = clock
        self.owner: OwnerLease | None = None
        self.on_stop: Callable[[str | None], None] | None = None
        self.running_task: asyncio.Task[object] | None = None
        self.pending_workers: set[asyncio.Task[bool]] = set()
        self.analysis: AnalysisExecutor | None = None
        self.analysis_limits: AnalysisLimits | None = None

    def acquire_owner(self, case_id: str, limits: RunLimits | None = None) -> OwnerLease:
        from tau_incident.store.control import acquire

        self.owner = acquire(
            self.store, case_id, uuid4().hex, limits.lease_seconds if limits else 30, limits
        )
        if limits is not None:
            from tau_incident.events import ExtendBudget
            from tau_incident.store.control import policy

            # Lease acquisition may have survived a crash before its first policy
            # receipt. Reconcile the persisted policy before allowing any requests.
            has_policy_event = self.store._connection.execute(
                "SELECT 1 FROM events WHERE case_id=? "
                "AND json_extract(body,'$.payload.kind')='extend_budget' LIMIT 1",
                (case_id,),
            ).fetchone()
            if not has_policy_event:
                persisted = policy(self.store, case_id)
                receipt = self.execute(
                    Command(
                        command_id=f"initial-policy:{case_id}",
                        case_id=case_id,
                        payload=ExtendBudget(
                            scope=self.get_case(case_id).scope,
                            token_limit=persisted.token_limit,
                            clear_token_limit=persisted.token_limit is None,
                            deadline=persisted.deadline,
                            reason="initial case request policy",
                        ),
                    )
                )
                if receipt.status != "accepted":
                    raise ValueError(receipt.reason)
        return self.owner

    def renew_owner(self, seconds: float) -> None:
        from datetime import timedelta

        from tau_incident.store.control import require_owner

        lease = self.owner
        if lease is None:
            raise ValueError("runtime does not own a case")
        with self.store._transaction():
            require_owner(self.store, lease.case_id, lease.owner_id, lease.generation)
            expires = self.clock().astimezone(UTC) + timedelta(seconds=seconds)
            self.store._connection.execute(
                "UPDATE owners SET expires_at=? WHERE case_id=?",
                (expires.isoformat(), lease.case_id),
            )
        self.owner = lease.model_copy(update={"expires_at": expires})

    def release_owner(self) -> None:
        lease = self.owner
        if lease is not None:
            with self.store._transaction():
                self.store._connection.execute(
                    "UPDATE owners SET expires_at=? "
                    "WHERE case_id=? AND owner_id=? AND generation=?",
                    (
                        self.clock().astimezone(UTC).isoformat(),
                        lease.case_id,
                        lease.owner_id,
                        lease.generation,
                    ),
                )
            self.owner = None

    def lifecycle(
        self, case_id: str, action: str, reason: str, task_id: str | None = None
    ) -> Receipt:
        from tau_incident.events import Lifecycle

        payload = Lifecycle.model_validate(
            {
                "scope": self.get_case(case_id).scope,
                "action": action,
                "reason": reason,
                "task_id": task_id,
            }
        )
        operation = self.execution.start(
            "lifecycle",
            case_id=case_id,
            command_id=uuid4().hex,
            runtime_generation=self.owner.generation if self.owner else 0,
            task_id=task_id,
        )
        for attempt in self.get_case(case_id).attempts:
            if (
                attempt.status == "running"
                and attempt.dispatch_operation_id
                and task_id in {None, attempt.task_id}
            ):
                self.execution.link(operation.operation_id, attempt.dispatch_operation_id, action)
        try:
            receipt = self.execute(
                Command(command_id=operation.command_id, case_id=case_id, payload=payload),
                parent=operation,
            )
            self.execution.finish(
                operation.operation_id,
                status="succeeded",
                result=receipt.status,
                receipt_id=receipt.receipt_id,
                detail=receipt.reason,
            )
            return receipt
        except BaseException as exc:
            self.execution.finish(
                operation.operation_id,
                status="unknown" if isinstance(exc, CommitUnknown) else "failed",
                result="lifecycle_failed",
                detail=str(exc),
            )
            raise

    def pause(self, case_id: str, reason: str = "user paused investigation") -> Receipt:
        return self.lifecycle(case_id, "pause", reason)

    def resume(self, case_id: str) -> Receipt:
        return self.lifecycle(case_id, "resume", "resume persisted investigation")

    def cancel(self, case_id: str, task_id: str | None = None) -> Receipt:
        return self.lifecycle(case_id, "cancel", "user cancelled investigation", task_id)

    def shutdown(self, case_id: str) -> Receipt:
        return self.pause(case_id, "embedded runtime shutdown")

    def claim_ready_tasks(self, owner_generation: int, maximum: int) -> tuple[TaskAttempt, ...]:
        from tau_incident.context import ContextBuilder
        from tau_incident.events import DispatchTasks
        from tau_incident.models import ReadManifest, Source

        lease = self.owner
        if lease is None or lease.generation != owner_generation:
            raise ValueError("dispatch generation does not own runtime")
        case = self.get_case(lease.case_id)
        from tau_incident.store.control import ready_tasks

        ready = ready_tasks(case)
        claimed: list[TaskAttempt] = []
        for task in ready:
            if len(claimed) >= maximum:
                break
            case = self.get_case(lease.case_id)
            op = self.execution.start(
                "dispatch",
                case_id=case.case_id,
                command_id=uuid4().hex,
                runtime_generation=lease.generation,
                task_id=task.task_id,
            )
            basis = ContextBuilder.task_basis_for_dispatch(case, task.scope, task)
            previous = next((a for a in reversed(case.attempts) if a.task_id == task.task_id), None)
            attempt = TaskAttempt(
                attempt_id=uuid4().hex,
                case_id=case.case_id,
                task_id=task.task_id,
                contract_version=task.contract_version,
                starting_case_version=case.version,
                runtime_generation=lease.generation,
                execution_token=uuid4().hex,
                trace_id=uuid4().hex,
                scope=task.scope,
                source=Source(kind="runtime", actor="coordinator"),
                basis=basis,
                status="running",
                predecessor_attempt_id=previous.attempt_id if previous else None,
                dispatch_operation_id=op.operation_id,
                manifest=ReadManifest(dispatch=basis),
            )
            receipt = self.execute(
                Command(
                    command_id=op.command_id,
                    case_id=case.case_id,
                    payload=DispatchTasks(scope=case.scope, attempts=(attempt,)),
                ),
                parent=op,
            )
            self.execution.finish(
                op.operation_id,
                status="succeeded",
                result=receipt.status,
                receipt_id=receipt.receipt_id,
                detail=receipt.reason,
            )
            if receipt.status == "accepted":
                claimed.append(attempt)
        return tuple(claimed)

    async def run(
        self,
        case_id: str,
        *,
        runner: RoleRunner,
        telemetry: TelemetryProvider,
        catalog: ServiceCatalog,
        runner_factory: RunnerFactory | None = None,
        resume: bool = False,
        wait_probe: WaitProbe | None = None,
    ) -> RunResult:
        from tau_incident.investigation import run_investigation

        return await run_investigation(
            self,
            case_id,
            runner=runner,
            telemetry=telemetry,
            catalog=catalog,
            runner_factory=runner_factory,
            resume=resume,
            wait_probe=wait_probe,
        )

    def get_case(self, case_id: str) -> IncidentCase:
        return self.store.get_case(case_id)

    def events(
        self, case_id: str, after_cursor: int = 0, *, limit: int = 100
    ) -> tuple[DomainEvent, ...]:
        return self.store.events(case_id, after_cursor, limit=limit)

    def brief(self, case_id: str) -> CaseBrief:
        return case_brief(self.get_case(case_id))

    def report(self, case_id: str) -> ProgressReport:
        return progress_report(self.get_case(case_id))

    def execute(
        self,
        command: Command,
        expected_versions: tuple[VersionRef, ...] = (),
        *,
        parent: ExecutionRecord | None = None,
    ) -> Receipt:
        if (
            self.owner is not None
            and command.owner_id is None
            and command.case_id == self.owner.case_id
        ):
            command = command.model_copy(
                update={"owner_id": self.owner.owner_id, "owner_generation": self.owner.generation}
            )
        opened: list[ExecutionRecord] = []
        receipt: Receipt | None = None
        observation: Observation | None = None
        try:
            payload = command.payload
            attempt_id = (
                payload.finding.attempt_id
                if isinstance(payload, SubmitFinding)
                else payload.attempt_id
                if isinstance(
                    payload,
                    (AddObservation, FinishAttempt, RecordRead, RecordReview, UpdateTaskProgress),
                )
                else payload.reviews[0].attempt_id
                if isinstance(payload, RecordReviews)
                else None
            )
            attempt = (
                next(
                    (
                        a
                        for a in self.get_case(command.case_id).attempts
                        if a.attempt_id == attempt_id
                    ),
                    None,
                )
                if attempt_id is not None
                else None
            )
            root = self.execution.start(
                "manual_command",
                case_id=command.case_id,
                command_id=command.command_id,
                parent=parent,
                attempt_id=attempt_id,
                task_id=attempt.task_id if attempt else None,
                trace_id=attempt.trace_id if attempt else None,
                runtime_generation=attempt.runtime_generation
                if attempt
                else command.owner_generation or 0,
            )
            opened.append(root)
            submission = self.execution.start(
                "submission",
                case_id=command.case_id,
                command_id=command.command_id,
                parent=root,
            )
            opened.append(submission)
            receipt = self.store.matching_receipt(command, expected_versions)
            duplicate = receipt is not None
            if receipt is not None:
                self.execution.link(
                    submission.operation_id, receipt.source_operation_id, "duplicate_of"
                )
            else:
                if isinstance(command.payload, AddObservation):
                    registration = self.execution.start(
                        "evidence_registration",
                        case_id=command.case_id,
                        command_id=command.command_id,
                        parent=root,
                    )
                    opened.append(registration)
                    observation = self.evidence.record(
                        command.payload.observation,
                        evidence_id=uuid4().hex,
                        case_id=command.case_id,
                        operation_id=registration.operation_id,
                        collected_at=self.clock(),
                        attempt_id=command.payload.attempt_id,
                    )
                receipt = self.store.commit(
                    command,
                    expected_versions,
                    source_operation_id=submission.operation_id,
                    observation=observation,
                )
                # Another process may have committed while artifacts were prepared.
                duplicate = receipt.source_operation_id != submission.operation_id
                if duplicate:
                    self.execution.link(
                        submission.operation_id, receipt.source_operation_id, "duplicate_of"
                    )
            references: tuple[VersionRef, ...] = ()
            if receipt.status == "accepted" and receipt.case_version is not None:
                references = (
                    VersionRef(
                        case_id=command.case_id,
                        kind="case",
                        object_id=command.case_id,
                        version=receipt.case_version,
                    ),
                )
            while opened:
                record = opened[-1]
                result: str = "duplicate" if duplicate else receipt.status
                artifacts: tuple[str, ...] = ()
                record_refs = references
                if record.operation_kind == "evidence_registration" and observation is not None:
                    if not duplicate and receipt.status == "accepted":
                        result = observation.result
                        artifacts = (observation.artifact.artifact_id,)
                        record_refs += (
                            VersionRef(
                                case_id=command.case_id,
                                kind="evidence",
                                object_id=observation.evidence_id,
                                version=observation.version,
                            ),
                        )
                    else:
                        result = "not_registered"
                self.execution.finish(
                    record.operation_id,
                    status="succeeded",
                    result=result,
                    detail=receipt.reason,
                    references=record_refs,
                    event_ids=receipt.event_ids,
                    artifact_ids=artifacts,
                    receipt_id=receipt.receipt_id,
                )
                opened.pop()
            if receipt.status == "accepted" and not duplicate and self.on_stop is not None:
                from tau_incident.events import Lifecycle, RecordWait, ReviseTask

                if isinstance(payload, Lifecycle) and payload.action in {
                    "pause",
                    "cancel",
                    "budget_stop",
                }:
                    self.on_stop(payload.task_id)
                elif isinstance(payload, ReviseTask):
                    self.on_stop(payload.task.task_id)
                elif (
                    isinstance(payload, RecordWait)
                    and self.get_case(command.case_id).investigation_status == "paused"
                ):
                    self.on_stop(None)
            return receipt
        except BaseException as exc:
            category = (
                "idempotency_conflict"
                if isinstance(exc, IdempotencyConflict)
                else "local_storage_or_validation"
            )
            uncertain = isinstance(exc, CommitUnknown)
            recording_errors: list[str] = []
            if uncertain and receipt is None:
                try:
                    receipt = self.store.matching_receipt(command, expected_versions)
                except Exception as lookup_exc:
                    recording_errors.append(f"receipt lookup failed: {lookup_exc}")
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            failure_status: ExecutionStatus = (
                "unknown" if uncertain else "cancelled" if interrupted else "failed"
            )
            failure_result = (
                "commit_unknown"
                if uncertain and receipt is None
                else "receipt_found_after_error"
                if uncertain
                else "cancelled"
                if interrupted
                else "error"
            )
            for record in reversed(opened):
                try:
                    self.execution.finish(
                        record.operation_id,
                        status=failure_status,
                        result=failure_result,
                        error_category=category,
                        detail=str(exc),
                        receipt_id=receipt.receipt_id if receipt else None,
                    )
                except Exception as recording_exc:
                    recording_errors.append(str(recording_exc))
            if receipt is not None or recording_errors:
                raise LocalRecordingError(command.command_id, receipt, str(exc)) from exc
            raise
