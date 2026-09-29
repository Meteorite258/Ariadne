"""Derived historical cards. Retrieval rechecks authoritative source report versions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from uuid import uuid4

from tau_incident.events import Command, CommitMemory, ReopenCase
from tau_incident.models import IncidentCase, MemoryCard, MemoryMatch, Scope, Source
from tau_incident.quality import ref

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime
    from tau_incident.store import CaseStore


def retrieve(
    store: CaseStore,
    *,
    project_key: str,
    scope: Scope,
    symptoms: str,
    exclude_case: str | None = None,
    cross_environment: bool = False,
    limit: int = 5,
) -> tuple[MemoryMatch, ...]:
    if not 1 <= limit <= 100:
        raise ValueError("memory limit must be between 1 and 100")
    candidates: list[tuple[float, MemoryMatch]] = []
    terms = set(re.findall(r"\w+", symptoms.casefold()))
    for row in store._connection.execute("SELECT body FROM cases ORDER BY case_id").fetchall():
        case = IncidentCase.model_validate_json(row[0])
        if case.case_id == exclude_case or case.project_key != project_key:
            continue
        if case.scope.environment != scope.environment and not cross_environment:
            continue
        if not case.memories or not case.reports:
            continue
        card, report = case.memories[-1], case.reports[-1]
        if (
            card.state != "current"
            or report.state != "current"
            or card.verification == "withdrawn"
            or card.source_report != ref(case, "report", report.report_id, report.version)
        ):
            continue
        differences: tuple[str, ...] = (
            ()
            if scope.environment == card.scope.environment
            else (
                f"Environment differs: historical={card.scope.environment}; "
                f"current={scope.environment}",
            )
        )
        if scope.time_window != card.scope.time_window:
            differences += (
                "Incident time windows differ; establish all facts from current evidence.",
            )
        overlap = len(set(scope.entities) & set(card.scope.entities))
        symptoms_overlap = len(terms & set(re.findall(r"\w+", card.symptoms.casefold())))
        time_score = 0.0
        if scope.time_window.start and card.scope.time_window.start:
            days = (
                abs((scope.time_window.start - card.scope.time_window.start).total_seconds())
                / 86400
            )
            time_score = 1 / (1 + days)
        score = (
            overlap * 10
            + symptoms_overlap
            + time_score
            + (100 if not differences or scope.environment == card.scope.environment else 0)
        )
        candidates.append((score, MemoryMatch(card=card, differences=differences)))
    candidates.sort(key=lambda item: (-item[0], item[1].card.memory_id))
    return tuple(match for _, match in candidates[:limit])


class MemoryStore:
    def __init__(self, runtime: IncidentRuntime) -> None:
        self.runtime = runtime

    def retrieve(
        self, case_id: str, *, cross_environment: bool = False, limit: int = 5
    ) -> tuple[MemoryMatch, ...]:
        case = self.runtime.get_case(case_id)
        operation = self.runtime.execution.start("memory", case_id=case_id, command_id=uuid4().hex)
        try:
            matches = retrieve(
                self.runtime.store,
                project_key=case.project_key,
                scope=case.scope,
                symptoms=case.symptoms,
                exclude_case=case_id,
                cross_environment=cross_environment,
                limit=limit,
            )
            self.runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result="retrieved",
                references=tuple(
                    ref(
                        self.runtime.get_case(m.card.case_id),
                        "memory",
                        m.card.memory_id,
                        m.card.version,
                    )
                    for m in matches
                ),
            )
            return matches
        except BaseException as exc:
            self.runtime.execution.finish(
                operation.operation_id, status="failed", result="retrieval_failed", detail=str(exc)
            )
            raise

    def invalidate(self, case_id: str, reason: str) -> None:
        case = self.runtime.get_case(case_id)
        receipt = self.runtime.execute(
            Command(
                command_id=uuid4().hex,
                case_id=case_id,
                payload=ReopenCase(scope=case.scope, reason=reason),
            )
        )
        if receipt.status != "accepted":
            raise ValueError(receipt.reason)

    def rebuild(self, case_id: str) -> MemoryCard:
        case = self.runtime.get_case(case_id)
        if not case.reports or case.reports[-1].state != "current":
            raise ValueError("rebuild requires a current report")
        report = case.reports[-1]
        previous = case.memories[-1] if case.memories else None
        if (
            previous
            and previous.state == "current"
            and previous.source_report == ref(case, "report", report.report_id, report.version)
        ):
            return previous
        card = MemoryCard(
            memory_id=f"memory:{case_id}",
            case_id=case_id,
            version=previous.version + 1 if previous else 1,
            project_key=case.project_key,
            scope=case.scope,
            source=Source(kind="runtime", actor="memory_builder"),
            source_report=ref(case, "report", report.report_id, report.version),
            evidence_cutoff=report.evidence_cutoff,
            summary=report.explanation,
            verification=report.verification,
            kind="diagnosis" if report.kind == "diagnosis" else "incomplete",
            symptoms=case.symptoms,
            effective_paths=tuple(t.goal for t in case.tasks if t.status == "completed"),
            failed_paths=tuple(t.goal for t in case.tasks if t.status in {"blocked", "cancelled"}),
            unresolved=report.unresolved,
            supersedes=ref(case, "memory", previous.memory_id, previous.version)
            if previous
            else None,
        )
        operation = self.runtime.execution.start("memory", case_id=case_id, command_id=uuid4().hex)
        try:
            receipt = self.runtime.execute(
                Command(
                    command_id=operation.command_id,
                    case_id=case_id,
                    payload=CommitMemory(scope=case.scope, card=card),
                ),
                parent=operation,
            )
            self.runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result=receipt.status,
                receipt_id=receipt.receipt_id,
                detail=receipt.reason,
            )
            if receipt.status != "accepted":
                raise ValueError(receipt.reason)
            return card
        except BaseException as exc:
            if self.runtime.store.execution(operation.operation_id).status == "running":
                self.runtime.execution.finish(
                    operation.operation_id,
                    status="failed",
                    result="rebuild_failed",
                    detail=str(exc),
                )
            raise

    async def rebuild_pending(self, case_id: str) -> MemoryCard | None:
        """Rebuild after the source transaction, without holding its write lock."""
        import asyncio

        await asyncio.sleep(0)
        case = self.runtime.get_case(case_id)
        if not case.reports or case.reports[-1].state != "current":
            return None
        return self.rebuild(case_id)
