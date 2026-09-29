"""Versioned domain contracts. Later-stage types do not imply executable features."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from tau_agent.types import JSONValue
from tau_incident.failure import FailureCategory

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Version = Annotated[int, Field(ge=1)]
ResultKind = Literal["complete", "partial", "no_match", "failed", "unknown"]
OperationKind = Literal[
    "alert_receive",
    "alert_associate",
    "manual_command",
    "evidence_registration",
    "submission",
    "planning",
    "investigation",
    "context",
    "model_request",
    "tool",
    "output_validation",
    "dispatch",
    "wait_check",
    "lifecycle",
    "recovery",
    "review",
    "report",
    "memory",
]
ExecutionStatus = Literal["running", "succeeded", "failed", "cancelled", "interrupted", "unknown"]
ObjectKind = Literal[
    "case",
    "evidence",
    "claim",
    "task",
    "attempt",
    "report",
    "memory",
    "request",
    "review",
    "wait",
    "decision",
    "input",
    "finding",
]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeWindow(Model):
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("time window ends before it starts")
        return self


class Scope(Model):
    environment: Text
    entities: tuple[Text, ...] = ()
    time_window: TimeWindow = Field(default_factory=TimeWindow)


class VersionRef(Model):
    """Exact object revision, always scoped to a case; version zero means absent elsewhere."""

    case_id: Text
    kind: ObjectKind
    object_id: Text
    version: Version


class Source(Model):
    kind: Literal["human", "tool", "import", "runtime"]
    actor: Text
    reference: str | None = None


class ArtifactRef(Model):
    artifact_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=0)]
    media_type: Text = "text/plain; charset=utf-8"

    @model_validator(mode="after")
    def content_addressed(self) -> Self:
        if self.artifact_id != self.sha256:
            raise ValueError("artifact ID must equal its content hash")
        return self


class ObservationInput(Model):
    summary: Text
    raw_text: str
    scope: Scope
    source: Source
    actual_query: dict[str, JSONValue] = Field(default_factory=dict)
    data_time: TimeWindow = Field(default_factory=TimeWindow)
    available_at: AwareDatetime | None = None
    collected_at: AwareDatetime | None = None
    actual_coverage: Scope | None = None
    result: ResultKind = "unknown"
    units: str | None = None
    sampled: bool | None = None
    truncated: bool | None = None
    completeness_note: str | None = None
    method: str | None = None
    inputs: tuple[VersionRef, ...] = ()
    baseline: str | None = None
    source_revision: str | None = None


class Observation(Model):
    evidence_id: Text
    case_id: Text
    version: Version = 1
    summary: Text
    scope: Scope
    source: Source
    source_operation_id: Text
    attempt_id: str | None = None
    actual_query: dict[str, JSONValue]
    data_time: TimeWindow
    available_at: AwareDatetime | None
    collected_at: AwareDatetime
    actual_coverage: Scope | None
    result: ResultKind
    units: str | None
    sampled: bool | None
    truncated: bool | None
    completeness_note: str | None
    method: str | None
    inputs: tuple[VersionRef, ...]
    baseline: str | None
    artifact: ArtifactRef
    source_revision: str | None = None


EvidenceRecord = Observation


class CandidateExplanation(Model):
    input_id: Text
    case_id: Text
    version: Version = 1
    text: Text
    scope: Scope
    source: Source
    created_at: AwareDatetime
    status: Literal["unverified"] = "unverified"


class ExecutionConstraint(Model):
    input_id: Text
    case_id: Text
    version: Version = 1
    text: Text
    scope: Scope
    source: Source
    created_at: AwareDatetime


class IncidentCase(Model):
    case_id: Text
    project_key: Text
    scope: Scope
    symptoms: Text
    impact: Text
    source: Source
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: Version = 1
    investigation_status: Literal["open", "investigating", "waiting", "paused", "completed"] = (
        "open"
    )
    control_intent: Literal["run", "pause", "cancel"] = "run"
    impact_status: Literal["unknown", "ongoing", "recovered"] = "unknown"
    observations: tuple[Observation, ...] = ()
    candidate_explanations: tuple[CandidateExplanation, ...] = ()
    constraints: tuple[ExecutionConstraint, ...] = ()
    decisions: tuple[Decision, ...] = ()
    tasks: tuple[InvestigationTask, ...] = ()
    attempts: tuple[TaskAttempt, ...] = ()
    findings: tuple[Finding, ...] = ()
    claims: tuple[ClaimRevision, ...] = ()
    review_issues: tuple[ReviewIssue, ...] = ()
    waits: tuple[WaitCondition, ...] = ()
    reports: tuple[DiagnosisReport, ...] = ()
    memories: tuple[MemoryCard, ...] = ()
    evidence_changes: tuple[EvidenceApplicability, ...] = ()
    recovery_basis: tuple[VersionRef, ...] = ()


class JudgmentDependency(Model):
    relation: Literal["supports", "opposes", "derived_from"]
    reference: VersionRef

    @model_validator(mode="after")
    def typed_reference(self) -> Self:
        expected = {"claim"} if self.relation == "derived_from" else {"evidence"}
        if self.reference.kind not in expected:
            raise ValueError("support/opposition requires evidence; derivation requires a claim")
        return self


class EvidenceApplicability(Model):
    evidence: VersionRef
    version: Version
    applicable: bool
    reason: Text
    source: Source


class ClaimRevision(Model):
    claim_id: Text
    case_id: Text
    version: Version
    statement: Text
    scope: Scope
    source: Source
    revision_reason: Text
    validity: Literal["proposed", "current", "needs_review", "withdrawn"]
    judgment: Literal["interpretation", "exclusion", "diagnosis"] = "interpretation"
    dependencies: tuple[JudgmentDependency, ...] = ()
    verification: Literal["unverified"] = "unverified"

    @model_validator(mode="before")
    @classmethod
    def canonical_dependencies(cls, value: object) -> object:
        # Accept the ergonomic constructor lists at the boundary, persist one representation.
        if not isinstance(value, dict):
            return value
        data = dict(value)
        supplied = any(key in data for key in ("support", "opposition", "premises"))
        dependencies = tuple(
            JudgmentDependency.model_validate({"relation": relation, "reference": ref})
            for key, relation in (
                ("support", "supports"),
                ("opposition", "opposes"),
                ("premises", "derived_from"),
            )
            for ref in data.pop(key, ())
        )
        if supplied:
            existing = tuple(
                JudgmentDependency.model_validate(item) for item in data.get("dependencies", ())
            )
            if existing and set(existing) != set(dependencies):
                raise ValueError("typed dependencies conflict with supplied reference lists")
            data["dependencies"] = dependencies
        return data

    @property
    def support(self) -> tuple[VersionRef, ...]:
        return tuple(d.reference for d in self.dependencies if d.relation == "supports")

    @property
    def opposition(self) -> tuple[VersionRef, ...]:
        return tuple(d.reference for d in self.dependencies if d.relation == "opposes")

    @property
    def premises(self) -> tuple[VersionRef, ...]:
        return tuple(d.reference for d in self.dependencies if d.relation == "derived_from")


class WorkBlocker(Model):
    kind: Literal["external", "user_input", "execution"]
    detail: Text
    required_change: Text


class WorkContent(Model):
    """Semantic work shared by live progress and frozen submissions; no raw evidence."""

    summary: str = ""
    observations: tuple[VersionRef, ...] = ()
    basis: tuple[VersionRef, ...] = ()
    counterevidence: tuple[VersionRef, ...] = ()
    gaps: tuple[str, ...] = ()
    new_questions: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    checked_operations: tuple[str, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    blocker: WorkBlocker | None = None


class TaskProgress(WorkContent):
    version: Annotated[int, Field(ge=0)] = 0
    attempt_id: str | None = None
    execution_cursor: Annotated[int, Field(ge=0)] = 0


class InvestigationTask(Model):
    task_id: Text
    case_id: Text
    contract_version: Version
    kind: Literal["explore", "distinguish", "verify", "repair", "review", "diagnose"]
    goal: Text
    scope: Scope
    source: Source
    completion_conditions: tuple[Text, ...]
    prerequisites: tuple[VersionRef, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    status: Literal["ready", "running", "waiting", "completed", "blocked", "cancelled"] = "ready"
    active_attempt_id: str | None = None
    status_source_version: Annotated[int, Field(ge=0)] = 0
    progress: TaskProgress = Field(default_factory=TaskProgress)
    related_claims: tuple[VersionRef, ...] = ()
    discriminating_question: str | None = None
    result_meaning: str | None = None
    nonblocking_reason: str | None = None
    rationale: tuple[VersionRef, ...] = ()
    review_id: str | None = None
    review_version: int | None = None
    review_cycle: int | None = None

    @property
    def condition_ids(self) -> tuple[str, ...]:
        return tuple(
            f"{self.task_id}:{self.contract_version}:c{index + 1}"
            for index in range(len(self.completion_conditions))
        )


class TaskAttempt(Model):
    attempt_id: Text
    case_id: Text
    task_id: Text
    contract_version: Version
    starting_case_version: Version
    runtime_generation: Annotated[int, Field(ge=0)]
    execution_token: Text
    trace_id: Text
    scope: Scope
    source: Source
    basis: tuple[VersionRef, ...] = ()
    reads: tuple[VersionRef, ...] = ()
    status: ExecutionStatus
    cost: Annotated[float, Field(ge=0)] | None = None
    cancellation_reason: str | None = None
    error_category: FailureCategory | None = None
    final_command_id: str | None = None
    predecessor_attempt_id: str | None = None
    dispatch_operation_id: str | None = None
    manifest: ReadManifest = Field(default_factory=lambda: ReadManifest())


class ReadManifest(Model):
    dispatch: tuple[VersionRef, ...] = ()
    requests: tuple[Text, ...] = ()
    dynamic: tuple[VersionRef, ...] = ()
    finding: tuple[VersionRef, ...] = ()
    changed: tuple[VersionRef, ...] = ()


class RequestSnapshot(Model):
    request_id: Text
    case_id: Text
    attempt_id: str | None = None
    version: Version = 1
    scope: Scope
    source: Source
    model: Text
    configuration: dict[str, JSONValue]
    provider_input: ArtifactRef
    tool_definitions: tuple[dict[str, JSONValue], ...]
    selection: tuple[VersionRef, ...]
    memory_selection: tuple[VersionRef, ...] = ()
    memory_retrieval: tuple[VersionRef, ...] = ()
    omissions: tuple[str, ...] = ()
    runtime_generation: Annotated[int, Field(ge=0)] = 0


class Finding(WorkContent):
    finding_id: Text
    case_id: Text
    attempt_id: Text
    scope: Scope
    source: Source
    proposed_claims: tuple[ClaimRevision, ...] = ()
    proposed_changes: dict[str, JSONValue] = Field(default_factory=dict)
    completion: Literal["complete", "partial", "blocked"]
    satisfied_conditions: tuple[str, ...] = ()


class Decision(Model):
    decision_id: Text
    case_id: Text
    version: Version
    scope: Scope
    source: Source
    choice: Text
    reason: Text
    basis: tuple[VersionRef, ...]
    information_signature: str | None = None


class ReviewIssue(Model):
    review_id: Text
    case_id: Text
    version: Version
    scope: Scope
    source: Source
    target: VersionRef
    gap: Text
    impact: Text
    required_action: Text
    status: Literal["open", "resolved", "unresolved"]
    problem: Literal["important_conclusion", "conflict", "basis_changed", "repair"] = (
        "important_conclusion"
    )
    basis: tuple[VersionRef, ...] = ()
    cycle: Annotated[int, Field(ge=1)] = 1
    repair_rounds: Annotated[int, Field(ge=0)] = 0
    max_repairs: Annotated[int, Field(ge=0)] = 2
    disposition: Literal["pending", "accepted", "repair", "unresolved", "superseded"] = "pending"
    assessment: str | None = None
    stop_reason: str | None = None
    attempt_id: str | None = None
    repair_task_ids: tuple[str, ...] = ()
    checked_basis: tuple[VersionRef, ...] = ()
    new_check: str | None = None
    judgment_action: Literal["retain", "withdraw"] = "retain"


class WaitCondition(Model):
    wait_id: Text
    case_id: Text
    version: Version
    scope: Scope
    source: Source
    condition: dict[str, JSONValue]
    next_check_at: AwareDatetime
    deadline: AwareDatetime
    on_timeout: Text
    task_id: str | None = None
    status: Literal["pending", "satisfied", "timed_out", "cancelled"] = "pending"
    interval_seconds: Annotated[float, Field(gt=0)] = 30
    operation_id: str | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def valid_condition(self) -> Self:
        if self.next_check_at > self.deadline:
            raise ValueError("wait check cannot be later than deadline")
        if self.on_timeout not in {"resume", "cancel", "pause"}:
            raise ValueError("wait timeout action must be resume, cancel or pause")
        if self.condition.get("kind") not in {"time", "evidence", "watermark"}:
            raise ValueError("wait requires time, evidence or watermark condition")
        if self.condition.get("kind") == "watermark" and not self.condition.get("source"):
            raise ValueError("watermark wait requires a source")
        if self.condition.get("kind") == "watermark":
            from datetime import datetime

            mark = datetime.fromisoformat(str(self.condition.get("watermark", "")))
            if mark.tzinfo is None or self.condition.get("signal") not in {
                "logs",
                "metrics",
                "traces",
                "configuration",
                "deployments",
            }:
                raise ValueError("watermark requires an aware timestamp and a signal kind")
        if "after" in self.condition:
            from datetime import datetime

            if datetime.fromisoformat(str(self.condition["after"])).tzinfo is None:
                raise ValueError("evidence wait cutoff requires timezone")
        return self


class OwnerLease(Model):
    case_id: Text
    owner_id: Text
    generation: Annotated[int, Field(ge=1)]
    expires_at: AwareDatetime


class DiagnosisReport(Model):
    report_id: Text
    case_id: Text
    version: Version
    case_version: Version
    scope: Scope
    source: Source
    kind: Literal["progress", "diagnosis"]
    explanation: Text
    evidence_cutoff: AwareDatetime
    basis: tuple[VersionRef, ...]
    unresolved: tuple[str, ...]
    recommendations: tuple[str, ...] = ()
    verification: Literal["unverified", "verified", "disputed", "withdrawn"]
    supersedes: VersionRef | None = None
    state: Literal["current", "stale", "withdrawn"] = "current"
    propagation: tuple[str, ...] = ()
    support: tuple[VersionRef, ...] = ()
    counterevidence: tuple[VersionRef, ...] = ()
    alternatives: tuple[str, ...] = ()
    verification_sources: tuple[VersionRef, ...] = ()
    impact_status: Literal["unknown", "ongoing", "recovered"] = "unknown"
    input_tasks: tuple[str, ...] = ()
    input_evidence: tuple[VersionRef, ...] = ()


class MemoryCard(Model):
    memory_id: Text
    case_id: Text
    version: Version
    project_key: Text
    scope: Scope
    source: Source
    source_report: VersionRef
    evidence_cutoff: AwareDatetime
    summary: Text
    verification: Literal["unverified", "verified", "disputed", "withdrawn"]
    supersedes: VersionRef | None = None
    kind: Literal["diagnosis", "incomplete"] = "incomplete"
    state: Literal["current", "stale", "withdrawn"] = "current"
    symptoms: Text = "unspecified"
    effective_paths: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()


class MemoryMatch(Model):
    card: MemoryCard
    differences: tuple[str, ...] = ()
    notice: str = "Historical investigation, not evidence about the current incident."


class ExecutionLink(Model):
    operation_id: Text
    relation: Text


class ExecutionRecord(Model):
    schema_version: Literal[1] = 1
    operation_id: Text
    trace_id: Text
    span_id: Text
    parent_operation_id: str | None = None
    parent_span_id: str | None = None
    runtime_generation: Annotated[int, Field(ge=0)] = 0
    case_id: Text | None
    task_id: str | None = None
    attempt_id: str | None = None
    request_id: str | None = None
    tool_call_id: str | None = None
    command_id: Text
    operation_kind: OperationKind
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    duration_ms: Annotated[float, Field(ge=0)] | None = None
    status: ExecutionStatus = "running"
    error_category: str | None = None
    result: str | None = None
    detail: str | None = None
    references: tuple[VersionRef, ...] = ()
    event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    receipt_id: str | None = None
    links: tuple[ExecutionLink, ...] = ()
    # Incremented on start/finish/link, so cursor polling also sees completed operations.
    cursor: Annotated[int, Field(ge=0)] = 0


IncidentCase.model_rebuild()
