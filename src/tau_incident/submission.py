"""Deterministic submission checks, repeated by the store under its commit lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tau_incident.events import Command, FinishAttempt, Receipt, RecordPlan, SubmitFinding
from tau_incident.models import Finding, IncidentCase, ReviewIssue, Scope, VersionRef

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime


def scope_contains(outer: Scope, inner: Scope) -> bool:
    if outer.environment != inner.environment:
        return False
    if outer.entities and (not inner.entities or not set(inner.entities) <= set(outer.entities)):
        return False
    a, b = outer.time_window, inner.time_window
    return not (
        (a.start is not None and (b.start is None or b.start < a.start))
        or (a.end is not None and (b.end is None or b.end > a.end))
    )


def validate_submission(
    case: IncidentCase, payload: RecordPlan | SubmitFinding | FinishAttempt
) -> str | None:
    if not scope_contains(case.scope, payload.scope):
        return "submission scope exceeds case"
    if isinstance(payload, RecordPlan):
        from tau_incident.telemetry.tools import TOOL_NAMES

        decision, task, attempt = payload.decision, payload.task, payload.attempt
        if decision.case_id != case.case_id or decision.scope != payload.scope:
            return "decision identity/scope mismatch"
        if any(d.decision_id == decision.decision_id for d in case.decisions):
            return "decision already exists"
        if attempt is not None:
            return "attempts must be allocated by dispatch"
        if task is None:
            return None
        if task.kind not in {"explore", "distinguish", "verify", "diagnose"}:
            return "review and repair tasks require the quality scheduling command"
        if any(
            r.kind != "task"
            or r.case_id != case.case_id
            or not any(
                t.task_id == r.object_id and t.contract_version == r.version for t in case.tasks
            )
            for r in task.prerequisites
        ):
            return "prerequisites must reference existing task contracts"
        if (
            task.case_id != case.case_id
            or task.status != "ready"
            or task.active_attempt_id is not None
            or not scope_contains(case.scope, task.scope)
            or not task.completion_conditions
            or not task.allowed_tools
            or not set(task.allowed_tools) <= set(TOOL_NAMES)
            or len(set(task.allowed_tools)) != len(task.allowed_tools)
            or any(t.task_id == task.task_id for t in case.tasks)
        ):
            return "invalid task contract or attempt identity"
        return None
    attempt_id = (
        payload.finding.attempt_id if isinstance(payload, SubmitFinding) else payload.attempt_id
    )
    attempt = next((a for a in case.attempts if a.attempt_id == attempt_id), None)
    if (
        attempt is None
        or attempt.status != "running"
        or attempt.execution_token != payload.execution_token
    ):
        return "inactive or mismatched execution identity"
    if isinstance(payload, FinishAttempt):
        return (
            None
            if payload.status in {"failed", "cancelled", "interrupted", "unknown"}
            else "invalid failure status"
        )
    task = next(t for t in case.tasks if t.task_id == attempt.task_id)
    if (
        task.contract_version != attempt.contract_version
        or task.active_attempt_id != attempt.attempt_id
    ):
        return "attempt contract was superseded"
    finding = payload.finding
    if task.kind == "review":
        return "review tasks must submit RecordReview, not investigator Findings"
    if task.kind == "diagnose" and not any(
        c.judgment == "diagnosis" for c in finding.proposed_claims
    ):
        return "diagnose tasks require a diagnosis claim for independent review"
    if (
        finding.case_id != case.case_id
        or finding.scope != task.scope
        or finding.source.kind != "runtime"
        or finding.source.reference != attempt.attempt_id
        or finding.proposed_changes
    ):
        return "Finding identity, scope, provenance or unsupported state changes"
    allowed = set(attempt.basis) | set(attempt.reads)
    allowed.update(
        VersionRef(
            case_id=case.case_id, kind="evidence", object_id=e.evidence_id, version=e.version
        )
        for e in case.observations
        if e.attempt_id == attempt.attempt_id
    )
    refs = (*finding.observations, *finding.basis, *finding.counterevidence)
    for claim in finding.proposed_claims:
        previous = next((c for c in case.claims if c.claim_id == claim.claim_id), None)
        if (
            claim.case_id != case.case_id
            or claim.version != (previous.version + 1 if previous else 1)
            or not scope_contains(task.scope, claim.scope)
            or claim.validity != "needs_review"
            or claim.source != finding.source
            or not claim.support
            or any(r.object_id == claim.claim_id for r in claim.premises)
            or (
                previous is not None
                and (
                    task.kind != "repair"
                    or not any(
                        i.review_id == task.review_id and i.target.object_id == previous.claim_id
                        for i in case.review_issues
                    )
                )
            )
        ):
            return "claim must be a new, supported candidate awaiting review"
        refs += (*claim.support, *claim.opposition, *claim.premises)
    if any(ref not in allowed for ref in refs):
        return "Finding references material outside the attempt's authorized reads"
    if any(ref.kind != "evidence" for ref in (*finding.observations, *finding.counterevidence)):
        return "observation and counterevidence references must be evidence"
    if len({c.claim_id for c in finding.proposed_claims}) != len(finding.proposed_claims):
        return "duplicate claim identity"
    if not set(finding.satisfied_conditions) <= set(task.condition_ids):
        return "unknown completion condition"
    if finding.completion == "blocked" and finding.blocker is None:
        return (
            "blocked Finding requires a specific external condition, user "
            "input or execution obstacle"
        )
    if finding.completion == "complete" and finding.blocker is not None:
        return "complete Finding cannot retain an unresolved blocker"
    if finding.completion == "complete" and (
        set(finding.satisfied_conditions) != set(task.condition_ids) or not finding.observations
    ):
        return "complete Finding requires all contract conditions and registered observations"
    if finding.completion == "complete" and any(
        e.result in {"failed", "unknown"}
        for e in case.observations
        if any(ref.object_id == e.evidence_id for ref in finding.observations)
    ):
        return "failed or unknown observations cannot establish task completion"
    expected_targets = {r.target.object_id for r in review_issues(finding)}
    if payload.reviews != review_issues(finding):
        return "review issues must use the canonical target, scope and dependency identity"
    if {r.target.object_id for r in payload.reviews} != expected_targets or any(
        r.case_id != case.case_id
        or r.target.kind != "claim"
        or not any(
            c.claim_id == r.target.object_id and c.version == r.target.version
            for c in finding.proposed_claims
        )
        or r.target.case_id != case.case_id
        or r.status != "open"
        for r in payload.reviews
    ):
        return "candidate claims require open review issues"
    return None


def review_issues(finding: Finding) -> tuple[ReviewIssue, ...]:
    from tau_incident.review import ReviewPolicy, issue_for

    return tuple(
        issue_for(claim, problem="conflict" if claim.opposition else "important_conclusion")
        for claim in finding.proposed_claims
        if ReviewPolicy.required(claim)
    )


class SubmissionService:
    def __init__(self, runtime: IncidentRuntime) -> None:
        self.runtime = runtime

    def submit(self, finding: Finding, *, execution_token: str) -> Receipt:
        # Stable IDs allow retries, including lookup after an uncertain COMMIT.
        reviews = review_issues(finding)
        return self.runtime.execute(
            Command(
                command_id=f"finding:{finding.finding_id}",
                case_id=finding.case_id,
                payload=SubmitFinding(
                    scope=finding.scope,
                    finding=finding,
                    execution_token=execution_token,
                    reviews=reviews,
                ),
            )
        )
