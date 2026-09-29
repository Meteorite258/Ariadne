"""Manual commands, accepted events, receipts, and deterministic state reduction."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from tau_incident.failure import FailureCategory
from tau_incident.models import (
    CandidateExplanation,
    ClaimRevision,
    Decision,
    DiagnosisReport,
    EvidenceApplicability,
    ExecutionConstraint,
    ExecutionStatus,
    Finding,
    IncidentCase,
    InvestigationTask,
    MemoryCard,
    Model,
    Observation,
    ObservationInput,
    ReadManifest,
    ReviewIssue,
    Scope,
    Source,
    TaskAttempt,
    TaskProgress,
    Text,
    VersionRef,
    WaitCondition,
    WorkContent,
)


class CreateCase(Model):
    kind: Literal["create_case"] = "create_case"
    project_key: Text
    scope: Scope
    symptoms: Text
    impact: Text
    source: Source


class AddObservation(Model):
    kind: Literal["add_observation"] = "add_observation"
    observation: ObservationInput
    attempt_id: str | None = None


class AddExplanation(Model):
    kind: Literal["add_explanation"] = "add_explanation"
    text: Text
    scope: Scope
    source: Source


class AddConstraint(Model):
    kind: Literal["add_constraint"] = "add_constraint"
    text: Text
    scope: Scope
    source: Source


class RecordPlan(Model):
    kind: Literal["record_plan"] = "record_plan"
    scope: Scope
    decision: Decision
    task: InvestigationTask | None = None
    attempt: TaskAttempt | None = None
    wait: WaitCondition | None = None
    read_basis: tuple[VersionRef, ...] = ()
    constraint_ids: tuple[str, ...] | None = None


class SubmitFinding(Model):
    kind: Literal["submit_finding"] = "submit_finding"
    scope: Scope
    finding: Finding
    execution_token: Text
    reviews: tuple[ReviewIssue, ...] = ()
    manifest: ReadManifest | None = None


class UpdateTaskProgress(Model):
    kind: Literal["update_task_progress"] = "update_task_progress"
    scope: Scope
    task_id: Text
    contract_version: int
    expected_progress_version: int
    attempt_id: Text
    execution_token: Text
    progress: TaskProgress


class FinishAttempt(Model):
    kind: Literal["finish_attempt"] = "finish_attempt"
    scope: Scope
    attempt_id: Text
    execution_token: Text
    status: ExecutionStatus
    reason: Text
    error_category: FailureCategory | None = None


class RecordRead(Model):
    kind: Literal["record_read"] = "record_read"
    scope: Scope
    attempt_id: Text
    reference: VersionRef


class DispatchTasks(Model):
    kind: Literal["dispatch_tasks"] = "dispatch_tasks"
    scope: Scope
    attempts: tuple[TaskAttempt, ...]


class Lifecycle(Model):
    kind: Literal["lifecycle"] = "lifecycle"
    scope: Scope
    action: Literal["pause", "resume", "cancel", "waiting", "recover", "budget_stop"]
    reason: Text
    task_id: str | None = None


class RecordWait(Model):
    kind: Literal["record_wait"] = "record_wait"
    scope: Scope
    wait: WaitCondition


class ExtendBudget(Model):
    kind: Literal["extend_budget"] = "extend_budget"
    scope: Scope
    token_limit: Annotated[int, Field(ge=1)] | None = None
    clear_token_limit: bool = False
    tokens: Annotated[int, Field(ge=0)] = 0
    deadline: AwareDatetime | None = None
    reason: Text


class ReviseTask(Model):
    kind: Literal["revise_task"] = "revise_task"
    scope: Scope
    task: InvestigationTask
    reason: Text


class RetryTask(Model):
    kind: Literal["retry_task"] = "retry_task"
    scope: Scope
    task_id: Text
    reason: Text


class SetTaskDisposition(Model):
    kind: Literal["set_task_disposition"] = "set_task_disposition"
    scope: Scope
    task_id: Text
    contract_version: int
    nonblocking: bool
    reason: Text


class RecordReview(Model):
    kind: Literal["record_review"] = "record_review"
    scope: Scope
    issue: ReviewIssue
    attempt_id: Text
    execution_token: Text
    basis: tuple[VersionRef, ...]
    claim: ClaimRevision | None = None
    satisfied_conditions: tuple[str, ...] = ()


class RecordReviews(Model):
    kind: Literal["record_reviews"] = "record_reviews"
    scope: Scope
    reviews: tuple[RecordReview, ...] = Field(min_length=2)


class ScheduleReview(Model):
    kind: Literal["schedule_review"] = "schedule_review"
    scope: Scope
    issue: ReviewIssue
    task: InvestigationTask | None = None


class ReviseEvidence(Model):
    kind: Literal["revise_evidence"] = "revise_evidence"
    scope: Scope
    change: EvidenceApplicability


class CommitReport(Model):
    kind: Literal["commit_report"] = "commit_report"
    scope: Scope
    report: DiagnosisReport


class CommitMemory(Model):
    kind: Literal["commit_memory"] = "commit_memory"
    scope: Scope
    card: MemoryCard


class SetImpact(Model):
    kind: Literal["set_impact"] = "set_impact"
    scope: Scope
    status: Literal["unknown", "ongoing", "recovered"]
    basis: tuple[VersionRef, ...]
    reason: Text


class ReopenCase(Model):
    kind: Literal["reopen_case"] = "reopen_case"
    scope: Scope
    reason: Text


class WithdrawReport(Model):
    kind: Literal["withdraw_report"] = "withdraw_report"
    scope: Scope
    target: VersionRef
    reason: Text


Payload = Annotated[
    CreateCase
    | AddObservation
    | AddExplanation
    | AddConstraint
    | RecordPlan
    | SubmitFinding
    | UpdateTaskProgress
    | FinishAttempt
    | RecordRead
    | DispatchTasks
    | Lifecycle
    | RecordWait
    | ExtendBudget
    | ReviseTask
    | RetryTask
    | SetTaskDisposition
    | RecordReview
    | RecordReviews
    | ScheduleReview
    | ReviseEvidence
    | CommitReport
    | CommitMemory
    | SetImpact
    | ReopenCase
    | WithdrawReport,
    # Task revisions and retries preserve historical attempts and accumulated usage.
    # Runtime control commands use the same idempotent commit protocol.
    Field(discriminator="kind"),
]


class Command(Model):
    command_id: Text
    case_id: Text
    payload: Payload
    owner_id: str | None = None
    owner_generation: int | None = None

    def content_hash(self, expected_versions: tuple[VersionRef, ...]) -> str:
        """Generated evidence/operation IDs and timestamps are not caller intent."""
        command_data = self.model_dump(mode="json")
        # Ownership fences execution, not stable caller intent across recovery.
        command_data.pop("owner_id", None)
        command_data.pop("owner_generation", None)
        if isinstance(self.payload, UpdateTaskProgress):
            command_data["payload"]["progress"].pop("execution_cursor", None)

        # Keep pre-Stage-4 command hashes stable when newly added fields carry defaults.
        def strip_defaults(value: object) -> None:
            if isinstance(value, list):
                for item in value:
                    strip_defaults(item)
            elif isinstance(value, dict):
                defaults: dict[str, object] = {}
                if "claim_id" in value:
                    defaults.update(judgment="interpretation", verification="unverified")
                if "contract_version" in value and "goal" in value:
                    defaults.update(
                        rationale=[], review_id=None, review_version=None, review_cycle=None
                    )
                if "review_id" in value and "gap" in value:
                    defaults.update(
                        problem="important_conclusion",
                        basis=[],
                        cycle=1,
                        repair_rounds=0,
                        max_repairs=2,
                        disposition="pending",
                        assessment=None,
                        stop_reason=None,
                        attempt_id=None,
                        repair_task_ids=[],
                        checked_basis=[],
                        new_check=None,
                        judgment_action="retain",
                    )
                for key, default in defaults.items():
                    if value.get(key) == default:
                        value.pop(key, None)
                for child in value.values():
                    strip_defaults(child)

        strip_defaults(command_data)
        if isinstance(self.payload, AddObservation) and self.payload.attempt_id is None:
            # Preserve the Stage 1 command hash for manual-observation retries.
            command_data["payload"].pop("attempt_id", None)
        if (
            isinstance(self.payload, AddObservation)
            and self.payload.observation.source_revision is None
        ):
            command_data["payload"]["observation"].pop("source_revision", None)
        if isinstance(self.payload, SubmitFinding) and self.payload.manifest is None:
            command_data["payload"].pop("manifest", None)
        if isinstance(self.payload, RecordPlan):
            data = command_data["payload"]
            for key in ("wait", "constraint_ids"):
                if data[key] is None:
                    data.pop(key)
            if not data["read_basis"]:
                data.pop("read_basis")
            if data["task"] is not None:
                for key, default in (("status", "ready"), ("active_attempt_id", None)):
                    if data["task"][key] == default:
                        data["task"].pop(key)
            if data["attempt"] is not None:
                for key in ("dispatch_operation_id", "manifest"):
                    if (
                        data["attempt"][key] is None
                        or key == "manifest"
                        and not any(data["attempt"][key].values())
                    ):
                        data["attempt"].pop(key)
        body = {
            "command": command_data,
            "expected_versions": sorted(
                (ref.model_dump(mode="json") for ref in expected_versions),
                key=lambda ref: (str(ref["kind"]), str(ref["object_id"])),
            ),
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Receipt(Model):
    receipt_id: Text
    command_id: Text
    content_hash: Text
    case_id: Text
    status: Literal["accepted", "rejected", "conflict"]
    reason: str | None = None
    expected_versions: tuple[VersionRef, ...]
    checked_versions: tuple[VersionRef, ...]
    case_version: int | None
    event_ids: tuple[str, ...] = ()
    source_operation_id: Text
    committed_at: AwareDatetime


class CaseCreated(Model):
    kind: Literal["case_created"] = "case_created"
    case: IncidentCase


class ObservationAdded(Model):
    kind: Literal["observation_added"] = "observation_added"
    observation: Observation


class ExplanationAdded(Model):
    kind: Literal["explanation_added"] = "explanation_added"
    explanation: CandidateExplanation


class ConstraintAdded(Model):
    kind: Literal["constraint_added"] = "constraint_added"
    constraint: ExecutionConstraint


EventPayload = Annotated[
    CaseCreated
    | ObservationAdded
    | ExplanationAdded
    | ConstraintAdded
    | RecordPlan
    | SubmitFinding
    | UpdateTaskProgress
    | FinishAttempt
    | RecordRead
    | DispatchTasks
    | Lifecycle
    | RecordWait
    | ExtendBudget
    | ReviseTask
    | RetryTask
    | SetTaskDisposition
    | RecordReview
    | RecordReviews
    | ScheduleReview
    | ReviseEvidence
    | CommitReport
    | CommitMemory
    | SetImpact
    | ReopenCase
    | WithdrawReport,
    Field(discriminator="kind"),
]


class DomainEvent(Model):
    schema_version: Literal[2] = 2
    event_id: Text
    case_id: Text
    case_version: Annotated[int, Field(ge=1)]
    command_id: Text
    source_operation_id: Text
    occurred_at: AwareDatetime
    payload: EventPayload
    cursor: Annotated[int, Field(ge=0)] = 0


def reduce_case(current: IncidentCase | None, event: DomainEvent) -> IncidentCase:
    """Apply only accepted events, without I/O, clocks, random IDs, or model work."""
    payload = event.payload
    if isinstance(payload, CaseCreated):
        if current is not None or event.case_version != 1:
            raise ValueError("case creation requires an absent case and version one")
        if payload.case.case_id != event.case_id or payload.case.version != 1:
            raise ValueError("case creation identity mismatch")
        return payload.case
    if current is None or current.case_id != event.case_id:
        raise ValueError("event has no matching case")
    if event.case_version != current.version + 1:
        raise ValueError("non-contiguous case event version")
    data = current.model_dump()
    data["tasks"] = current.tasks
    data.update(version=event.case_version, updated_at=event.occurred_at)
    if isinstance(payload, ObservationAdded):
        if payload.observation.case_id != current.case_id:
            raise ValueError("evidence belongs to a different case")
        data["observations"] = (*current.observations, payload.observation)
    elif isinstance(payload, ExplanationAdded):
        if payload.explanation.case_id != current.case_id:
            raise ValueError("explanation belongs to a different case")
        data["candidate_explanations"] = (*current.candidate_explanations, payload.explanation)
    elif isinstance(payload, ConstraintAdded):
        if payload.constraint.case_id != current.case_id:
            raise ValueError("constraint belongs to a different case")
        data["constraints"] = (*current.constraints, payload.constraint)
    elif isinstance(payload, RecordPlan):
        data["decisions"] = (*current.decisions, payload.decision)
        if payload.task is not None:
            task = payload.task
            if payload.attempt is not None:
                task = task.model_copy(
                    update={"status": "running", "active_attempt_id": payload.attempt.attempt_id}
                )
            data["tasks"] = (*current.tasks, task)
        if payload.attempt is not None:
            data["attempts"] = (*current.attempts, payload.attempt)
            data["investigation_status"] = "investigating"
        if payload.wait is not None:
            wait = payload.wait
            data["waits"] = (*current.waits, wait)
            data["tasks"] = tuple(
                t.model_copy(update={"status": "waiting"}) if t.task_id == wait.task_id else t
                for t in data["tasks"]
            )
    elif isinstance(payload, ReviseTask):
        data["tasks"] = tuple(
            payload.task.model_copy(update={"progress": t.progress})
            if t.task_id == payload.task.task_id
            else t
            for t in current.tasks
        )
        data["attempts"] = tuple(
            a.model_copy(update={"status": "cancelled", "cancellation_reason": payload.reason})
            if a.task_id == payload.task.task_id and a.status == "running"
            else a
            for a in current.attempts
        )
        data["waits"] = tuple(
            w.model_copy(update={"status": "cancelled", "version": w.version + 1})
            if w.task_id == payload.task.task_id and w.status == "pending"
            else w
            for w in current.waits
        )
    elif isinstance(payload, SetTaskDisposition):
        data["tasks"] = tuple(
            t.model_copy(
                update={
                    "contract_version": t.contract_version + 1,
                    "nonblocking_reason": payload.reason if payload.nonblocking else None,
                    "status": "cancelled" if payload.nonblocking else "ready",
                }
            )
            if t.task_id == payload.task_id
            else t
            for t in current.tasks
        )
        data["waits"] = tuple(
            w.model_copy(update={"status": "cancelled", "version": w.version + 1})
            if w.task_id == payload.task_id and w.status == "pending"
            else w
            for w in current.waits
        )
    elif isinstance(payload, RetryTask):
        data["tasks"] = tuple(
            t.model_copy(update={"status": "ready", "active_attempt_id": None})
            if t.task_id == payload.task_id
            else t
            for t in current.tasks
        )
    elif isinstance(payload, DispatchTasks):
        data["attempts"] = (*current.attempts, *payload.attempts)
        assigned = {a.task_id: a.attempt_id for a in payload.attempts}
        data["tasks"] = tuple(
            t.model_copy(update={"status": "running", "active_attempt_id": assigned[t.task_id]})
            if t.task_id in assigned
            else t
            for t in current.tasks
        )
        data["investigation_status"] = "investigating"
    elif isinstance(payload, Lifecycle):
        interrupted = payload.action in {"pause", "cancel", "recover", "budget_stop"}
        targets = {t.task_id for t in current.tasks if payload.task_id in {None, t.task_id}}
        if interrupted:
            data["attempts"] = tuple(
                a.model_copy(
                    update={
                        "status": "interrupted" if payload.action == "recover" else "cancelled",
                        "cancellation_reason": payload.reason,
                    }
                )
                if a.task_id in targets and a.status == "running"
                else a
                for a in current.attempts
            )
            data["tasks"] = tuple(
                t.model_copy(
                    update={
                        "status": "cancelled" if payload.action == "cancel" else "ready",
                        "active_attempt_id": None,
                    }
                )
                if t.task_id in targets
                and t.status in {"running", "ready", "waiting"}
                and (payload.action != "recover" or t.status == "running")
                and (payload.action == "cancel" or t.status != "waiting")
                else t
                for t in current.tasks
            )
        if payload.action == "cancel":
            data["waits"] = tuple(
                w.model_copy(update={"status": "cancelled", "version": w.version + 1})
                if w.status == "pending" and payload.task_id in {None, w.task_id}
                else w
                for w in current.waits
            )
        if payload.task_id is None:
            if payload.action in {"pause", "budget_stop"}:
                data["control_intent"] = "pause"
            elif payload.action == "cancel":
                data["control_intent"] = "cancel"
            elif payload.action == "resume":
                data["control_intent"] = "run"
            data["investigation_status"] = (
                "paused"
                if payload.action in {"pause", "cancel", "budget_stop"}
                else "waiting"
                if payload.action == "waiting"
                else "investigating"
                if payload.action == "resume"
                else current.investigation_status
            )
    elif isinstance(payload, RecordWait):
        wait = payload.wait
        data["waits"] = (*tuple(w for w in current.waits if w.wait_id != wait.wait_id), wait)
        if wait.task_id is not None:
            state = (
                "waiting"
                if wait.status == "pending"
                else (
                    "cancelled"
                    if wait.status == "timed_out" and wait.on_timeout == "cancel"
                    else "ready"
                )
            )
            data["tasks"] = tuple(
                t.model_copy(update={"status": state}) if t.task_id == wait.task_id else t
                for t in current.tasks
            )
        if wait.status == "timed_out" and (
            wait.on_timeout == "pause" or (wait.on_timeout == "cancel" and wait.task_id is None)
        ):
            data["investigation_status"] = "paused"
            data["control_intent"] = "cancel" if wait.on_timeout == "cancel" else "pause"
            data["attempts"] = tuple(
                a.model_copy(
                    update={"status": "cancelled", "cancellation_reason": "wait deadline reached"}
                )
                if a.status == "running"
                else a
                for a in current.attempts
            )
            cancel_all = wait.on_timeout == "cancel"
            if cancel_all:
                data["waits"] = tuple(
                    w.model_copy(update={"status": "cancelled", "version": w.version + 1})
                    if w.status == "pending"
                    else w
                    for w in data["waits"]
                )
            data["tasks"] = tuple(
                t.model_copy(
                    update={
                        "status": "cancelled" if cancel_all else "ready",
                        "active_attempt_id": None,
                    }
                )
                if t.status == "running" or (cancel_all and t.status in {"waiting", "ready"})
                else t
                for t in data["tasks"]
            )
        elif wait.status != "pending" and current.investigation_status == "waiting":
            data["investigation_status"] = "investigating"
    elif isinstance(payload, UpdateTaskProgress):
        data["tasks"] = tuple(
            t.model_copy(update={"progress": payload.progress})
            if t.task_id == payload.task_id
            else t
            for t in current.tasks
        )
    elif isinstance(payload, SubmitFinding):
        finding = payload.finding
        data["findings"] = (*current.findings, finding)
        data["claims"] = (*current.claims, *finding.proposed_claims)
        data["review_issues"] = (*current.review_issues, *payload.reviews)
        data["attempts"] = tuple(
            attempt.model_copy(
                update={
                    "status": "succeeded",
                    "final_command_id": event.command_id,
                    "manifest": payload.manifest or attempt.manifest,
                }
            )
            if attempt.attempt_id == finding.attempt_id
            else attempt
            for attempt in current.attempts
        )
        data["tasks"] = tuple(
            t.model_copy(
                update={
                    "status": "completed"
                    if finding.completion == "complete"
                    and not (payload.manifest and payload.manifest.changed)
                    else "blocked"
                    if finding.completion == "blocked"
                    else "ready",
                    "active_attempt_id": None,
                    "progress": TaskProgress(
                        **finding.model_dump(include=set(WorkContent.model_fields)),
                        version=t.progress.version + 1,
                        attempt_id=finding.attempt_id,
                        execution_cursor=t.progress.execution_cursor,
                    ),
                }
            )
            if t.active_attempt_id == finding.attempt_id
            else t
            for t in current.tasks
        )
    elif isinstance(payload, FinishAttempt):
        data["attempts"] = tuple(
            attempt.model_copy(
                update={
                    "status": payload.status,
                    "cancellation_reason": payload.reason,
                    "error_category": payload.error_category,
                }
            )
            if attempt.attempt_id == payload.attempt_id
            else attempt
            for attempt in current.attempts
        )
        data["tasks"] = tuple(
            t.model_copy(update={"status": "blocked", "active_attempt_id": None})
            if t.active_attempt_id == payload.attempt_id
            else t
            for t in current.tasks
        )
    elif isinstance(payload, RecordRead):
        data["attempts"] = tuple(
            attempt.model_copy(
                update={"reads": tuple(dict.fromkeys((*attempt.reads, payload.reference)))}
            )
            if attempt.attempt_id == payload.attempt_id
            else attempt
            for attempt in current.attempts
        )
    from tau_incident.projection import project_case
    from tau_incident.quality import reduce_quality

    return project_case(reduce_quality(current, IncidentCase.model_validate(data), event))
