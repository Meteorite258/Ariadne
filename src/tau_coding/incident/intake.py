"""Authenticated adapters hand durable normalized input to the shared command boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from tau_coding.incident.actions import IncidentAction
from tau_coding.incident.host import IncidentHost
from tau_incident.alerts import AlertEvent
from tau_incident.events import AddObservation, Command, CreateCase
from tau_incident.models import ObservationInput, Scope, Source, TimeWindow
from tau_incident.store import IdempotencyConflict


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime
    endsAt: datetime | None = None
    fingerprint: str
    generatorURL: str | None = None


class AlertmanagerDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: str
    alerts: list[AlertmanagerAlert] = Field(min_length=1, max_length=1000)
    truncatedAlerts: int = Field(default=0, ge=0)


def normalize(payload: dict[str, Any], *, source: str, environment: str) -> tuple[AlertEvent, ...]:
    if payload.get("format") == "normalized":
        values = payload.get("events")
        if not isinstance(values, list) or not 1 <= len(values) <= 1000:
            raise ValueError("normalized input requires 1..1000 events")
        normalized = tuple(AlertEvent.model_validate(value) for value in values)
        if any(e.scope.environment != environment or e.source != source for e in normalized):
            raise ValueError("normalized event source/environment mismatch")
        return normalized
    delivery = AlertmanagerDelivery.model_validate(payload)
    if delivery.version != "4":
        raise ValueError("only Alertmanager webhook version 4 is supported")
    events = []
    for alert in delivery.alerts:
        labels = alert.labels
        if labels.get("environment") != environment:
            raise ValueError("alert environment must explicitly match the configured environment")
        entity = labels.get("service")
        if not entity or not labels.get("alertname"):
            raise ValueError("alert requires service and alertname labels")
        end = alert.endsAt if alert.status == "resolved" else None
        event = AlertEvent.model_validate(
            {
                "source": source,
                "fingerprint": alert.fingerprint,
                "rule": labels["alertname"],
                "severity": labels.get("severity", "unknown"),
                "scope": Scope(
                    environment=environment,
                    entities=(entity,),
                    time_window=TimeWindow(start=alert.startsAt, end=end),
                ),
                "starts_at": alert.startsAt,
                "ends_at": end,
                "observed_at": end if alert.status == "resolved" else None,
                "status": alert.status,
                "summary": alert.annotations.get("summary") or labels["alertname"],
                "external_incident_id": labels.get("incident_id"),
                "raw_reference": alert.generatorURL,
            }
        )
        if event.status == "resolved" and event.ends_at is None:
            raise ValueError("resolved alert requires endsAt")
        events.append(event)
    return tuple(events)


def receive(
    host: IncidentHost,
    payload: dict[str, Any],
    *,
    source: str | None = None,
    delivery_id: str | None = None,
) -> dict[str, Any]:
    source = source or host.settings.alert_source
    events = normalize(payload, source=source, environment=host.config.environment)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    key = host.config.environment + ":" + (delivery_id or digest)
    existing = host.store._connection.execute(
        "SELECT * FROM alert_inbox WHERE source=? AND delivery_key=?", (source, key)
    ).fetchone()
    if existing is not None:
        if existing["payload_hash"] != digest:
            raise IdempotencyConflict("delivery ID reused with different payload")
        return {"inbox_id": existing["inbox_id"], "status": existing["status"], "duplicate": True}
    inbox_id = uuid4().hex
    operation = host.runtime.execution.start("alert_receive", case_id=None, command_id=inbox_id)
    try:
        with host.store._transaction():
            count = host.store._connection.execute(
                "SELECT COUNT(*) FROM alert_inbox WHERE status IN ('pending','processing')"
            ).fetchone()[0]
            if count >= host.settings.max_pending_alerts:
                raise ValueError("alert inbox capacity reached")
            host.store._connection.execute(
                "INSERT INTO alert_inbox VALUES (?,?,?,?,?,?,'pending',NULL,?)",
                (
                    inbox_id,
                    source,
                    key,
                    digest,
                    body,
                    host.runtime.clock().isoformat(),
                    operation.operation_id,
                ),
            )
            for event in events:
                event_body = event.model_dump_json()
                update_id = hashlib.sha256(event_body.encode()).hexdigest()
                host.store._connection.execute(
                    "INSERT OR IGNORE INTO alert_updates "
                    "VALUES (?,?,?,?,NULL,'pending',?,NULL,NULL)",
                    (update_id, inbox_id, event.occurrence_key, event_body, f"alert:{update_id}"),
                )
        host.runtime.execution.finish(
            operation.operation_id,
            status="succeeded",
            result="durable_inbox",
            detail=f"inbox={inbox_id}; truncatedAlerts={payload.get('truncatedAlerts', 0)}",
        )
    except Exception:
        host.runtime.execution.finish(
            operation.operation_id, status="failed", result="receive_failed"
        )
        raise
    return {"inbox_id": inbox_id, "status": "pending", "duplicate": False}


def choose_case(host: IncidentHost, event: AlertEvent) -> tuple[str | None, str]:
    if event.external_incident_id:
        row = host.store._connection.execute(
            "SELECT case_id FROM external_incidents "
            "WHERE source=? AND environment=? AND external_id=?",
            (event.source, event.scope.environment, event.external_incident_id),
        ).fetchone()
        if row is not None:
            return str(row[0]), "external_incident"
    rows = host.store._connection.execute(
        "SELECT DISTINCT case_id FROM alert_updates WHERE occurrence_key=? AND case_id IS NOT NULL",
        (event.occurrence_key,),
    ).fetchall()
    if len(rows) == 1:
        return str(rows[0][0]), "same_occurrence"
    candidates = []
    for row in host.store._connection.execute("SELECT case_id FROM cases").fetchall():
        case = host.store.get_case(row[0])
        if (
            case.project_key != host.config.project_key
            or case.scope.environment != event.scope.environment
        ):
            continue
        if case.investigation_status == "completed" or not set(case.scope.entities).intersection(
            event.scope.entities
        ):
            continue
        start = case.scope.time_window.start
        if (
            start is not None
            and abs((event.starts_at - start).total_seconds())
            <= host.settings.auto_start.correlation_seconds
        ):
            # A recurrence must not silently attach to the previous occurrence of this rule.
            previous = host.store._connection.execute(
                "SELECT body FROM alert_updates WHERE case_id=? "
                "AND status IN ('associated','processed')",
                (case.case_id,),
            ).fetchall()
            if any(
                (old := AlertEvent.model_validate_json(p[0])).fingerprint == event.fingerprint
                and old.starts_at != event.starts_at
                for p in previous
            ):
                continue
            candidates.append(case.case_id)
    if len(candidates) > 1:
        return None, "ambiguous"
    if candidates:
        return candidates[0], "scope_time"
    if event.status == "resolved":
        return None, "unmatched_resolved"
    return uuid5(NAMESPACE_URL, f"{host.config.project_key}:{event.occurrence_key}").hex, "new"


async def process_pending(host: IncidentHost) -> None:
    rows = host.store._connection.execute(
        "SELECT * FROM alert_updates WHERE status='pending' "
        "AND json_extract(body, '$.scope.environment')=? ORDER BY rowid LIMIT 32",
        (host.config.environment,),
    ).fetchall()
    for row in rows:
        event = AlertEvent.model_validate_json(row["body"])
        if event.scope.environment != host.config.environment:
            continue
        case_id, reason = (row["case_id"], "manual") if row["case_id"] else choose_case(host, event)
        if case_id is None:
            host.store._connection.execute(
                "UPDATE alert_updates SET status='needs_association',detail=? WHERE update_id=?",
                (reason, row["update_id"]),
            )
            continue
        host.store._connection.execute(
            "UPDATE alert_updates SET case_id=? WHERE update_id=?", (case_id, row["update_id"])
        )
        operation = host.runtime.execution.start(
            "alert_associate", case_id=case_id, command_id=row["command_id"]
        )
        host.store._connection.execute(
            "UPDATE alert_updates SET operation_id=? WHERE update_id=?",
            (operation.operation_id, row["update_id"]),
        )
        try:
            source = Source(kind="import", actor=event.source, reference=f"inbox:{row['inbox_id']}")
            try:
                host.get_case(case_id)
            except KeyError:
                creation = Command(
                    command_id=f"alert-create:{case_id}",
                    case_id=case_id,
                    payload=CreateCase(
                        project_key=host.config.project_key,
                        scope=event.scope,
                        symptoms=event.summary,
                        impact="Unverified alert impact",
                        source=source,
                    ),
                )
                receipt = host.execute(creation, parent=operation)
                if receipt.status != "accepted":
                    raise ValueError(receipt.reason) from None
            prior = host.store._connection.execute(
                "SELECT body FROM alert_updates WHERE occurrence_key=? "
                "AND status IN ('associated','processed')",
                (event.occurrence_key,),
            ).fetchall()
            conflict = any(
                AlertEvent.model_validate_json(p[0]).status == "resolved"
                and event.status == "firing"
                for p in prior
            )
            receipt = host.execute(
                Command(
                    command_id=row["command_id"],
                    case_id=case_id,
                    payload=AddObservation(
                        observation=ObservationInput(
                            summary=f"Alert {event.rule}: {event.status}"
                            + (" (out-of-order conflict)" if conflict else ""),
                            raw_text=event.model_dump_json(),
                            scope=event.scope,
                            source=source,
                            result="unknown",
                        )
                    ),
                ),
                parent=operation,
            )
            if receipt.status != "accepted":
                raise ValueError(receipt.reason) from None
            with host.store._transaction():
                host.store._connection.execute(
                    "UPDATE alert_updates SET status='associated',detail=? WHERE update_id=?",
                    ("out_of_order_conflict" if conflict else reason, row["update_id"]),
                )
                if event.external_incident_id:
                    host.store._connection.execute(
                        "INSERT OR IGNORE INTO external_incidents VALUES (?,?,?,?)",
                        (
                            event.source,
                            event.scope.environment,
                            event.external_incident_id,
                            case_id,
                        ),
                    )
            host.runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result="associated",
                receipt_id=receipt.receipt_id,
                event_ids=receipt.event_ids,
            )
        except Exception as exc:
            record = host.store.execution(operation.operation_id)
            if record.status == "running":
                host.runtime.execution.finish(
                    operation.operation_id,
                    status="failed",
                    result="association_failed",
                    detail=str(exc),
                )
            host.store._connection.execute(
                "UPDATE alert_updates SET status='failed',detail=? WHERE update_id=?",
                (str(exc), row["update_id"]),
            )
    await schedule_associated(host)
    host.store._connection.execute(
        "UPDATE alert_inbox SET status='processed' WHERE status='pending' AND NOT EXISTS "
        "(SELECT 1 FROM alert_updates u WHERE u.inbox_id=alert_inbox.inbox_id "
        "AND u.status!='processed')"
    )


def associate(host: IncidentHost, update_id: str, case_id: str) -> None:
    host.get_case(case_id)
    row = host.store._connection.execute(
        "SELECT body,status FROM alert_updates WHERE update_id=?", (update_id,)
    ).fetchone()
    if (
        row is None
        or AlertEvent.model_validate_json(row[0]).scope.environment != host.config.environment
    ):
        raise KeyError("unknown alert update")
    if row[1] not in {"needs_association", "failed"}:
        raise ValueError("only unresolved/failed alert updates can be associated")
    host.store._connection.execute(
        "UPDATE alert_updates SET case_id=?,status='pending',detail='manual' WHERE update_id=?",
        (case_id, update_id),
    )


async def schedule_associated(host: IncidentHost) -> None:
    policy = host.settings.auto_start
    for row in host.store._connection.execute(
        "SELECT * FROM alert_updates WHERE status='associated' "
        "AND json_extract(body, '$.scope.environment')=? ORDER BY rowid LIMIT 32",
        (host.config.environment,),
    ).fetchall():
        event = AlertEvent.model_validate_json(row["body"])
        if event.scope.environment != host.config.environment:
            continue
        try:
            if (
                event.status == "firing"
                and row["detail"] != "out_of_order_conflict"
                and event.scope.environment in policy.environments
                and set(event.scope.entities).issubset(policy.services)
                and event.severity in policy.severities
            ):
                await host.dispatch(
                    IncidentAction(
                        request_id=f"alert-run:{event.occurrence_key}",
                        operation="run",
                        case_id=row["case_id"],
                        limits=policy.limits,
                    )
                )
            host.store._connection.execute(
                "UPDATE alert_updates SET status='processed' WHERE update_id=?", (row["update_id"],)
            )
        except Exception as exc:
            # Keep the durable scheduling phase pending; bounded queue backpressure can clear.
            host.store._connection.execute(
                "UPDATE alert_updates SET detail=? WHERE update_id=?",
                (f"dispatch pending: {exc}", row["update_id"]),
            )


def inbox(host: IncidentHost, inbox_id: str | None = None) -> dict[str, Any]:
    if inbox_id is not None:
        row = host.store._connection.execute(
            "SELECT * FROM alert_inbox WHERE inbox_id=?", (inbox_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown inbox delivery")
        payload = json.loads(row["body"])
        normalize(payload, source=row["source"], environment=host.config.environment)
        return {
            "inbox_id": inbox_id,
            "status": row["status"],
            "payload": payload,
            "received_at": row["received_at"],
            "payload_hash": row["payload_hash"],
            "execution": host.store.execution(row["operation_id"]).model_dump(mode="json"),
        }
    updates = []
    for row in host.store._connection.execute("SELECT * FROM alert_updates ORDER BY rowid DESC"):
        event = AlertEvent.model_validate_json(row["body"])
        if event.scope.environment == host.config.environment:
            updates.append(
                {
                    key: row[key]
                    for key in (
                        "update_id",
                        "inbox_id",
                        "case_id",
                        "status",
                        "detail",
                        "operation_id",
                        "command_id",
                    )
                }
            )
            if len(updates) == 100:
                break
    return {"updates": updates}
