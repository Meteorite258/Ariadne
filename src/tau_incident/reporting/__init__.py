"""Deterministic views of committed manual input; no diagnostic inference."""

from __future__ import annotations

from typing import Literal

from tau_incident.models import (
    CandidateExplanation,
    ExecutionConstraint,
    IncidentCase,
    Model,
    VersionRef,
)
from tau_incident.reporting.builder import ReportBuilder as ReportBuilder
from tau_incident.reporting.builder import render_report as render_report


class CaseBrief(Model):
    case: IncidentCase
    claims: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    waits: tuple[str, ...] = ()
    unresolved: tuple[str, ...]


class ProgressReport(Model):
    kind: Literal["progress"] = "progress"
    verification: Literal["unverified"] = "unverified"
    case_id: str
    case_version: int
    brief: CaseBrief
    basis: tuple[VersionRef, ...]
    markdown: str


def case_brief(case: IncidentCase) -> CaseBrief:
    unresolved = ["Final diagnosis and investigation quality remain unverified."]
    if not case.findings:
        unresolved.append("No automated Finding has been accepted.")
    for finding in case.findings:
        unresolved.extend(finding.gaps)
        unresolved.extend(finding.new_questions)
    unresolved.extend(
        f"Review {r.review_id}: {r.gap}" for r in case.review_issues if r.status != "resolved"
    )
    if case.impact_status == "unknown":
        unresolved.append("Business impact and recovery have not been verified.")
    if not case.observations:
        unresolved.append("No observations have been registered.")
    for observation in case.observations:
        if observation.result != "complete" or observation.actual_coverage is None:
            coverage = "unknown" if observation.actual_coverage is None else "declared by source"
            unresolved.append(
                f"Evidence {observation.evidence_id}: result={observation.result}; "
                f"coverage={coverage}."
            )
    if case.candidate_explanations:
        unresolved.append("Human candidate explanations are unverified inputs.")
    if case.constraints:
        unresolved.append("Execution constraints apply to the investigator's task selection.")
    return CaseBrief(
        case=case,
        unresolved=tuple(unresolved),
        claims=tuple(f"{c.claim_id}: {c.statement} [{c.validity}]" for c in case.claims),
        tasks=tuple(f"{t.task_id}: {t.goal} [{t.status}]" for t in case.tasks),
        waits=tuple(
            f"{w.wait_id}: {w.status}; next={w.next_check_at.isoformat()}; "
            f"deadline={w.deadline.isoformat()}; timeout={w.on_timeout}"
            for w in case.waits
        ),
    )


def progress_report(case: IncidentCase) -> ProgressReport:
    brief = case_brief(case)
    lines = [
        f"# Incident {case.case_id}",
        "",
        f"Case version: {case.version}",
        f"Environment: {case.scope.environment}",
        f"Entities: {', '.join(case.scope.entities) or 'unspecified'}",
        f"Incident window: {case.scope.time_window.model_dump_json()}",
        f"Investigation: {case.investigation_status}; business impact: {case.impact_status}",
        f"Recorded through: {case.updated_at.isoformat()}",
        f"Source: {case.source.kind}/{case.source.actor}; "
        f"reference={case.source.reference or 'unspecified'}",
        "",
        "## Symptoms and reported impact",
        "",
        case.symptoms,
        "",
        case.impact,
        "",
        "## Observations",
        "",
    ]
    basis = [
        VersionRef(case_id=case.case_id, kind="case", object_id=case.case_id, version=case.version)
    ]
    for observation in case.observations:
        coverage = (
            observation.actual_coverage.model_dump_json()
            if observation.actual_coverage
            else "unknown"
        )
        lines.extend(
            [
                f"- {observation.evidence_id}@{observation.version}: {observation.summary}",
                f"  Source: {observation.source.kind}/{observation.source.actor}; "
                f"result: {observation.result}; collected: {observation.collected_at.isoformat()}",
                f"  Scope: {observation.scope.model_dump_json()}",
                f"  Actual coverage: {coverage}",
                f"  Artifact: {observation.artifact.artifact_id}; "
                f"operation: {observation.source_operation_id}",
            ]
        )
        basis.append(
            VersionRef(
                case_id=case.case_id,
                kind="evidence",
                object_id=observation.evidence_id,
                version=observation.version,
            )
        )
    if not case.observations:
        lines.append("No observations registered.")
    manual_inputs: tuple[CandidateExplanation | ExecutionConstraint, ...] = (
        *case.candidate_explanations,
        *case.constraints,
    )
    basis.extend(
        VersionRef(
            case_id=case.case_id, kind="input", object_id=item.input_id, version=item.version
        )
        for item in manual_inputs
    )
    lines.extend(["", "## Candidate explanations (unverified)", ""])
    lines.extend(
        f"- {item.text} (source: {item.source.actor}, input: {item.input_id})"
        for item in case.candidate_explanations
    )
    lines.extend(["", "## Execution constraints", ""])
    lines.extend(
        f"- {item.text} (source: {item.source.actor}, input: {item.input_id})"
        for item in case.constraints
    )
    lines.extend(["", "## Tasks and Findings", ""])
    for task in case.tasks:
        lines.append(f"- Task {task.task_id}@{task.contract_version}: {task.goal} [{task.status}]")
        for attempt in case.attempts:
            if attempt.task_id == task.task_id:
                lines.append(
                    f"  Attempt {attempt.attempt_id}: {attempt.status}; "
                    f"reason={attempt.cancellation_reason or 'none'}"
                )
                for finding in case.findings:
                    if finding.attempt_id == attempt.attempt_id:
                        lines.append(
                            f"  Finding {finding.finding_id}: {finding.completion}; "
                            f"evidence={','.join(r.object_id for r in finding.observations)}"
                        )
    lines.extend(["", "## Claims awaiting review", "", *(f"- {c}" for c in brief.claims)])
    lines.extend(["", "## Decisions", "", *(f"- {d.choice}: {d.reason}" for d in case.decisions)])
    lines.extend(["", "## Wait conditions", "", *(f"- {w}" for w in brief.waits)])
    lines.extend(["", "## Unresolved", "", *(f"- {item}" for item in brief.unresolved), ""])
    return ProgressReport(
        case_id=case.case_id,
        case_version=case.version,
        brief=brief,
        basis=tuple(basis),
        markdown="\n".join(lines),
    )
