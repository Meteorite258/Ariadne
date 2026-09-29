"""Bounded review and local repair using ordinary task attempts and role execution."""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import Field

from tau_incident.events import Command, RecordReview, RecordReviews, ScheduleReview
from tau_incident.models import (
    ClaimRevision,
    IncidentCase,
    InvestigationTask,
    Model,
    ReviewIssue,
    Source,
    TaskAttempt,
    VersionRef,
)
from tau_incident.quality import applicable, ref

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime
    from tau_incident.executor import RoleRunner
    from tau_incident.telemetry import ServiceCatalog, TelemetryProvider


class ReviewPolicy:
    @staticmethod
    def required(claim: ClaimRevision) -> bool:
        return claim.validity in {"proposed", "needs_review"} and (
            claim.judgment in {"diagnosis", "exclusion"} or bool(claim.opposition)
        )


def related_review_targets(case: IncidentCase, issue: ReviewIssue) -> tuple[VersionRef, ...]:
    """Group independent sibling judgments used by one diagnosis, without extra queue state."""
    siblings: set[str] = set()
    for diagnosis in case.claims:
        if diagnosis.judgment == "diagnosis" and issue.target in diagnosis.premises:
            siblings.update(r.object_id for r in diagnosis.premises)
    selected = [issue.target]
    for peer in case.review_issues:
        if (
            peer.review_id == issue.review_id
            or peer.status != "open"
            or peer.disposition != "pending"
            or peer.scope != issue.scope
            or peer.target.object_id not in siblings
        ):
            continue
        if any(
            t.status in {"ready", "running", "waiting"}
            and (t.review_id == peer.review_id or peer.target in t.related_claims)
            for t in case.tasks
        ):
            continue
        claims = [
            c for c in case.claims if c.claim_id in {r.object_id for r in (*selected, peer.target)}
        ]
        if any(r.object_id in {c.claim_id for c in claims} for c in claims for r in c.premises):
            continue
        selected.append(peer.target)
    return tuple(selected)


def issue_for(
    claim: ClaimRevision,
    *,
    problem: Literal[
        "important_conclusion", "conflict", "basis_changed", "repair"
    ] = "important_conclusion",
    extra_basis: tuple[VersionRef, ...] = (),
) -> ReviewIssue:
    target = VersionRef(
        case_id=claim.case_id, kind="claim", object_id=claim.claim_id, version=claim.version
    )
    basis = tuple(dict.fromkeys((*claim.support, *claim.opposition, *claim.premises, *extra_basis)))
    key = "|".join(
        (
            target.model_dump_json(),
            problem,
            claim.scope.model_dump_json(),
            *(r.model_dump_json() for r in sorted(basis, key=lambda r: r.model_dump_json())),
        )
    )
    return ReviewIssue(
        review_id=hashlib.sha256(key.encode()).hexdigest(),
        case_id=claim.case_id,
        version=1,
        scope=claim.scope,
        source=Source(kind="runtime", actor="review_policy"),
        target=target,
        gap="Check reasoning, counterevidence, coverage and competing explanations.",
        impact="This judgment cannot establish a final diagnosis before review.",
        required_action="Inspect the cited evidence and name any concrete correction needed.",
        status="open",
        problem=problem,
        basis=basis,
    )


class RepairPlanner:
    @staticmethod
    def affected_claims(case: IncidentCase, changed: tuple[VersionRef, ...]) -> set[str]:
        """Follow reasoning edges only. Task rationale never invalidates observations."""
        frontier = set(changed)
        affected: set[str] = set()
        while True:
            size = len(frontier)
            frontier.update(
                ref(case, "evidence", e.evidence_id, e.version)
                for e in case.observations
                if any(r in frontier for r in e.inputs)
            )
            found = [
                c
                for c in case.claims
                if c.claim_id not in affected
                and any(d.reference in frontier for d in c.dependencies)
            ]
            if not found and len(frontier) == size:
                return affected
            for c in found:
                affected.add(c.claim_id)
                frontier.add(ref(case, "claim", c.claim_id, c.version))

    @staticmethod
    def candidates(case: IncidentCase, issue: ReviewIssue) -> tuple[ClaimRevision, ...]:
        from tau_incident.context import relevant

        return tuple(c for c in case.claims if relevant(c.scope, issue.scope))

    @staticmethod
    def independent_support(
        case: IncidentCase, claim: ClaimRevision, changed: tuple[VersionRef, ...]
    ) -> tuple[VersionRef, ...]:
        return tuple(r for r in claim.support if r not in changed and applicable(case, r))


