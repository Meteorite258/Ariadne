"""Incident RPC namespace; Pi commands and coding agent events stay independent."""

from __future__ import annotations

from typing import Any

from tau_coding.incident.actions import IncidentAction, IncidentQuery, capabilities
from tau_coding.incident.client import IncidentClient
from tau_coding.incident.host import IncidentHost


class IncidentDispatcher:
    def __init__(self, backend: IncidentHost | IncidentClient) -> None:
        self.backend = backend

    async def dispatch(self, command: dict[str, object]) -> dict[str, Any]:
        kind = command.get("type")
        if kind == "incident.capabilities":
            if isinstance(self.backend, IncidentClient):
                return await self.backend.request("/capabilities", {})
            return {
                **capabilities(),
                "environment": self.backend.config.environment,
                "project_key": self.backend.config.project_key,
            }
        if kind == "incident.dispatch":
            return await self.backend.dispatch(IncidentAction.model_validate(command.get("action")))
        if kind in {"incident.query", "incident.events", "incident.timeline"}:
            payload = command.get("query")
            if not isinstance(payload, dict):
                raise ValueError("query must be an object")
            query = IncidentQuery.model_validate(payload)
            if kind in {"incident.events", "incident.timeline"}:
                query = query.model_copy(update={"view": str(kind).split(".")[1]})
            if isinstance(self.backend, IncidentClient):
                return await self.backend.query(query)
            return self.backend.query(query)
        paths = {
            "incident.intake": "/intake",
            "incident.inbox": "/inbox",
            "incident.associate": "/associate",
        }
        if kind not in paths:
            raise ValueError("unknown incident command")
        payload = command.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        if isinstance(self.backend, IncidentClient):
            return await self.backend.request(paths[str(kind)], payload)
        from tau_coding.incident.intake import associate, inbox, process_pending, receive

        if kind == "incident.intake":
            result = receive(self.backend, payload)
            await process_pending(self.backend)
            return result
        if kind == "incident.associate":
            associate(self.backend, str(payload["update_id"]), str(payload["case_id"]))
            await process_pending(self.backend)
            return {"status": "pending"}
        return inbox(self.backend, str(payload["inbox_id"]) if payload.get("inbox_id") else None)

    async def aclose(self) -> None:
        if isinstance(self.backend, IncidentClient):
            await self.backend.aclose()
        else:
            await self.backend.shutdown()
