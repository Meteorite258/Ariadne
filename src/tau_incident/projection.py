"""Domain display projection. Rebuilt with reduce_case; execution ledgers stay separate."""

from tau_incident.models import IncidentCase


def project_case(case: IncidentCase) -> IncidentCase:
    # Task status is reduced from task lifecycle commands and accepted results.
    # The source version identifies the event snapshot used for the materialized view.
    tasks = tuple(t.model_copy(update={"status_source_version": case.version}) for t in case.tasks)
    if any(r.kind == "diagnosis" and r.state == "current" for r in case.reports):
        status = "completed"
    elif case.control_intent in {"pause", "cancel"}:
        status = "paused"
    elif any(a.status == "running" for a in case.attempts) or any(
        t.status == "ready" for t in tasks
    ):
        status = "investigating"
    elif any(w.status == "pending" for w in case.waits):
        status = "waiting"
    else:
        status = "open"
    return case.model_copy(update={"tasks": tasks, "investigation_status": status})