class ReviewOutput(Model):
    disposition: Literal["accepted", "repair", "unresolved"]
    assessment: str = Field(min_length=1)
    gap: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    basis: tuple[VersionRef, ...] = Field(min_length=1)
    new_check: str | None = None
    judgment_action: Literal["retain", "withdraw"] = "retain"
    satisfied_conditions: tuple[str, ...] = ()


class ReviewBatchOutput(Model):
    reviews: dict[str, ReviewOutput] = Field(
        description="One independent result per supplied review_id; address every assigned issue."
    )


class Reviewer:
    def __init__(
        self,
        runtime: IncidentRuntime,
        runner: RoleRunner,
        telemetry: TelemetryProvider,
        catalog: ServiceCatalog,
    ) -> None:
        self.runtime, self.runner, self.telemetry, self.catalog = (
            runtime,
            runner,
            telemetry,
            catalog,
        )

    async def review(self, attempt: TaskAttempt) -> RecordReview | RecordReviews:
        from tau_incident.executor import OutputInvalid
        from tau_incident.telemetry.tools import worker_tools

        view = self.runner.builder.build_task(attempt)
        case = self.runtime.store.case_at_version(attempt.case_id, attempt.starting_case_version)
        issue = next(i for i in case.review_issues if i.review_id == view.task.review_id)
        assigned = tuple(
            i
            for i in case.review_issues
            if i.status == "open" and i.target in view.task.related_claims
        ) or (issue,)
        candidates = RepairPlanner.candidates(case, issue)
        support_notes = "\n".join(
            f"Candidate {c.claim_id}: independently applicable support "
            + ", ".join(r.object_id for r in RepairPlanner.independent_support(case, c, ()))
            for c in candidates
        )
        operation = self.runtime.execution.start(
            "review",
            case_id=case.case_id,
            command_id=uuid4().hex,
            attempt_id=attempt.attempt_id,
            task_id=attempt.task_id,
            trace_id=attempt.trace_id,
            runtime_generation=attempt.runtime_generation,
        )
        if attempt.dispatch_operation_id:
            self.runtime.execution.link(
                operation.operation_id, attempt.dispatch_operation_id, "dispatched_by"
            )
        target_claim = next((c for c in case.claims if c.claim_id == issue.target.object_id), None)
        if target_claim and target_claim.source.reference:
            origins = self.runtime.store.executions(
                attempt_id=target_claim.source.reference, operation_kind="investigation", limit=1
            )
            if origins:
                self.runtime.execution.link(
                    operation.operation_id, origins[0].operation_id, "reviews_judgment_from"
                )
        self.runtime.store.update_execution(
            operation.operation_id,
            lambda r: r.model_copy(
                update={
                    "references": (
                        issue.target,
                        ref(case, "review", issue.review_id, issue.version),
                    )
                }
            ),
        )

        def materialize(output: ReviewOutput, target_issue: ReviewIssue = issue) -> RecordReview:
            issue = target_issue
            accepted = output.disposition == "accepted"
            revised = issue.model_copy(
                update={
                    "version": issue.version + 1,
                    "status": "resolved"
                    if accepted
                    else "open"
                    if output.disposition == "repair"
                    else "unresolved",
                    "disposition": output.disposition,
                    "assessment": output.assessment,
                    "gap": output.gap,
                    "impact": output.impact,
                    "required_action": output.required_action,
                    "checked_basis": output.basis,
                    "attempt_id": attempt.attempt_id,
                    "new_check": output.new_check,
                    "judgment_action": output.judgment_action,
                    "stop_reason": output.assessment
                    if output.disposition == "unresolved"
                    else None,
                }
            )
            target = next((c for c in case.claims if c.claim_id == issue.target.object_id), None)
            claim = (
                target.model_copy(
                    update={
                        "version": target.version + 1,
                        "validity": "withdrawn"
                        if output.judgment_action == "withdraw"
                        else "current",
                        "revision_reason": output.assessment,
                    }
                )
                if accepted and target
                else None
            )
            return RecordReview(
                scope=view.task.scope,
                issue=revised,
                attempt_id=attempt.attempt_id,
                execution_token=attempt.execution_token,
                basis=output.basis,
                claim=claim,
                satisfied_conditions=output.satisfied_conditions,
            )

        def materialize_result(output: Model) -> RecordReview | RecordReviews:
            if isinstance(output, ReviewOutput):
                return materialize(output)
            if not isinstance(output, ReviewBatchOutput) or set(output.reviews) != {
                i.review_id for i in assigned
            }:
                raise OutputInvalid("review batch must address every assigned issue exactly once")
            return RecordReviews(
                scope=view.task.scope,
                reviews=tuple(materialize(output.reviews[i.review_id], i) for i in assigned),
            )

        def validate(output: Model) -> None:
            from tau_incident.quality import validate_quality

            error = validate_quality(
                self.runtime.store,
                Command(
                    command_id=operation.command_id,
                    case_id=case.case_id,
                    payload=materialize_result(output),
                    owner_id=self.runner.budget.owner_id,
                    owner_generation=attempt.runtime_generation,
                ),
                self.runtime.get_case(case.case_id),
            )
            if error:
                raise OutputInvalid(error)

        try:
            fatal_errors: list[Exception] = []
            output = await self.runner.run(
                ReviewBatchOutput if len(assigned) > 1 else ReviewOutput,
                view=view,
                parent=operation,
                tools=worker_tools(
                    self.runtime, attempt, operation, self.telemetry, self.catalog, fatal_errors
                ),
                instructions="Review the assigned issue: "
                + "\n".join(i.model_dump_json() for i in assigned)
                + "\nScope candidates (overlap is not an invalidation):\n"
                + support_notes
                + " Inspect raw evidence if needed. Acceptance assesses reasoning, "
                "never verifies causality. Name a concrete gap, impact and executable correction. "
                "Do not request another reviewer. "
                "For independent support consider narrowing, supplementary checks or replacement; "
                "scope overlap alone does not invalidate a judgment.",
                validate=validate,
                fatal_errors=fatal_errors,
            )
            result = materialize_result(output)
            self.runtime.execution.finish(
                operation.operation_id, status="succeeded", result="review_proposed"
            )
            return result
        except BaseException as exc:
            self.runtime.execution.finish(
                operation.operation_id,
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                result="review_failed",
                detail=str(exc),
                error_category="review",
            )
            raise


