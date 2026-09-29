"""Explicit settings for owned runtimes and authenticated local transports."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from tau_incident.budget import RunLimits
from tau_incident.models import Model


class AutoStart(Model):
    environments: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    severities: tuple[str, ...] = ()
    max_running_cases: int = Field(default=2, ge=1, le=32)
    max_queued_cases: int = Field(default=100, ge=1, le=10000)
    correlation_seconds: int = Field(default=900, ge=0)
    limits: RunLimits = Field(default_factory=RunLimits)


class HostSettings(Model):
    environment: str = Field(default="demo", min_length=1)
    alert_source: str = Field(default="alertmanager", min_length=1)
    bind: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    endpoint: str = "http://127.0.0.1:8765"
    token_env: str = "AMADEUS_INCIDENT_TOKEN"
    webhook_token_env: str = "AMADEUS_ALERT_TOKEN"
    allow_remote: bool = False
    max_body_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    max_pending_alerts: int = Field(default=10000, ge=1)
    fixture: Path | None = None
    services_config: Path | None = None
    provider: str | None = None
    model: str | None = None
    jaeger_url: str | None = None
    auto_start: AutoStart = Field(default_factory=AutoStart)

    @model_validator(mode="after")
    def validate_transport(self) -> Self:
        if self.fixture is not None and self.services_config is not None:
            raise ValueError("choose fixture or services_config")
        for value in (self.endpoint, self.jaeger_url):
            if value is None:
                continue
            url = urlsplit(value)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username:
                raise ValueError("transport URLs require http(s), host and no credentials")
            if url.query or url.fragment:
                raise ValueError("transport URLs cannot contain query or fragment")
        local = {"127.0.0.1", "localhost", "::1"}
        if not self.allow_remote and (
            self.bind not in local or urlsplit(self.endpoint).hostname not in local
        ):
            raise ValueError("remote binding/connection requires allow_remote=true")
        if self.auto_start.limits.output_tokens >= self.auto_start.limits.context_tokens:
            raise ValueError("output allowance must be smaller than context window")
        return self

    def credential(self, *, webhook: bool = False) -> str:
        name = self.webhook_token_env if webhook else self.token_env
        value = os.environ.get(name, "")
        if len(value) < 24:
            raise ValueError(f"{name} must contain at least 24 characters")
        return value

    @classmethod
    def from_file(cls, path: Path) -> HostSettings:
        settings = cls.model_validate_json(path.read_text(encoding="utf-8"))
        return settings.model_copy(
            update={
                name: (path.parent / value).resolve()
                for name in ("fixture", "services_config")
                if (value := getattr(settings, name)) is not None and not value.is_absolute()
            }
        )
