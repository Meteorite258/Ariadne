"""Shared, versioned application actions for incident frontends."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from tau_incident.budget import RunLimits
from tau_incident.events import Command
from tau_incident.models import Model, Text, VersionRef


def capabilities() -> dict[str, object]:
    """The same discovery contract for embedded, HTTP and JSONL clients."""
    commands = [
        "incident.dispatch",
        "incident.query",
        "incident.events",
        "incident.timeline",
        "incident.intake",
        "incident.inbox",
        "incident.associate",
    ]
    return {
        "schema_version": 2,
        "commands": commands,
        "capabilities": commands,
        "events": ["incident.events", "incident.timeline"],
        "cursor_semantics": "persistent polling; independent event and execution cursors",
    }


class IncidentAction(Model):
    request_id: Text
    operation: Literal["command", "bind", "run", "resume", "report", "handoff"]
    case_id: Text
    command: Command | None = None
    expected_versions: tuple[VersionRef, ...] = ()
    limits: RunLimits = Field(default_factory=RunLimits)


class IncidentQuery(Model):
    case_id: Text
    view: Literal[
        "case",
        "brief",
        "events",
        "timeline",
        "evidence",
        "request",
        "receipt",
        "budget",
        "report",
        "handoff",
        "provenance",
        "action",
        "export",
    ] = "brief"
    after_cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    reference: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    operation_kind: str | None = None
    status: str | None = None
    error_category: str | None = None
