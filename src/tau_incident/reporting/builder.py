"""Versioned reports with deterministic fallback and the shared command commit boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from tau_incident.events import Command, CommitReport
from tau_incident.models import DiagnosisReport, Source
from tau_incident.quality import ref

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime


class ReportBuilder:
    def __init__(self, runtime: IncidentRuntime) -> None:
        self.runtime = runtime

    def build(self, case_id: str, *, partial: bool = False) -> DiagnosisReport:
        case = self.runtime.get_case(case_id)
        from tau_incident.context import ContextBuilder
        from tau_incident.readiness import diagnosis_readiness

        readiness = diagnosis_readiness(case)
        unresolved = readiness.blockers
        current = tuple(c for c in case.claims if c.validity == "current")
        complete = not partial and readiness.ready
        previous = case.reports[-1] if case.reports else None
        evidence = tuple(ref(case, "evidence", e.evidence_id, e.version) for e in case.observations)
        return DiagnosisReport(
            report_id=f"report:{case_id}",
            case_id=case_id,
            version=previous.version + 1 if previous else 1,
            case_version=case.version,
            scope=case.scope,
            source=Source(kind="runtime", actor="report_builder"),
            kind="diagnosis" if complete else "progress",
            explanation="\n".join(f"{c.claim_id}@{c.version}: {c.statement}" for c in current)
            or "No reviewed diagnosis is available. Investigation remains incomplete.",
            evidence_cutoff=self.runtime.clock(),
            basis=ContextBuilder._refs(case),
            unresolved=unresolved,
            recommendations=readiness.followups
            + tuple(
                dict.fromkeys(
                    i.required_action for i in case.review_issues if i.status != "resolved"
                )
            ),
            verification="unverified",
            impact_status=case.impact_status,
            supersedes=ref(case, "report", previous.report_id, previous.version)
            if previous
            else None,
            propagation=tuple(
                f"{d.reference.object_id}@{d.reference.version} -> {c.claim_id}@{c.version}"
                for c in current
                for d in c.dependencies
                if d.relation == "derived_from"
            ),
            support=tuple(dict.fromkeys(r for c in current for r in c.support)),
            counterevidence=tuple(dict.fromkeys(r for c in case.claims for r in c.opposition)),
            alternatives=tuple(c.statement for c in case.claims if c.validity != "current")
            + tuple(e.text for e in case.candidate_explanations),
            verification_sources=(),
            input_evidence=evidence,
            input_tasks=tuple(f"{t.task_id}:{t.contract_version}:{t.status}" for t in case.tasks),
        )

    def commit(self, report: DiagnosisReport) -> DiagnosisReport:
        operation = self.runtime.execution.start(
            "report",
            case_id=report.case_id,
            command_id=uuid4().hex,
            runtime_generation=self.runtime.owner.generation if self.runtime.owner else 0,
        )
        try:
            receipt = self.runtime.execute(
                Command(
                    command_id=operation.command_id,
                    case_id=report.case_id,
                    payload=CommitReport(scope=report.scope, report=report),
                ),
                parent=operation,
            )
            self.runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result=receipt.status,
                receipt_id=receipt.receipt_id,
                detail=receipt.reason,
                references=(
                    ref(
                        self.runtime.get_case(report.case_id),
                        "report",
                        report.report_id,
                        report.version,
                    ),
                ),
            )
            if receipt.status != "accepted":
                raise ValueError(receipt.reason)
            return report
        except BaseException as exc:
            if self.runtime.store.execution(operation.operation_id).status == "running":
                self.runtime.execution.finish(
                    operation.operation_id, status="failed", result="report_failed", detail=str(exc)
                )
            raise


def render_report(report: DiagnosisReport) -> str:
    lines = [
        f"# {report.kind.title()} {report.report_id}@{report.version}",
        "",
        f"State: {report.state}; verification: {report.verification}; "
        f"business impact: {report.impact_status}",
        f"Evidence cutoff: {report.evidence_cutoff.isoformat()}",
        "",
        report.explanation,
    ]
    for title, values in (
        ("Propagation", report.propagation),
        ("Alternatives", report.alternatives),
        ("Unresolved", report.unresolved),
        ("Recommendations", report.recommendations),
    ):
        lines.extend(["", f"## {title}", "", *(f"- {v}" for v in values)])
    for title, refs in (
        ("Supporting evidence", report.support),
        ("Counterevidence", report.counterevidence),
        ("Input versions", report.basis),
        ("Verification sources", report.verification_sources),
    ):
        lines.extend(
            ["", f"## {title}", "", *(f"- {r.kind}:{r.object_id}@{r.version}" for r in refs)]
        )
    return "\n".join(lines) + "\n"
