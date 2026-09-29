"""Durable application intake and dispatch state, sharing the Case Store transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tau_incident.store import CaseStore


def migrate(store: CaseStore) -> None:
    for statement in (
        "CREATE TABLE incident_actions (request_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
        "body TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, "
        "environment TEXT NOT NULL, runner_id TEXT, lease_until TEXT)",
        "CREATE UNIQUE INDEX incident_active_run ON incident_actions(case_id) "
        "WHERE status IN ('queued','running')",
        "CREATE TABLE alert_inbox (inbox_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "delivery_key TEXT NOT NULL, payload_hash TEXT NOT NULL, body TEXT NOT NULL, "
        "received_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT, operation_id TEXT NOT NULL, "
        "UNIQUE(source, delivery_key))",
        "CREATE TABLE alert_updates (update_id TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, "
        "occurrence_key TEXT NOT NULL, body TEXT NOT NULL, case_id TEXT, status TEXT NOT NULL, "
        "command_id TEXT NOT NULL, operation_id TEXT, detail TEXT)",
        "CREATE INDEX alert_occurrence ON alert_updates(occurrence_key)",
        "CREATE TABLE external_incidents (source TEXT NOT NULL, environment TEXT NOT NULL, "
        "external_id TEXT NOT NULL, case_id TEXT NOT NULL, "
        "PRIMARY KEY(source, environment, external_id))",
    ):
        store._connection.execute(statement)
    store._connection.execute("PRAGMA user_version = 5")
