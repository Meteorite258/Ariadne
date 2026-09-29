"""Deterministic quality commands and derived invalidation inside the case transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tau_incident.events import (
    CommitMemory,
    CommitReport,
    DomainEvent,
    Lifecycle,
    RecordReview,
    RecordReviews,
    ReopenCase,
    ReviseEvidence,
    ScheduleReview,
    SetImpact,
    SubmitFinding,
    WithdrawReport,
)
from tau_incident.models import IncidentCase, InvestigationTask, TaskAttempt, VersionRef

if TYPE_CHECKING:
    from tau_incident.events import Command
    from tau_incident.store import CaseStore

QUALITY_COMMANDS = (
    RecordReview,
    RecordReviews,
    ScheduleReview,
    ReviseEvidence,
    CommitReport,
    CommitMemory,
    SetImpact,
    ReopenCase,
    WithdrawReport,
)


def ref(case: IncidentCase, kind: str, identity: str, version: int) -> VersionRef:
    return VersionRef.model_validate(
        dict(
            case_id=case.case_id,
            kind=kind,
            object_id=identity,
            version=version,
        )
    )


def applicable(case: IncidentCase, reference: VersionRef) -> bool:
    if reference.case_id != case.case_id:
        return False
    if reference.kind == "evidence":
        changes = [c for c in case.evidence_changes if c.evidence == reference]
        evidence = next(
            (
                e
                for e in case.observations
                if e.evidence_id == reference.object_id and e.version == reference.version
            ),
            None,
        )
        return (
            evidence is not None
            and (not changes or changes[-1].applicable)
            and all(applicable(case, r) for r in evidence.inputs)
        )
    if reference.kind == "claim":
        return any(
            c.claim_id == reference.object_id
            and c.version == reference.version
            and c.validity == "current"
            for c in case.claims
        )
    return True


def validate_quality(
    store: CaseStore, command: Command, case: IncidentCase, *, batch: bool = False
) -> str | None:
    from tau_incident.store.control import require_owner
    from tau_incident.submission import scope_contains

    p = command.payload
    require_owner(store, case.case_id, command.owner_id, command.owner_generation)
    if not isinstance(p, QUALITY_COMMANDS):
        return "unsupported quality command"
    if not scope_contains(case.scope, p.scope):
        return "quality command scope exceeds case"
    if isinstance(p, RecordReviews):
        identities = {item.issue.review_id for item in p.reviews}
        attempts = {item.attempt_id for item in p.reviews}
        if len(identities) != len(p.reviews) or len(attempts) != 1:
            return "review batch requires unique issues and one attempt"
        attempt = next((a for a in case.attempts if a.attempt_id in attempts), None)
        task = next((t for t in case.tasks if attempt and t.task_id == attempt.task_id), None)
        expected = {
            i.review_id
            for i in case.review_issues
            if task and i.target in task.related_claims and i.status == "open"
        }
        if identities != expected:
            return "review batch must address exactly the assigned judgment revisions"
        for item in p.reviews:
            error = validate_quality(
                store, command.model_copy(update={"payload": item}), case, batch=True
            )
            if error:
                return error
        return None
    if isinstance(p, RecordReview):
        a = next((a for a in case.attempts if a.attempt_id == p.attempt_id), None)
        t = next((t for t in case.tasks if a and t.task_id == a.task_id), None)
        old = next((i for i in case.review_issues if i.review_id == p.issue.review_id), None)
        if (
            a is None
            or t is None
            or old is None
            or a.status != "running"
            or a.execution_token != p.execution_token
            or a.runtime_generation != command.owner_generation
            or t.active_attempt_id != a.attempt_id
            or t.contract_version != a.contract_version
            or t.kind != "review"
            or (not batch and t.review_id != old.review_id)
            or (batch and old.target not in t.related_claims)
            or (t.review_id == old.review_id and t.review_version != old.version)
            or case.investigation_status in {"paused", "completed"}
        ):
            return "review attempt no longer owns its issue and contract"
        if not batch and len(t.related_claims) > 1:
            return "assigned group requires one atomic RecordReviews submission"
        if (
            store._connection.execute(
                "SELECT 1 FROM attempt_reservations WHERE attempt_id=? AND state='reserved'",
                (a.attempt_id,),
            ).fetchone()
            is None
        ):
            return "review has no execution reservation"
        if store._connection.execute(
            "SELECT 1 FROM request_usage WHERE attempt_id=? AND state='reserved'", (a.attempt_id,)
        ).fetchone():
            return "review has unsettled requests"
        i = p.issue
        if (
            i.version != old.version + 1
            or i.target != old.target
            or i.scope != old.scope
            or i.case_id != case.case_id
            or i.cycle != old.cycle
            or i.repair_rounds != old.repair_rounds
            or i.max_repairs != old.max_repairs
            or i.attempt_id != a.attempt_id
            or not i.assessment
            or i.disposition not in {"accepted", "repair", "unresolved"}
            or (i.status == "resolved") != (i.disposition == "accepted")
            or i.checked_basis != p.basis
        ):
            return "invalid review revision or disposition"
        authorized = set(a.basis) | set(a.reads)
        authorized.update(
            ref(case, "evidence", e.evidence_id, e.version)
            for e in case.observations
            if e.attempt_id == a.attempt_id
        )
        if not p.basis or any(r not in authorized for r in p.basis):
            return "review basis was not supplied to this attempt"
        error = store._version_error(case.case_id, tuple(dict.fromkeys((*p.basis, old.target))))
        if error:
            return error
        # Recheck relevant delivered premises, while allowing unrelated concurrent revisions.
        from tau_incident.context import relevant

        original = store.case_at_version(case.case_id, a.starting_case_version)
        target_before = next(
            (c for c in original.claims if c.claim_id == old.target.object_id), None
        )
        critical_claims = {old.target.object_id}
        if target_before:
            critical_claims.update(r.object_id for r in target_before.premises)
        relevant_reads = tuple(
            r
            for r in a.basis
            if (r.kind == "review" and r.object_id == old.review_id)
            or r.kind == "input"
            or (r.kind == "claim" and r.object_id in critical_claims)
            or (
                r.kind == "evidence"
                and any(
                    e.evidence_id == r.object_id and relevant(e.scope, i.scope)
                    for e in original.observations
                )
            )
        )
        read_error = store._version_error(case.case_id, tuple(dict.fromkeys(relevant_reads)))
        if read_error:
            return read_error
        # New relevant observations and changed constraints require a fresh review request.
        seen = {r.object_id for r in p.basis if r.kind == "evidence"}
        if any(
            e not in original.observations
            and e.attempt_id != a.attempt_id
            and relevant(e.scope, i.scope)
            and e.evidence_id not in seen
            for e in case.observations
        ):
            return "new relevant evidence arrived during review"
        if (
            original.constraints != case.constraints
            or original.evidence_changes != case.evidence_changes
        ):
            return "review premises changed during execution"
        target = next((c for c in case.claims if c.claim_id == i.target.object_id), None)
        if i.disposition == "accepted" and target is not None:
            mandatory = {
                r
                for r in (*target.support, *target.opposition, *target.premises)
                if store._version_error(case.case_id, (r,)) is None
            }
            if not mandatory <= set(p.basis):
                return "accepted review must address support, counterevidence and premises"
            if target.judgment == "diagnosis" and i.judgment_action == "retain":
                diagnosis_inputs = {
                    ref(case, "evidence", e.evidence_id, e.version)
                    for e in case.observations
                    if relevant(e.scope, target.scope)
                }
                diagnosis_inputs.update(
                    ref(case, "task", t.task_id, t.contract_version)
                    for t in case.tasks
                    if t.nonblocking_reason
                )
                if not diagnosis_inputs <= set(p.basis):
                    return "diagnosis review must address all scoped observations and gaps"
            if p.claim is None or p.claim != target.model_copy(
                update={
                    "version": target.version + 1,
                    "validity": "withdrawn" if i.judgment_action == "withdraw" else "current",
                    "revision_reason": i.assessment,
                }
            ):
                return "acceptance must revise the exact reviewed claim"
            if i.judgment_action == "retain" and (
                not target.support
                or not all(applicable(case, r) for r in (*target.support, *target.premises))
            ):
                return "accepted claim has unavailable support or unreviewed premises"
        elif p.claim is not None:
            return "only an accepted claim review may promote a judgment"
        if i.disposition == "accepted" and target is None and i.target.kind == "task":
            target_task = next((t for t in case.tasks if t.task_id == i.target.object_id), None)
            if target_task is None or not any(r.kind == "evidence" for r in p.basis):
                return "task review acceptance requires current observations"
            if set(p.satisfied_conditions) != set(target_task.condition_ids):
                return "accepted task review must address every original completion condition"
            if not any(
                r.kind == "evidence"
                and applicable(case, r)
                and any(
                    e.evidence_id == r.object_id and e.result not in {"failed", "unknown"}
                    for e in case.observations
                )
                for r in p.basis
            ):
                return "task review requires usable evidence"
        return None
    if isinstance(p, ScheduleReview):
        old = next((i for i in case.review_issues if i.review_id == p.issue.review_id), None)
        i = p.issue
        if i.case_id != case.case_id or not scope_contains(case.scope, i.scope):
            return "review issue scope or identity mismatch"
        if old is None or i.version != old.version + 1:
            return "schedule requires the next existing issue revision"
        if i.status == "resolved" or i.disposition in {"accepted", "superseded"}:
            return "scheduling cannot accept a review"
        if i.cycle == old.cycle and i.repair_rounds < old.repair_rounds:
            return "scheduling cannot reset repair rounds within a cycle"
        if i.cycle == old.cycle and i.status == "open" and old.status == "unresolved":
            return "an unresolved issue requires a new review cycle"
        if old.status == "resolved":
            return "resolved issues are immutable; review a new target revision"
        if i.target != old.target or i.max_repairs != old.max_repairs:
            return "schedule cannot replace the issue identity or policy"
        if any(
            t.review_id == i.review_id and t.status in {"ready", "running", "waiting"}
            for t in case.tasks
        ):
            return "issue already has work in flight"
        if i.cycle == old.cycle + 1:
            fresh = set(i.basis) - set(old.basis)
            from tau_incident.context import relevant

            if any(
                r.kind != "evidence"
                or not any(
                    e.evidence_id == r.object_id and relevant(e.scope, i.scope)
                    for e in case.observations
                )
                for r in fresh
            ):
                return "new cycles require relevant current evidence"
            if not fresh and (not i.new_check or i.new_check == old.new_check):
                return "new cycle requires new relevant evidence or an explicit new check"
            if i.repair_rounds != 0:
                return "new cycle must reset repair count"
        elif i.cycle != old.cycle:
            return "invalid review cycle"
        task = p.task
        if task is not None:
            if (
                task.case_id != case.case_id
                or task.contract_version != 1
                or task.status != "ready"
                or task.active_attempt_id is not None
                or task.review_id != i.review_id
                or task.review_version != i.version
                or task.review_cycle != i.cycle
                or task.kind not in {"review", "repair"}
                or not scope_contains(case.scope, task.scope)
                or any(t.task_id == task.task_id for t in case.tasks)
                or not task.completion_conditions
                or not task.allowed_tools
                or task.rationale != (i.target,)
            ):
                return "invalid quality task contract"
            from tau_incident.review import related_review_targets
            from tau_incident.telemetry.tools import TOOL_NAMES

            if task.kind == "review" and task.related_claims != related_review_targets(case, old):
                return "quality group differs from the current related judgment assignments"

            if not set(task.allowed_tools) <= set(TOOL_NAMES):
                return "unknown quality task capability"
            if task.kind == "repair" and (
                i.repair_rounds != old.repair_rounds + 1 or i.repair_rounds > i.max_repairs
            ):
                return "repair round limit or sequence violated"
            if task.kind == "review" and i.repair_rounds != (
                0 if i.cycle > old.cycle else old.repair_rounds
            ):
                return "review cannot consume/reset repair rounds"
        return store._version_error(case.case_id, tuple(dict.fromkeys(i.basis)))
    if isinstance(p, ReviseEvidence):
        change = p.change
        old_changes = [c for c in case.evidence_changes if c.evidence == change.evidence]
        if change.evidence.kind != "evidence" or change.version != len(old_changes) + 1:
            return "invalid evidence applicability revision"
        return store._version_error(case.case_id, (change.evidence,))
    if isinstance(p, SetImpact):
        if p.status == "recovered" and (
            not p.basis
            or any(
                r.kind != "evidence"
                or not applicable(case, r)
                or not any(
                    e.evidence_id == r.object_id and e.result == "complete"
                    for e in case.observations
                )
                for r in p.basis
            )
        ):
            return "business recovery requires applicable complete observations"
        return store._version_error(case.case_id, p.basis)
    if isinstance(p, CommitReport):
        from tau_incident.context import ContextBuilder

        report = p.report
        previous = case.reports[-1] if case.reports else None
        if (
            report.case_id != case.case_id
            or report.scope != case.scope
            or report.version != (previous.version + 1 if previous else 1)
            or report.report_id != (previous.report_id if previous else f"report:{case.case_id}")
            or report.verification != "unverified"
            or report.state != "current"
            or report.impact_status != case.impact_status
            or report.supersedes
            != (ref(case, "report", previous.report_id, previous.version) if previous else None)
        ):
            return "invalid report identity, version or verification"
        evidence = tuple(ref(case, "evidence", e.evidence_id, e.version) for e in case.observations)
        if report.input_evidence != evidence or report.input_tasks != tuple(
            f"{t.task_id}:{t.contract_version}:{t.status}" for t in case.tasks
        ):
            return "report inputs changed; rebuild report"
        if set(report.basis) != set(ContextBuilder._refs(case)):
            return "report judgments, review issues or manual inputs changed"
        if report.case_version > case.version:
            return "report input version is in the future"
        snapshot = store.case_at_version(case.case_id, report.case_version)
        if snapshot.evidence_changes != case.evidence_changes:
            return "evidence applicability changed since report generation"
        if any(r not in report.basis for r in (*report.support, *report.counterevidence)):
            return "report cites material outside its validated inputs"
        if report.verification_sources:
            return "this implementation cannot certify model-generated causal verification"
        current_claims = tuple(c for c in case.claims if c.validity == "current")
        explanation = "\n".join(f"{c.claim_id}@{c.version}: {c.statement}" for c in current_claims)
        if report.explanation != (
            explanation or "No reviewed diagnosis is available. Investigation remains incomplete."
        ):
            return "report explanations must come from reviewed, versioned judgments"
        if report.propagation != tuple(
            f"{d.reference.object_id}@{d.reference.version} -> {c.claim_id}@{c.version}"
            for c in current_claims
            for d in c.dependencies
            if d.relation == "derived_from"
        ):
            return "report propagation must match explicit judgment dependencies"
        if report.support != tuple(
            dict.fromkeys(r for c in current_claims for r in c.support)
        ) or report.counterevidence != tuple(
            dict.fromkeys(r for c in case.claims for r in c.opposition)
        ):
            return "report must preserve supporting and opposing references"
        error = store._version_error(case.case_id, tuple(dict.fromkeys(report.basis)))
        if error:
            return error
        from tau_incident.readiness import diagnosis_readiness

        readiness = diagnosis_readiness(case)
        if report.kind == "diagnosis" and not readiness.ready:
            return (
                "diagnosis requires reviewed judgments and no outstanding relevant work: "
                + "; ".join(readiness.blockers)
            )
        if report.unresolved != readiness.blockers:
            return "report blockers changed; rebuild report"
        recommendations = readiness.followups + tuple(
            dict.fromkeys(i.required_action for i in case.review_issues if i.status != "resolved")
        )
        if report.recommendations != recommendations:
            return "report follow-ups changed; rebuild report"
        return None
    if isinstance(p, WithdrawReport):
        if p.target.kind != "report":
            return "withdrawal requires a report reference"
        return store._version_error(case.case_id, (p.target,))
    if isinstance(p, CommitMemory):
        card = p.card
        source_report = next(
            (
                r
                for r in case.reports
                if r.report_id == card.source_report.object_id
                and r.version == card.source_report.version
            ),
            None,
        )
        previous_card = case.memories[-1] if case.memories else None
        if (
            source_report is None
            or source_report.state != "current"
            or card.source_report.case_id != case.case_id
            or card.source_report.kind != "report"
            or card.case_id != case.case_id
            or card.project_key != case.project_key
            or card.scope != case.scope
            or card.verification != source_report.verification
            or card.state != "current"
            or card.version != (previous_card.version + 1 if previous_card else 1)
            or card.memory_id != f"memory:{case.case_id}"
            or card.kind != ("diagnosis" if source_report.kind == "diagnosis" else "incomplete")
            or card.summary != source_report.explanation
            or card.evidence_cutoff != source_report.evidence_cutoff
            or card.unresolved != source_report.unresolved
            or card.supersedes
            != (
                ref(case, "memory", previous_card.memory_id, previous_card.version)
                if previous_card
                else None
            )
        ):
            return "memory must derive from the current source report"
        return None
    return None


def reduce_quality(before: IncidentCase, after: IncidentCase, event: DomainEvent) -> IncidentCase:
    p = event.payload
    if isinstance(p, RecordReviews):
        result = after
        for item in p.reviews:
            result = reduce_quality(before, result, event.model_copy(update={"payload": item}))
        return result
    data = after.model_dump()
    claims = list(after.claims)
    issues = list(after.review_issues)
    changed: list[VersionRef] = []
    from tau_incident.events import ObservationAdded

    if isinstance(p, ObservationAdded):
        from tau_incident.context import relevant
        from tau_incident.review import issue_for

        observation = p.observation
        changed.extend(
            ref(before, "evidence", old.evidence_id, old.version)
            for old in before.observations
            if old.source.actor == observation.source.actor
            and old.scope == observation.scope
            and old.source_revision is not None
            and observation.source_revision is not None
            and old.source_revision != observation.source_revision
            and (observation.available_at or observation.collected_at)
            >= (old.available_at or old.collected_at)
        )
        origin = next((a for a in before.attempts if a.attempt_id == observation.attempt_id), None)
        origin_task = next(
            (t for t in before.tasks if origin and t.task_id == origin.task_id), None
        )
        # Scope overlap creates a review candidate, never an automatic invalidation.
        # Quality workers evaluate their own new observations within the assigned issue.
        if origin_task is None or origin_task.kind not in {"review", "repair"}:
            for claim in before.claims:
                if (
                    claim.validity == "current"
                    and claim.judgment == "diagnosis"
                    and relevant(claim.scope, observation.scope)
                ):
                    candidate = issue_for(
                        claim,
                        problem="basis_changed",
                        extra_basis=(
                            ref(after, "evidence", observation.evidence_id, observation.version),
                        ),
                    )
                    issues.append(
                        candidate.model_copy(
                            update={
                                "gap": "New scoped observation: reassess relevance "
                                "before retaining this diagnosis."
                            }
                        )
                    )
    if isinstance(p, ScheduleReview):
        issues = [p.issue if i.review_id == p.issue.review_id else i for i in issues]
        if p.task is not None:
            data["tasks"] = (*after.tasks, p.task)
    elif isinstance(p, RecordReview):
        issues = [p.issue if i.review_id == p.issue.review_id else i for i in issues]
        if p.claim is not None:
            changed.append(p.issue.target)
            claims = [p.claim if c.claim_id == p.claim.claim_id else c for c in claims]
        data["attempts"] = tuple(
            a.model_copy(
                update={
                    "status": "succeeded",
                    "final_command_id": event.command_id,
                    "manifest": a.manifest.model_copy(update={"finding": p.basis}),
                }
            )
            if a.attempt_id == p.attempt_id
            else a
            for a in after.attempts
        )
        data["tasks"] = tuple(
            t.model_copy(update={"status": "completed", "active_attempt_id": None})
            if t.active_attempt_id == p.attempt_id
            or (
                p.issue.disposition == "accepted"
                and p.issue.target.kind == "task"
                and t.task_id == p.issue.target.object_id
                and t.status == "blocked"
            )
            else t
            for t in after.tasks
        )
    elif isinstance(p, ReviseEvidence):
        data["evidence_changes"] = (*after.evidence_changes, p.change)
        changed.append(p.change.evidence)
    elif isinstance(p, CommitReport):
        data["reports"] = (
            *tuple(
                r.model_copy(update={"state": "stale"}) if r.state == "current" else r
                for r in after.reports
            ),
            p.report,
        )
        if p.report.kind == "diagnosis":
            data["investigation_status"] = "completed"
    elif isinstance(p, CommitMemory):
        data["memories"] = (
            *tuple(
                m.model_copy(update={"state": "stale"}) if m.state == "current" else m
                for m in after.memories
            ),
            p.card,
        )
    elif isinstance(p, SetImpact):
        data["impact_status"], data["recovery_basis"] = p.status, p.basis
    elif isinstance(p, ReopenCase):
        data["investigation_status"] = "open"
        data["control_intent"] = "run"
    elif isinstance(p, WithdrawReport):
        data["reports"] = tuple(
            r.model_copy(update={"state": "withdrawn", "verification": "withdrawn"})
            if r.report_id == p.target.object_id and r.version == p.target.version
            else r
            for r in after.reports
        )
        data["memories"] = tuple(
            m.model_copy(update={"state": "withdrawn", "verification": "withdrawn"})
            if m.source_report == p.target
            else m
            for m in after.memories
        )
        data["investigation_status"] = "open"
    if isinstance(p, (ReopenCase, WithdrawReport)):
        from tau_incident.review import issue_for

        for index, claim in enumerate(claims):
            if claim.judgment == "diagnosis" and claim.validity == "current":
                changed.append(ref(before, "claim", claim.claim_id, claim.version))
                revised = claim.model_copy(
                    update={
                        "version": claim.version + 1,
                        "validity": "needs_review",
                        "revision_reason": p.reason,
                    }
                )
                claims[index] = revised
                issues.append(issue_for(revised, problem="basis_changed"))
    if isinstance(p, Lifecycle) and p.action == "budget_stop":
        issues = [
            i.model_copy(
                update={
                    "version": i.version + 1,
                    "status": "unresolved",
                    "disposition": "unresolved",
                    "stop_reason": p.reason,
                }
            )
            if i.status == "open"
            else i
            for i in issues
        ]
        data["tasks"] = tuple(
            t.model_copy(update={"status": "cancelled", "active_attempt_id": None})
            if t.kind in {"review", "repair"} and t.status in {"ready", "running", "waiting"}
            else t
            for t in after.tasks
        )
    if isinstance(p, SubmitFinding):
        old_by_id = {c.claim_id: c for c in before.claims}
        replacements = {c.claim_id: c for c in p.finding.proposed_claims}
        claims = [replacements.get(c.claim_id, c) for c in before.claims]
        claims.extend(c for c in p.finding.proposed_claims if c.claim_id not in old_by_id)
        changed.extend(
            ref(before, "claim", c.claim_id, c.version)
            for c in before.claims
            if c.claim_id in replacements
        )
        attempt = next(a for a in before.attempts if a.attempt_id == p.finding.attempt_id)
        task = next(t for t in before.tasks if t.task_id == attempt.task_id)
        parent_issue = next(
            (i for i in before.review_issues if i.review_id == task.review_id), None
        )
        if task.kind == "repair" and parent_issue is not None:
            new_ids = {i.review_id for i in p.reviews}
            issues = [
                i.model_copy(
                    update={
                        "cycle": parent_issue.cycle,
                        "repair_rounds": parent_issue.repair_rounds,
                        "max_repairs": parent_issue.max_repairs,
                        "repair_task_ids": parent_issue.repair_task_ids,
                    }
                )
                if i.review_id in new_ids
                else i
                for i in issues
            ]
    if changed:
        from tau_incident.review import RepairPlanner, issue_for

        affected = RepairPlanner.affected_claims(before, tuple(changed))
        for index, c in enumerate(claims):
            if (
                c.claim_id in affected
                and c.validity != "withdrawn"
                and not (
                    isinstance(p, RecordReview)
                    and p.claim is not None
                    and p.claim.claim_id == c.claim_id
                )
                and not (
                    isinstance(p, SubmitFinding)
                    and any(
                        proposed.claim_id == c.claim_id for proposed in p.finding.proposed_claims
                    )
                )
            ):
                revised = c.model_copy(
                    update={
                        "version": c.version + 1,
                        "validity": "needs_review",
                        "revision_reason": "An explicit reasoning dependency changed.",
                    }
                )
                claims[index] = revised
                issues.append(issue_for(revised, problem="basis_changed"))
    # Old issues remain auditable in the event log and resolve only as superseded, never verified.
    current_versions = {c.claim_id: c.version for c in claims}
    issues = [
        i.model_copy(
            update={
                "version": i.version + 1,
                "status": "resolved",
                "disposition": "superseded",
                "stop_reason": "target revised",
            }
        )
        if i.status != "resolved"
        and i.target.kind == "claim"
        and current_versions.get(i.target.object_id) != i.target.version
        else i
        for i in issues
    ]
    data["claims"], data["review_issues"] = tuple(claims), tuple(issues)
    previous_issue_ids = {i.review_id for i in before.review_issues}
    previous_limits = {i.target.object_id: i.max_repairs for i in before.review_issues}
    data["review_issues"] = tuple(
        i.model_copy(update={"max_repairs": previous_limits[i.target.object_id]})
        if i.review_id not in previous_issue_ids and i.target.object_id in previous_limits
        else i
        for i in issues
    )
    issue_state = {i.review_id: i for i in issues}
    retired_tasks: set[str] = set()
    tasks = tuple(InvestigationTask.model_validate(t) for t in data["tasks"])
    for task in tasks:
        issue = issue_state.get(task.review_id or "")
        if (
            issue is not None
            and task.kind in {"review", "repair"}
            and task.status in {"ready", "running", "waiting", "blocked"}
            and (issue.status == "resolved" or (task.review_cycle or 1) < issue.cycle)
        ):
            retired_tasks.add(task.task_id)
    if retired_tasks:
        data["tasks"] = tuple(
            t.model_copy(update={"status": "cancelled", "active_attempt_id": None})
            if t.task_id in retired_tasks
            else t
            for t in tasks
        )
        attempts = tuple(TaskAttempt.model_validate(a) for a in data["attempts"])
        data["attempts"] = tuple(
            a.model_copy(
                update={
                    "status": "cancelled",
                    "cancellation_reason": "review target or cycle superseded",
                }
            )
            if a.task_id in retired_tasks and a.status == "running"
            else a
            for a in attempts
        )
    from tau_incident.events import (
        ConstraintAdded,
        DispatchTasks,
        ExplanationAdded,
        FinishAttempt,
        ObservationAdded,
        RecordPlan,
        RecordWait,
        RetryTask,
        ReviseTask,
        SetTaskDisposition,
        UpdateTaskProgress,
    )

    dirty = (
        bool(changed)
        or isinstance(
            p,
            (
                SubmitFinding,
                RecordReview,
                ReviseEvidence,
                ReopenCase,
                SetImpact,
                ObservationAdded,
                ConstraintAdded,
                ExplanationAdded,
                RecordPlan,
                DispatchTasks,
                FinishAttempt,
                ReviseTask,
                UpdateTaskProgress,
                SetTaskDisposition,
                RetryTask,
                RecordWait,
            ),
        )
        or (isinstance(p, ScheduleReview) and p.issue.status != "resolved")
        or (isinstance(p, Lifecycle) and p.action in {"resume", "budget_stop", "cancel"})
    )
    if dirty and not isinstance(p, WithdrawReport):
        data["reports"] = tuple(
            r.model_copy(update={"state": "stale"}) if r.state == "current" else r
            for r in after.reports
        )
    if (dirty or isinstance(p, CommitReport)) and not isinstance(p, WithdrawReport):
        data["memories"] = tuple(
            m.model_copy(update={"state": "stale"}) if m.state == "current" else m
            for m in after.memories
        )
    if dirty and before.investigation_status == "completed":
        data["investigation_status"] = "open"
    return IncidentCase.model_validate(data)


def save_versions(store: CaseStore, case: IncidentCase) -> None:
    for kind, entries in (
        ("claim", case.claims),
        ("review", case.review_issues),
        ("report", case.reports),
        ("memory", case.memories),
    ):
        for entry in entries:
            identity = getattr(entry, f"{kind}_id")
            store._save_version(case.case_id, kind, identity, entry.version)
