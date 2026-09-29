import asyncio
from contextlib import asynccontextmanager

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage, ToolCall
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.investigation import run_investigation
from tau_incident.telemetry import FixtureProvider, ReplayData

from .test_dispatch_wait import queue_task
from .test_investigation import runner


@pytest.mark.anyio
async def test_checkpoint_drains_sent_sibling_and_preserves_nonterminal_progress(
    runtime, case, clock
):
    limits = RunLimits(checkpoint_steps=2, concurrency=2, format_repairs=0)
    runtime.acquire_owner("case", limits)
    for name in ("fast", "slow"):
        queue_task(runtime, case, name)
    runtime.release_owner()
    completed = asyncio.Event()
    sibling_sent = asyncio.Event()

    class Fast(FakeProvider):
        async def stream_response(self, **kwargs):
            await asyncio.wait_for(sibling_sent.wait(), timeout=5)
            async for event in super().stream_response(**kwargs):
                yield event

    class Slow(FakeProvider):
        async def stream_response(self, **kwargs):
            sibling_sent.set()

            async def saved():
                while not next(
                    t for t in runtime.get_case("case").tasks if t.task_id == "fast"
                ).progress.version:
                    await asyncio.sleep(0.001)

            await asyncio.wait_for(saved(), timeout=5)
            # Let the fast worker's next request hit the shared checkpoint.
            await asyncio.sleep(0.02)
            async for event in super().stream_response(**kwargs):
                yield event
            completed.set()

    providers = {}

    @asynccontextmanager
    async def factory(attempt):
        if attempt.task_id == "fast":
            provider = Fast(
                [
                    [
                        assistant_done(
                            AssistantMessage(
                                content=[
                                    ToolCall(
                                        id="save",
                                        name="save_progress",
                                        arguments={
                                            "expected_progress_version": 0,
                                            "summary": "Preserved before checkpoint",
                                        },
                                    )
                                ]
                            )
                        )
                    ]
                ]
            )
        else:
            provider = Slow(
                [
                    [
                        assistant_done(
                            AssistantMessage(
                                content=(
                                    '{"completion":"partial",'
                                    '"summary":"In-flight response preserved",'
                                    '"next_actions":["continue comparison"]}'
                                )
                            )
                        )
                    ]
                ]
            )
        providers[attempt.task_id] = provider
        yield runner(runtime, clock, provider, limits)

    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    result = await asyncio.wait_for(
        run_investigation(
            runtime,
            "case",
            runner=runner(runtime, clock, FakeProvider([]), limits),
            telemetry=telemetry,
            catalog=telemetry,
            runner_factory=factory,
        ),
        timeout=10,
    )
    assert result.stop_reason == "checkpoint_steps", [
        (a.task_id, a.status, a.cancellation_reason) for a in runtime.get_case("case").attempts
    ]
    assert completed.is_set()
    state = runtime.get_case("case")
    assert state.control_intent == "pause"
    assert {t.task_id: t.progress.summary for t in state.tasks} == {
        "fast": "Preserved before checkpoint",
        "slow": "In-flight response preserved",
    }
    assert all(t.status == "ready" for t in state.tasks)
    assert len(state.findings) == 1
    assert runtime.store.usage_totals("case")[0] == 2
    assert all(len(provider.calls) == 1 for provider in providers.values())
