"""Opt-in session binding and frontend lifecycle, separate from coding resources."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tau_agent.session.entries import CustomEntry
from tau_coding.incident.client import IncidentClient
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.rpc import IncidentDispatcher
from tau_coding.incident.settings import HostSettings
from tau_coding.paths import TauPaths

if TYPE_CHECKING:
    from tau_coding.session import CodingSession

NAMESPACE = "amadeus.incident"


async def binding(session: CodingSession) -> str | None:
    # Frontend session adapters without persistent entries remain in coding mode.
    for entry in reversed(getattr(session.state, "entries", ())):
        if isinstance(entry, CustomEntry) and entry.namespace == NAMESPACE:
            value = entry.data.get("active_case_id")
            return value if isinstance(value, str) else None
    return None


async def bind(session: CodingSession, case_id: str | None) -> None:
    await session.append_custom_entry(NAMESPACE, {"active_case_id": case_id})


def configured_dispatcher(cwd: Path) -> IncidentDispatcher | None:
    filename = os.environ.get("AMADEUS_INCIDENT_CONFIG")
    if not filename:
        return None
    settings = HostSettings.from_file(Path(filename))
    if os.environ.get("AMADEUS_INCIDENT_MODE", "connect") == "connect":
        return IncidentDispatcher(IncidentClient(settings))
    if os.environ.get("AMADEUS_INCIDENT_MODE") != "embedded":
        raise ValueError("AMADEUS_INCIDENT_MODE must be connect or embedded")
    config = IncidentConfig.resolve(project=cwd, environment=settings.environment, paths=TauPaths())
    descriptor = config.data_dir / "incident-service.json"
    if descriptor.exists():
        remote = HostSettings.from_file(descriptor)
        if remote.environment != settings.environment:
            raise ValueError("service environment differs from session settings")
        return IncidentDispatcher(IncidentClient(remote))
    return IncidentDispatcher(IncidentHost(config, settings=settings))


async def session_dispatch(session: CodingSession, command: dict[str, object]) -> dict[str, Any]:
    if session.incident_dispatcher is None:
        session.incident_dispatcher = configured_dispatcher(session.cwd)
    dispatcher = session.incident_dispatcher
    if dispatcher is None:
        raise ValueError("set AMADEUS_INCIDENT_CONFIG to enable incident mode")
    result = await dispatcher.dispatch(command)
    action = command.get("action")
    if (
        command.get("type") == "incident.dispatch"
        and isinstance(action, dict)
        and action.get("operation") == "bind"
    ):
        await bind(session, str(action["case_id"]))
    return result
