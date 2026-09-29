"""Application configuration and provider lifecycle, without CodingSession resources."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from tau_coding.context_window import estimate_text_tokens
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.services import IncidentServices, ServiceSettings
from tau_coding.provider_config import (
    OpenAICodexProviderConfig,
    ProviderModelMetadata,
    load_provider_settings,
    resolve_provider_selection,
)
from tau_coding.provider_runtime import create_model_provider
from tau_incident.budget import RequestBudget, RunLimits
from tau_incident.context import ContextBuilder
from tau_incident.executor import RoleRunner
from tau_incident.investigation import RunResult
from tau_incident.models import TaskAttempt


async def investigate(
    host: IncidentHost,
    case_id: str,
    *,
    fixture: Path | None,
    limits: RunLimits,
    provider_name: str | None = None,
    model: str | None = None,
    fixture_clock: Callable[[], datetime] | None = None,
    resume: bool = False,
    services_config: Path | None = None,
) -> RunResult:
    host.get_case(case_id)
    selection = resolve_provider_selection(
        load_provider_settings(), provider_name=provider_name, model=model
    )
    metadata = selection.provider.model_metadata.get(selection.model, ProviderModelMetadata())
    context_tokens = selection.provider.context_windows.get(
        selection.model, metadata.context_window
    )
    if context_tokens is not None:
        limits = limits.model_copy(
            update={"context_tokens": min(context_tokens, limits.context_tokens)}
        )
    if metadata.max_tokens is not None:
        limits = limits.model_copy(
            update={"output_tokens": min(metadata.max_tokens, limits.output_tokens)}
        )
    # Explicit attempt settings; do not mutate durable provider configuration.
    configured = replace(
        selection.provider,
        max_retries=0,
        model_metadata={
            **selection.provider.model_metadata,
            selection.model: replace(metadata, max_tokens=limits.output_tokens),
        },
    )
    if (fixture is None) == (services_config is None):
        raise ValueError("select exactly one of --fixture or --services-config")
    settings = (
        ServiceSettings.from_file(services_config)
        if services_config is not None
        else ServiceSettings(mode="fixture", fixture=fixture)
    )
    if settings.mode == "live" and fixture_clock is not None:
        raise ValueError("--fixture-now is only valid for fixture mode")
    services = IncidentServices(settings, clock=fixture_clock or host.runtime.clock)
    telemetry = services.telemetry
    budget = RequestBudget(host.store, limits, host.runtime.clock)
    builder = ContextBuilder(
        host.store,
        estimate=estimate_text_tokens,
        input_limit=limits.context_tokens - limits.output_tokens - 256,
    )
    provider = create_model_provider(
        configured, model=selection.model, max_output_tokens=limits.output_tokens
    )
    try:
        services.attach(host)
        runner = RoleRunner(
            provider=provider,
            model=selection.model,
            builder=builder,
            budget=budget,
            recorder=host.runtime.execution,
            configuration={
                "provider": configured.name,
                "model": selection.model,
                "limits": limits.model_dump(mode="json"),
                "max_retries": 0,
                "timeout_seconds": configured.timeout_seconds,
                "output_reservation": limits.output_tokens,
                "output_limit_enforced": not isinstance(configured, OpenAICodexProviderConfig),
                "accounting": "estimated input; unknown usage retains reservation",
                "telemetry_mode": settings.mode,
            },
        )

        @asynccontextmanager
        async def worker_factory(attempt: TaskAttempt) -> AsyncIterator[RoleRunner]:
            worker_provider = create_model_provider(
                configured, model=selection.model, max_output_tokens=limits.output_tokens
            )
            try:
                yield RoleRunner(
                    provider=worker_provider,
                    model=selection.model,
                    configuration={**runner.configuration, "provider_session": attempt.attempt_id},
                    builder=builder,
                    budget=RequestBudget(host.store, limits, host.runtime.clock),
                    recorder=host.runtime.execution,
                )
            finally:
                import asyncio

                async with asyncio.timeout(max(0.1, limits.shutdown_seconds)):
                    await worker_provider.aclose()

        return await host.runtime.run(
            case_id,
            runner=runner,
            telemetry=telemetry,
            catalog=services.catalog,
            runner_factory=worker_factory,
            resume=resume,
            wait_probe=services.wait_probe,
        )
    finally:
        import asyncio

        try:
            async with asyncio.timeout(max(0.1, limits.shutdown_seconds)):
                await provider.aclose()
        finally:
            await services.close(host)
