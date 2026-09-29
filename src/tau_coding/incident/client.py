"""Reconnectable HTTP client; owns only its transport, never the daemon runtime."""

from __future__ import annotations

from typing import Any

import httpx

from tau_coding.incident.actions import IncidentAction, IncidentQuery
from tau_coding.incident.settings import HostSettings


class IncidentClient:
    def __init__(self, settings: HostSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.endpoint.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.credential()}"},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        )

    async def request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.client.post(path, json=payload)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def dispatch(self, action: IncidentAction) -> dict[str, Any]:
        return await self.request("/actions", action.model_dump(mode="json"))

    async def query(self, query: IncidentQuery) -> dict[str, Any]:
        return await self.request("/query", query.model_dump(mode="json"))

    async def aclose(self) -> None:
        await self.client.aclose()