def schedule(runtime: IncidentRuntime, case_id: str) -> bool:
    """One durable scheduling decision per pass; all model work runs through normal dispatch."""
    from tau_incident.context import relevant
    from tau_incident.telemetry.tools import TOOL_NAMES

    case = runtime.get_case(case_id)
    for issue in case.review_issues:
        if issue.status == "resolved":
            continue
        target = next((c for c in case.claims if c.claim_id == issue.target.object_id), None)
        stale_premises = (
            ()
            if target is None
            else tuple(
                reference for reference in target.premises if not applicable(case, reference)
            )
        )
        if (
            target is not None
            and target.judgment == "diagnosis"
            and any(
                not any(
                    c.claim_id == reference.object_id and c.validity == "current"
                    for c in case.claims
                )
                for reference in stale_premises
            )
        ):
            # Review dependent judgments before their synthesis, so accepting a
            # premise cannot immediately invalidate a concurrent diagnosis review.
            continue
        if any(
            t.review_id != issue.review_id
            and issue.target in t.related_claims
            and t.kind == "review"
            and t.status in {"ready", "running", "waiting"}
            for t in case.tasks
        ):
            continue
        tasks = [
            t
            for t in case.tasks
            if t.review_id == issue.review_id and t.review_cycle == issue.cycle
        ]
        if any(t.status in {"ready", "running", "waiting"} for t in tasks):
            continue
        scoped_evidence = tuple(
            ref(case, "evidence", e.evidence_id, e.version)
            for e in case.observations
            if relevant(e.scope, issue.scope)
            and applicable(case, ref(case, "evidence", e.evidence_id, e.version))
        )
        basis = tuple(
            dict.fromkeys(
                (
                    *(
                        r
                        for r in issue.basis
                        if runtime.store._version_error(case_id, (r,)) is None
                    ),
                    *scoped_evidence,
                    *(
                        ref(case, "task", t.task_id, t.contract_version)
                        for t in case.tasks
                        if t.nonblocking_reason
                        and target is not None
                        and target.judgment == "diagnosis"
                    ),
                )
            )
        )
        updated = issue.model_copy(update={"version": issue.version + 1})
        fresh = set(basis) - set(issue.basis) - set(issue.checked_basis)
        if issue.status == "unresolved":
            if not fresh:
                continue
            updated = updated.model_copy(
                update={
                    "cycle": issue.cycle + 1,
                    "repair_rounds": 0,
                    "status": "open",
                    "disposition": "pending",
                    "basis": basis,
                    "stop_reason": None,
                }
            )
            tasks = []
        last = tasks[-1] if tasks else None
        kind: Literal["review", "repair"] = "review"
        stop_reason = None
        if last and last.kind == "repair":
            findings = [
                f
                for f in case.findings
                if any(
                    a.attempt_id == f.attempt_id and a.task_id == last.task_id
                    for a in case.attempts
                )
            ]
            if not fresh and not any(f.proposed_claims for f in findings):
                stop_reason = (
                    "repair made no progress: no new relevant evidence or revised judgment"
                )
        elif issue.disposition == "repair" or stale_premises:
            kind = "repair"
            if issue.repair_rounds >= issue.max_repairs:
                stop_reason = "repair round limit reached"
        elif last and last.status in {"blocked", "cancelled"}:
            stop_reason = "quality execution failed; explicit new check or new evidence required"
        task = None
        if stop_reason:
            updated = updated.model_copy(
                update={
                    "status": "unresolved",
                    "disposition": "unresolved",
                    "stop_reason": stop_reason,
                    "basis": basis,
                }
            )
        else:
            tid = uuid4().hex
            updated = updated.model_copy(
                update={
                    "basis": basis,
                    "repair_rounds": updated.repair_rounds + (kind == "repair"),
                    "repair_task_ids": (*issue.repair_task_ids, tid)
                    if kind == "repair"
                    else issue.repair_task_ids,
                }
            )
            task = InvestigationTask(
                task_id=tid,
                case_id=case_id,
                contract_version=1,
                kind=kind,
                goal=issue.required_action
                if kind == "repair"
                else "Review judgment and its explicit gaps.",
                scope=issue.scope,
                source=Source(kind="runtime", actor="quality_scheduler"),
                completion_conditions=("Address the assigned issue with cited current evidence.",),
                allowed_tools=TOOL_NAMES,
                rationale=(issue.target,),
                related_claims=related_review_targets(case, issue)
                if kind == "review"
                else (issue.target,),
                review_id=issue.review_id,
                review_version=updated.version,
                review_cycle=updated.cycle,
            )
        receipt = runtime.execute(
            Command(
                command_id=uuid4().hex,
                case_id=case_id,
                payload=ScheduleReview(scope=issue.scope, issue=updated, task=task),
            ),
            (ref(case, "review", issue.review_id, issue.version),),
        )
        if receipt.status != "accepted":
            raise ValueError(receipt.reason)
        return True
    return False
