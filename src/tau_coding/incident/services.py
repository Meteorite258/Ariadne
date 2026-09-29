"""Application configuration and lifecycle for telemetry, analysis and export."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tau_coding.incident.analysis import DockerAnalysisExecutor
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.otel import ExecutionExporter, ExportSettings
from tau_incident.analysis import AnalysisLimits
from tau_incident.models import Model
from tau_incident.telemetry import FixtureProvider, ServiceCatalog, TelemetryProvider
from tau_incident.telemetry.catalog import EnvironmentProvider, VersionedCatalog
from tau_incident.telemetry.dataset import load_dataset
from tau_incident.telemetry.live import Endpoint, LiveProvider, LiveSettings


class ServiceSettings(Model):
    schema_version: Literal[1] = 1
    mode: Literal["fixture", "live"]
    fixture: Path | None = None
    live: LiveSettings | None = None
    history: Path | None = None
    analysis_image: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}$"
    )
    analysis_limits: AnalysisLimits = Field(default_factory=AnalysisLimits)
    otel: ExportSettings | None = None

    @model_validator(mode="after")
    def coherent(self) -> ServiceSettings:
        if self.mode == "fixture" and self.fixture is None:
            raise ValueError("fixture mode requires a file/dataset directory")
        if self.mode == "live" and (self.live is None or self.history is None):
            raise ValueError("live mode requires live settings and versioned history")
        if (
            self.live
            and self.otel
            and (
                self.otel.service in self.live.services
                or self.otel.environment == self.live.environment
            )
        ):
            raise ValueError("business and agent services/environments must be separate")
        return self

    @classmethod
    def from_file(cls, path: Path) -> ServiceSettings:
        settings = cls.model_validate_json(path.read_bytes())
        return settings.model_copy(
            update={
                name: (path.parent / value).resolve()
                for name in ("fixture", "history")
                if (value := getattr(settings, name)) is not None
            }
        )


def auth(endpoint: Endpoint) -> dict[str, str]:
    if endpoint.token_env is None:
        return {}
    token = os.environ.get(endpoint.token_env)
    if not token:
        raise ValueError("configured telemetry token environment variable is missing")
    return {"Authorization": "Bearer " + token}


class IncidentServices:
    def __init__(self, settings: ServiceSettings, *, clock: Callable[[], datetime]) -> None:
        self.settings = settings
        self.telemetry: TelemetryProvider
        self.catalog: ServiceCatalog
        self.exporter: ExecutionExporter | None = None
        if settings.mode == "fixture":
            assert settings.fixture is not None
            path = settings.fixture
            fixture = (
                FixtureProvider(load_dataset(path), clock=clock, source="dataset:" + path.name)
                if path.is_dir()
                else FixtureProvider.from_file(path, clock=clock)
            )
            self.telemetry, self.catalog = fixture, fixture
            self.wait_probe = fixture.check_wait
        else:
            assert settings.live is not None and settings.history is not None
            headers = {
                kind: auth(endpoint)
                for kind, endpoint in (
                    ("metrics", settings.live.prometheus),
                    ("traces", settings.live.jaeger),
                    ("logs", settings.live.opensearch),
                )
                if endpoint is not None
            }
            live = LiveProvider(settings.live, clock=clock, headers=headers)
            catalog = VersionedCatalog(settings.history, clock=clock)
            catalog.history()  # Fail before creating model sessions when history is malformed.
            provider = EnvironmentProvider(live, catalog)
            self.telemetry, self.catalog = provider, catalog
            self.wait_probe = provider.check_wait

    def attach(self, host: IncidentHost) -> None:
        s = self.settings
        if s.live is not None and s.live.environment != host.config.environment:
            raise ValueError("data source environment does not match incident host")
        if s.analysis_image is not None:
            host.runtime.analysis = DockerAnalysisExecutor(
                host.store,
                host.artifacts,
                image=s.analysis_image,
                workspace=host.config.data_dir / "analysis-staging",
            )
            host.runtime.analysis_limits = s.analysis_limits
        if s.otel is not None:
            self.exporter = ExecutionExporter(
                s.otel, host.store, host.config.data_dir / "otel-gaps.jsonl", auth(s.otel.endpoint)
            )
            self.exporter.start()
            host.runtime.execution.on_finished = self.exporter.enqueue

    async def close(self, host: IncidentHost) -> None:
        host.runtime.execution.on_finished = None
        if self.exporter is not None:
            await self.exporter.close()
        host.runtime.analysis = None
        host.runtime.analysis_limits = None
