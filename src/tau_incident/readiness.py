"""One pure diagnosis completion policy shared by scheduling, reports and commit."""

from dataclasses import dataclass

from tau_incident.models import ClaimRevision, IncidentCase, VersionRef


@dataclass(frozen=True)
class DiagnosisReadiness:
    blockers: tuple[str, ...]
    followups: tuple[str, ...]
    claims: tuple[ClaimRevision, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers


def diagnosis_readiness(case: IncidentCase) -> DiagnosisReadiness:
    from tau_incident.quality import applicable

    diagnoses = [c for c in case.claims if c.judgment == "diagnosis" and c.validity == "current"]
    blockers: list[str] = []
    followups: list[str] = []
    if not diagnoses:
        blockers.append("No independently reviewed diagnosis")
    needed: dict[str, ClaimRevision] = {}

    def visit(claim: ClaimRevision) -> None:
        if claim.claim_id in needed:
            return
        needed[claim.claim_id] = claim
        if claim.validity != "current":
            blockers.append(f"Unreviewed judgment {claim.claim_id}@{claim.version}")
        for dependency in claim.dependencies:
            reference = dependency.reference
            if not applicable(case, reference):
                blockers.append(
                    f"Unavailable {reference.kind} {reference.object_id}@{reference.version}"
                )
            if reference.kind == "claim":
                parent = next((c for c in case.claims if c.claim_id == reference.object_id), None)
                if parent is not None:
                    visit(parent)

    for diagnosis in diagnoses:
        visit(diagnosis)
    # Missing edges cannot hide known opposition or a conflict against the conclusion.
    for issue in case.review_issues:
        if issue.status != "resolved" and (
            issue.target.object_id in needed or issue.problem in {"conflict", "basis_changed"}
        ):
            blockers.append(f"Review {issue.review_id}: {issue.gap}")
    for task in case.tasks:
        if task.status == "completed":
            continue
        if task.nonblocking_reason:
            task_ref = VersionRef(
                case_id=case.case_id,
                kind="task",
                object_id=task.task_id,
                version=task.contract_version,
            )
            assessed = any(
                i.disposition == "accepted"
                and i.target.object_id in {c.claim_id for c in diagnoses}
                and task_ref in i.checked_basis
                for i in case.review_issues
            )
            if not assessed:
                blockers.append(f"Unreviewed branch disposition {task.task_id}")
            followups.append(f"Task {task.task_id}: {task.nonblocking_reason}")
        elif task.kind in {"review", "repair"} and task.review_id is not None:
            task_issue = next(
                (i for i in case.review_issues if i.review_id == task.review_id), None
            )
            if task_issue is None or task_issue.status != "resolved":
                blockers.append(f"Task {task.task_id}: {task.status}")
        else:
            blockers.append(f"Task {task.task_id}: {task.status}")
    for wait in case.waits:
        if wait.status == "pending" and not any(
            t.task_id == wait.task_id and t.nonblocking_reason for t in case.tasks
        ):
            blockers.append(f"Wait {wait.wait_id}: {wait.condition}")
    for task in case.tasks:
        if not task.nonblocking_reason:
            blockers.extend(f"Task {task.task_id}: {gap}" for gap in task.progress.gaps)
    return DiagnosisReadiness(
        tuple(dict.fromkeys(blockers)), tuple(followups), tuple(needed.values())
    )
