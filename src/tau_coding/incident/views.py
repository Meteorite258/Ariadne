"""Scoped handoff and provenance projections over authoritative records."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tau_coding.incident.host import IncidentHost


def records(host: IncidentHost, case_id: str) -> list[dict[str, Any]]:
    host.get_case(case_id)
    rows = host.store._connection.execute(
        "SELECT body FROM executions WHERE case_id=? ORDER BY cursor", (case_id,)
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def gaps(host: IncidentHost, case_id: str) -> list[dict[str, Any]]:
    ids = {r["operation_id"] for r in records(host, case_id)}
    path = host.config.data_dir / "otel-gaps.jsonl"
    if not path.exists():
        return []
    result = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                result.append({"reason": "incomplete exporter gap record"})
                continue
            if row.get("operation_id") in ids:
                result.append(row)
    return result


def handoff(host: IncidentHost, case_id: str) -> dict[str, Any]:
    case = host.get_case(case_id)
    executions = records(host, case_id)
    events = [
        json.loads(row[0])
        for row in host.store._connection.execute(
            "SELECT body FROM events WHERE case_id=? ORDER BY cursor", (case_id,)
        ).fetchall()
    ]
    requests = [
        json.loads(row[0])
        for row in host.store._connection.execute(
            "SELECT body FROM requests WHERE case_id=? ORDER BY rowid", (case_id,)
        ).fetchall()
    ]
    receipts = [
        json.loads(row[0])
        for row in host.store._connection.execute(
            "SELECT body FROM receipts WHERE case_id=? ORDER BY rowid", (case_id,)
        ).fetchall()
    ]
    return {
        "schema_version": 2,
        "case": case.model_dump(mode="json"),
        "case_version": case.version,
        "exported_at": host.runtime.clock().isoformat(),
        "events": events,
        "executions": executions,
        "requests": requests,
        "receipts": receipts,
        "budget": host.store.budget_summary(case_id),
        "export_gaps": gaps(host, case_id),
        "artifact_policy": "Retrieve scoped artifacts through evidence/request IDs.",
        "follow_up": "Review unresolved issues, waits, constraints and unknown usage.",
    }


def provenance(host: IncidentHost, case_id: str, reference: str) -> dict[str, Any]:
    case = host.get_case(case_id)
    targets = [c for c in case.claims if c.claim_id == reference]
    reports = [r for r in case.reports if r.report_id == reference]
    if not targets and not reports:
        raise KeyError("unknown report or claim reference")
    refs = [ref for c in targets for ref in (*c.support, *c.opposition, *c.premises)]
    refs.extend(ref for r in reports for ref in r.basis)

    def mentions(value: Any) -> bool:
        if isinstance(value, dict):
            return (
                value.get("claim_id") == reference
                or value.get("report_id") == reference
                or any(mentions(child) for child in value.values())
            )
        if isinstance(value, list):
            return any(mentions(child) for child in value)
        return False

    source_events = [
        json.loads(row[0])
        for row in host.store._connection.execute(
            "SELECT body FROM events WHERE case_id=? ORDER BY cursor", (case_id,)
        ).fetchall()
        if mentions(json.loads(row[0])["payload"])
    ]
    source_commands = {event["command_id"] for event in source_events}
    operations = records(host, case_id)
    related = [
        r
        for r in operations
        if r["command_id"] in source_commands
        or any(
            ref["object_id"] == reference or any(ref["object_id"] == x.object_id for x in refs)
            for ref in r["references"]
        )
    ]
    # Submission links connect interpretation records to their generating attempt/request.
    attempt_ids = {r["attempt_id"] for r in related if r["attempt_id"]}
    related_ids = {r["operation_id"] for r in related}
    related.extend(
        r
        for r in operations
        if r["attempt_id"] in attempt_ids and r["operation_id"] not in related_ids
    )
    return {
        "claims": [c.model_dump(mode="json") for c in targets],
        "reports": [r.model_dump(mode="json") for r in reports],
        "references": [r.model_dump(mode="json") for r in dict.fromkeys(refs)],
        "events": source_events,
        "executions": related,
        "request_ids": list(dict.fromkeys(r["request_id"] for r in related if r["request_id"])),
        "command_ids": list(dict.fromkeys(r["command_id"] for r in related)),
        "note": "Execution links describe provenance; judgment dependencies describe evidence.",
    }
