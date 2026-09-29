"""Host action/query implementation shared by local and remote frontends."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from tau_coding.incident.actions import IncidentAction, IncidentQuery
from tau_incident.store import IdempotencyConflict

if TYPE_CHECKING:
    from tau_coding.incident.host import IncidentHost


async def dispatch(host: IncidentHost, action: IncidentAction) -> dict[str, Any]:
    if host.stopping:
        raise ValueError("host is shutting down")
    if action.operation == "command":
        if action.command is not None and action.command.payload.kind not in {
            "create_case",
            "add_observation",
            "add_explanation",
            "add_constraint",
            "lifecycle",
            "extend_budget",
            "retry_task",
            "set_task_disposition",
            "reopen_case",
            "set_impact",
            "revise_evidence",
            "revise_task",
            "schedule_review",
            "withdraw_report",
        }:
            raise ValueError("internal runtime command is not a frontend capability")
        if (
            action.command is not None
            and action.command.payload.kind == "lifecycle"
            and action.command.payload.action not in {"pause", "cancel"}
        ):
            raise ValueError("use the resume action to resume a runtime")
        if action.command is None or action.command.case_id != action.case_id:
            raise ValueError("command and action case IDs must match")
        if action.command.command_id != action.request_id:
            raise ValueError("command ID must equal request_id")
        receipt = host.execute(action.command, action.expected_versions)
        return {"request_id": action.request_id, "receipt": receipt.model_dump(mode="json")}
    host.get_case(action.case_id)
    if action.operation == "bind":
        return {
            "request_id": action.request_id,
            "case": host.get_case(action.case_id).model_dump(mode="json"),
        }
    if action.operation in {"report", "handoff"}:
        return query(
            host,
            IncidentQuery(
                case_id=action.case_id, view="report" if action.operation == "report" else "handoff"
            ),
        )
    remote = host.owner_service(action.case_id) if action.case_id not in host.children else None
    if remote is not None:
        from tau_coding.incident.client import IncidentClient

        client = IncidentClient(remote)
        try:
            return await client.dispatch(action)
        finally:
            await client.aclose()
    settings = host.settings
    if settings.fixture is None and settings.services_config is None:
        raise ValueError("run requires configured fixture or services_config")
    body = action.model_dump_json()
    with host.store._transaction():
        row = host.store._connection.execute(
            "SELECT body,status,result,error FROM incident_actions WHERE request_id=?",
            (action.request_id,),
        ).fetchone()
        if row is not None:
            if row["body"] != body:
                raise IdempotencyConflict("request_id has different content")
            return {
                "request_id": action.request_id,
                "status": row["status"],
                "result": json.loads(row["result"]) if row["result"] else None,
                "error": row["error"],
            }
        current = host.get_case(action.case_id)
        for reference in action.expected_versions:
            if (
                reference.kind != "case"
                or reference.case_id != action.case_id
                or reference.version != current.version
            ):
                raise ValueError("run requires the current case version")
        active = host.store._connection.execute(
            "SELECT request_id FROM incident_actions WHERE case_id=? "
            "AND status IN ('queued','running')",
            (action.case_id,),
        ).fetchone()
        if active is not None:
            return {
                "request_id": action.request_id,
                "status": "already_scheduled",
                "active_request_id": active[0],
            }
        count = host.store._connection.execute(
            "SELECT COUNT(*) FROM incident_actions WHERE status='queued'"
        ).fetchone()[0]
        if count >= settings.auto_start.max_queued_cases:
            raise ValueError("incident run queue is full")
        host.store._connection.execute(
            "INSERT INTO incident_actions(request_id,case_id,body,status,environment,runner_id) "
            "VALUES (?,?,?,'queued',?,?)",
            (action.request_id, action.case_id, body, host.config.environment, host.instance_id),
        )
    host.ensure_scheduler()
    return {"request_id": action.request_id, "status": "queued"}


def query(host: IncidentHost, request: IncidentQuery) -> dict[str, Any]:
    case = host.get_case(request.case_id)
    view, reference = request.view, request.reference
    if view == "case":
        return case.model_dump(mode="json")
    if view == "export":
        import base64

        from tau_incident.history import HistoricalCases

        with (
            host.store._transaction(),
            HistoricalCases(host.store.path, host.artifacts.root) as archive,
        ):
            files = archive.export(case.case_id)
        return {
            "schema_version": 2,
            "files": {name: base64.b64encode(data).decode("ascii") for name, data in files.items()},
        }
    if view == "brief":
        return host.brief(case.case_id).model_dump(mode="json")
    if view == "budget":
        return dict(host.store.budget_summary(case.case_id))
    if view == "events":
        events = host.events(case.case_id, request.after_cursor, limit=request.limit)
        return {
            "type": "incident.events",
            "events": [e.model_dump(mode="json") for e in events],
            "next_cursor": events[-1].cursor if events else request.after_cursor,
        }
    if view == "provenance":
        from tau_coding.incident.views import provenance

        if reference is None:
            raise ValueError("provenance requires report_id or claim_id")
        return provenance(host, case.case_id, reference)
    if view == "action":
        row = host.store._connection.execute(
            "SELECT request_id,status,result,error FROM incident_actions "
            "WHERE request_id=? AND case_id=?",
            (reference, case.case_id),
        ).fetchone()
        if row is None:
            raise KeyError("unknown action")
        return dict(row)
    if view == "timeline":
        from tau_coding.incident.views import gaps

        records = host.executions(
            case.case_id, after_cursor=request.after_cursor, limit=request.limit
        )
        return {
            "type": "incident.timeline",
            "executions": [
                {
                    **r.model_dump(mode="json"),
                    "jaeger_url": f"{host.settings.jaeger_url.rstrip('/')}/trace/{r.trace_id}"
                    if host.settings.jaeger_url
                    else None,
                }
                for r in records
                if all(
                    getattr(request, key) is None or getattr(request, key) == getattr(r, key)
                    for key in (
                        "task_id",
                        "attempt_id",
                        "operation_kind",
                        "status",
                        "error_category",
                    )
                )
            ],
            "next_cursor": records[-1].cursor if records else request.after_cursor,
            "export_gaps": gaps(host, case.case_id),
            "waits": [w.model_dump(mode="json") for w in case.waits],
            "budget": host.store.budget_summary(case.case_id),
        }
    if view == "report":
        return {
            "reports": [r.model_dump(mode="json") for r in case.reports],
            "progress": host.report(case.case_id).model_dump(mode="json"),
        }
    if view == "handoff":
        from tau_coding.incident.views import handoff

        return handoff(host, case.case_id)
    if reference is None:
        raise ValueError(f"{view} requires reference")
    if view == "receipt":
        receipt = host.receipt(case.case_id, reference)
        return {"receipt": receipt.model_dump(mode="json") if receipt else None}
    if view == "evidence":
        return {
            "evidence": host.store.evidence(case.case_id, reference).model_dump(mode="json"),
            "raw_text": host.read_evidence(case.case_id, reference).decode(
                "utf-8", errors="replace"
            ),
        }
    snapshot = host.store.request(case.case_id, reference)
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "provider_input": json.loads(host.artifacts.read(snapshot.provider_input)),
        "usage": host.store.request_usage(case.case_id, reference),
    }


async def scheduler(host: IncidentHost) -> None:
    from tau_coding.incident.host import IncidentHost
    from tau_coding.incident.investigation import investigate

    async def run(action: IncidentAction, child: IncidentHost) -> None:
        try:
            if host.stopping or child.stopping:
                # Shutdown may precede this task's first turn and ownership acquisition.
                host.store._connection.execute(
                    "UPDATE incident_actions SET status=?,runner_id=NULL,lease_until=NULL "
                    "WHERE request_id=? AND runner_id=?",
                    (
                        "queued" if host.mode == "daemon" else "paused",
                        action.request_id,
                        host.instance_id,
                    ),
                )
                return
            result = await investigate(
                child,
                action.case_id,
                fixture=host.settings.fixture,
                services_config=host.settings.services_config,
                limits=action.limits.model_copy(
                    update={
                        "global_concurrency": min(
                            action.limits.global_concurrency,
                            host.settings.auto_start.limits.global_concurrency,
                        )
                    }
                ),
                provider_name=host.settings.provider,
                model=host.settings.model,
                resume=action.operation == "resume",
            )
            host.store._connection.execute(
                "UPDATE incident_actions SET status='finished',result=? "
                "WHERE request_id=? AND runner_id=?",
                (result.model_dump_json(), action.request_id, host.instance_id),
            )
        except Exception as exc:
            host.store._connection.execute(
                "UPDATE incident_actions SET status='failed',error=? "
                "WHERE request_id=? AND runner_id=?",
                (str(exc), action.request_id, host.instance_id),
            )
        finally:
            await child.shutdown()
            host.children.pop(action.case_id, None)

    from tau_incident.store.control import owner

    while not host.stopping:
        now = host.runtime.clock()
        host.store._connection.execute(
            "UPDATE incident_actions SET lease_until=? WHERE status='running' AND runner_id=?",
            ((now + timedelta(seconds=30)).isoformat(), host.instance_id),
        )
        for stale in host.store._connection.execute(
            "SELECT request_id,case_id FROM incident_actions WHERE status='running' "
            "AND environment=? AND (lease_until IS NULL OR lease_until<?)",
            (host.config.environment, now.isoformat()),
        ).fetchall():
            lease = owner(host.store, stale["case_id"])
            if lease is None or lease.expires_at <= now:
                host.store._connection.execute(
                    "UPDATE incident_actions SET status='queued',runner_id=NULL WHERE request_id=?",
                    (stale["request_id"],),
                )
        for task in tuple(host.jobs):
            if task.done():
                host.jobs.remove(task)
                task.result()
        capacity = host.settings.auto_start.max_running_cases - len(host.children)
        if capacity > 0:
            rows = host.store._connection.execute(
                "SELECT body FROM incident_actions WHERE status='queued' AND environment=? "
                "ORDER BY rowid LIMIT ?",
                (host.config.environment, capacity),
            ).fetchall()
            for row in rows:
                action = IncidentAction.model_validate_json(row[0])
                case = host.store.get_case(action.case_id)
                if case.scope.environment != host.config.environment:
                    continue
                child = IncidentHost(host.config, mode=host.mode, settings=host.settings)
                with host.store._transaction():
                    active_count = host.store._connection.execute(
                        "SELECT COUNT(*) FROM incident_actions WHERE status='running'"
                    ).fetchone()[0]
                    if active_count >= host.settings.auto_start.max_running_cases:
                        child.close()
                        continue
                    changed = host.store._connection.execute(
                        "UPDATE incident_actions SET status='running',runner_id=?,lease_until=? "
                        "WHERE request_id=? AND status='queued'",
                        (
                            host.instance_id,
                            (now + timedelta(seconds=30)).isoformat(),
                            action.request_id,
                        ),
                    ).rowcount
                if not changed:
                    child.close()
                    continue
                host.children[action.case_id] = child
                host.jobs.add(asyncio.create_task(run(action, child)))
        await asyncio.sleep(0.5)
