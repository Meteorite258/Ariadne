import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage, ToolCall, ToolResultMessage
from tau_ai import FakeProvider
from tau_incident.budget import RequestBudget, RunLimits
from tau_incident.context import ContextBuilder
from tau_incident.executor import RoleRunner
from tau_incident.investigation import run_investigation
from tau_incident.telemetry import Coverage, FixtureProvider, ReplayData, TelemetryRow


def runner(runtime, clock, provider, limits):
    return RoleRunner(
        provider=provider,
        model="fake",
        configuration={"provider": "fake"},
        builder=ContextBuilder(runtime.store, estimate=lambda s: len(s) // 4, input_limit=28000),
        budget=RequestBudget(runtime.store, limits, clock),
        recorder=runtime.execution,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("worker_count,diagnosis", [(1, False), (2, False), (1, True)])
async def test_plan_tool_finding_report_memory_and_request_provenance(
    runtime, case, clock, worker_count, diagnosis
):
    limits = RunLimits(
        checkpoint_steps=None if diagnosis else 3 * worker_count,
        concurrency=worker_count,
        output_tokens=512,
    )
    planner = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=json.dumps(
                            {
                                "choice": "investigate",
                                "reason": "inspect failures",
                                "task": {
                                    "kind": "diagnose" if diagnosis else "explore",
                                    "goal": "inspect logs",
                                    "scope": case.scope.model_dump(mode="json"),
                                    "completion_conditions": ["query logs"],
                                    "allowed_tools": ["telemetry_query"],
                                },
                            }
                        )
                    )
                )
            ]
        ]
        * worker_count
    )
    barrier = asyncio.Event()
    entered = []

    class ConcurrentTelemetry(FixtureProvider):
        async def query(self, *args, **kwargs):
            entered.append(asyncio.current_task())
            if len(entered) == worker_count:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
            return await super().query(*args, **kwargs)

    telemetry = ConcurrentTelemetry(
        ReplayData(
            name="queryable",
            rows=(
                TelemetryRow(
                    kind="logs",
                    environment="test",
                    entity="checkout",
                    data_at=clock(),
                    available_at=clock(),
                    body={"error": "payment unavailable"},
                ),
            ),
            coverage=(
                Coverage(kind="logs", scope=case.scope, available_at=clock(), note="test capture"),
            ),
        ),
        clock=clock,
        source="fixture",
    )
    workers = []

    class Investigator(FakeProvider):
        def stream_response(self, **kwargs):
            # Every actual provider call must already have its persisted neutral snapshot.
            count = runtime.store._connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            assert count >= len(planner.calls) + sum(len(w.calls) for w in workers) + 1
            assert not runtime.store._connection.in_transaction
            assert "completion_condition_ids" in kwargs["system"]
            assert "catalog observations with claims=[]" in kwargs["system"]
            results = [m for m in kwargs["messages"] if isinstance(m, ToolResultMessage)]
            if not results:
                message = AssistantMessage(
                    content=[
                        ToolCall(
                            id="query",
                            name="telemetry_query",
                            arguments={"kind": "logs", "scope": case.scope.model_dump(mode="json")},
                        )
                    ]
                )
            else:
                data = json.loads(results[-1].text)
                assert not results[-1].is_error
                evidence = data["evidence"]
                reference = dict(
                    case_id="case", kind="evidence", object_id=evidence["evidence_id"], version=1
                )
                message = AssistantMessage(
                    content=json.dumps(
                        {
                            "observations": [reference],
                            "completion": "complete",
                            "satisfied_conditions": list(
                                next(
                                    t.condition_ids
                                    for t in runtime.get_case("case").tasks
                                    if t.active_attempt_id == kwargs["session_id"]
                                )
                            ),
                            "gaps": []
                            if diagnosis
                            else ["root cause still requires investigation"],
                            "claims": [
                                {
                                    "judgment": "diagnosis",
                                    "statement": (
                                        "checkout errors are caused by unavailable payment"
                                    ),
                                    "scope": case.scope.model_dump(mode="json"),
                                    "support": [reference],
                                    "reason": "observed downstream error",
                                }
                            ]
                            if diagnosis
                            else [],
                        }
                    )
                )
            self._streams.append([assistant_done(message)])
            return super().stream_response(**kwargs)

    @asynccontextmanager
    async def factory(attempt):
        current = runtime.get_case("case")
        task = next(t for t in current.tasks if t.task_id == attempt.task_id)
        if task.kind == "review":
            issue = next(i for i in current.review_issues if i.review_id == task.review_id)
            # The reviewed diagnosis cannot enter the report before this separate role.
            assert current.claims[0].validity == "needs_review"
            assert not current.reports
            provider = FakeProvider(
                [
                    [
                        assistant_done(
                            AssistantMessage(
                                content=json.dumps(
                                    {
                                        "disposition": "accepted",
                                        "assessment": (
                                            "The scoped observation supports this fixture diagnosis"
                                        ),
                                        "gap": "none identified in the fixture",
                                        "impact": "checkout availability",
                                        "required_action": "none",
                                        "basis": [
                                            r.model_dump(mode="json")
                                            for r in (issue.target, *issue.basis)
                                        ],
                                    }
                                )
                            )
                        )
                    ]
                ]
            )
        else:
            provider = Investigator([])
        workers.append(provider)
        yield runner(runtime, clock, provider, limits)

    result = await run_investigation(
        runtime,
        "case",
        runner=runner(runtime, clock, planner, limits),
        telemetry=telemetry,
        catalog=telemetry,
        runner_factory=factory,
    )
    state = runtime.get_case("case")
    assert result.tasks_submitted == worker_count + int(diagnosis), result.stop_reason
    assert len(set(entered)) == worker_count
    assert all(task.status == "completed" for task in state.tasks)
    assert len(state.observations) == len(state.findings) == worker_count
    assert state.reports[-1].kind == ("diagnosis" if diagnosis else "progress")
    assert state.memories[-1].kind == ("diagnosis" if diagnosis else "incomplete")
    if diagnosis:
        assert result.stop_reason == "case_completed"
        assert state.claims[0].validity == "current" and state.claims[0].version == 2
        assert all(issue.status == "resolved" for issue in state.review_issues)
        assert state.reports[-1].support == state.claims[0].support
        assert state.memories[-1].source_report.version == state.reports[-1].version
        assert result.diagnosis == state.reports[-1]
    records = runtime.store.executions(case_id="case", limit=1000)
    tool_records = [r for r in records if r.operation_kind == "tool"]
    assert len(tool_records) == worker_count
    for attempt in state.attempts:
        if next(t for t in state.tasks if t.task_id == attempt.task_id).kind == "review":
            assert any(
                r.operation_kind == "review" and r.attempt_id == attempt.attempt_id for r in records
            )
            continue
        tool = next(r for r in tool_records if r.attempt_id == attempt.attempt_id)
        assert tool.trace_id == attempt.trace_id
        assert tool.tool_call_id == "query"
        observation = next(o for o in state.observations if o.source.reference == tool.operation_id)
        registration = next(r for r in records if r.operation_id == observation.source_operation_id)
        assert registration.operation_kind == "evidence_registration"
        command = next(r for r in records if r.operation_id == registration.parent_operation_id)
        assert command.parent_operation_id == tool.operation_id
        assert registration.trace_id == command.trace_id == tool.trace_id
    requests = [r for r in records if r.operation_kind == "model_request"]
    assert len(requests) == 3 * worker_count + int(diagnosis)
    assert len({r.request_id for r in requests}) == 3 * worker_count + int(diagnosis)
    for record in requests:
        snapshot = runtime.store.request("case", record.request_id)
        assert runtime.store.artifacts.read(snapshot.provider_input)
        assert record.status == "succeeded"
    assert runtime.store.case_at_version("case", state.version) == state


@pytest.mark.anyio
async def test_failed_worker_replans_after_progress_while_running(runtime, case, clock):
    release = asyncio.Event()
    limits = RunLimits(format_repairs=0, output_tokens=512)
    plans = [
        {
            "choice": "investigate",
            "reason": "inspect",
            "task": {
                "kind": "explore",
                "goal": "inspect",
                "scope": case.scope.model_dump(mode="json"),
                "completion_conditions": ["inspect"],
                "allowed_tools": ["telemetry_query"],
            },
        },
        {"choice": "progress", "reason": "worker is running"},
        {"choice": "pause", "reason": "worker failed; preserve explicit gap"},
    ]

    class PlannerProvider(FakeProvider):
        def stream_response(self, **kwargs):
            if len(self.calls) == 1:
                release.set()
            return super().stream_response(**kwargs)

    class WorkerProvider(FakeProvider):
        async def stream_response(self, **kwargs):
            await release.wait()
            async for event in super().stream_response(**kwargs):
                yield event

    planner = PlannerProvider(
        [[assistant_done(AssistantMessage(content=json.dumps(p)))] for p in plans]
    )
    worker = WorkerProvider(
        [[assistant_done(AssistantMessage(content="invalid structured output"))]]
    )
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")

    @asynccontextmanager
    async def factory(attempt):
        yield runner(runtime, clock, worker, limits)

    result = await run_investigation(
        runtime,
        "case",
        runner=runner(runtime, clock, planner, limits),
        telemetry=telemetry,
        catalog=telemetry,
        runner_factory=factory,
    )
    assert runtime.get_case("case").tasks[0].status == "blocked"
    assert len(planner.calls) == 3
    assert "Currently running tasks: []" in planner.calls[-1][1]
    assert result.stop_reason == "pause: worker failed; preserve explicit gap"
    assert result.tasks_submitted == 0


@pytest.mark.anyio
async def test_optional_step_checkpoint_keeps_planned_work_ready(runtime, case, clock):
    limits = RunLimits(checkpoint_steps=1, format_repairs=0, output_tokens=512)
    planner = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=json.dumps(
                            {
                                "choice": "investigate",
                                "reason": "inspect checkout",
                                "task": {
                                    "kind": "explore",
                                    "goal": "inspect checkout",
                                    "scope": case.scope.model_dump(mode="json"),
                                    "completion_conditions": ["inspect checkout"],
                                    "allowed_tools": ["telemetry_query"],
                                },
                            }
                        )
                    )
                )
            ]
        ]
    )
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    result = await run_investigation(
        runtime,
        "case",
        runner=runner(runtime, clock, planner, limits),
        telemetry=telemetry,
        catalog=telemetry,
    )
    state = runtime.get_case("case")
    assert result.stop_reason == "checkpoint_steps"
    assert state.investigation_status == "paused"
    assert len(state.tasks) == 1 and state.tasks[0].status == "ready"
    assert "budget" not in state.tasks[0].model_dump()
    assert state.attempts == () and state.reports[-1].kind == "progress"
    assert runtime.store.usage_totals("case")[0] == 1
    assert "This is the final response" not in planner.calls[0][1]


@pytest.mark.anyio
async def test_resuming_step_checkpoint_starts_new_cycle_without_resetting_usage(
    runtime, case, clock
):
    limits = RunLimits(checkpoint_steps=1, format_repairs=0, output_tokens=512)
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    for cycle in range(2):
        planner = FakeProvider(
            [
                [
                    assistant_done(
                        AssistantMessage(
                            content=json.dumps(
                                {"choice": "progress", "reason": "await more evidence"}
                            )
                        )
                    )
                ]
            ]
        )
        result = await run_investigation(
            runtime,
            "case",
            runner=runner(runtime, clock, planner, limits),
            telemetry=telemetry,
            catalog=telemetry,
            resume=cycle > 0,
        )
        assert result.stop_reason == "checkpoint_steps"
        assert runtime.get_case("case").investigation_status == "paused"
        assert runtime.store.usage_totals("case")[0] == cycle + 1
        assert result.diagnosis is not None and result.diagnosis.kind == "progress"


@pytest.mark.anyio
async def test_invalid_final_worker_step_checkpoints_instead_of_failing_task(runtime, case, clock):
    limits = RunLimits(checkpoint_steps=2, concurrency=1, format_repairs=0, output_tokens=512)
    planner = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=json.dumps(
                            {
                                "choice": "investigate",
                                "reason": "inspect checkout",
                                "task": {
                                    "kind": "explore",
                                    "goal": "inspect checkout",
                                    "scope": case.scope.model_dump(mode="json"),
                                    "completion_conditions": ["inspect checkout"],
                                    "allowed_tools": ["telemetry_query"],
                                },
                            }
                        )
                    )
                )
            ]
        ]
    )
    worker = FakeProvider(
        [
            [
                assistant_done(
                    AssistantMessage(
                        content=[ToolCall(id="late", name="telemetry_query", arguments={})]
                    )
                )
            ]
        ]
    )
    telemetry = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")

    @asynccontextmanager
    async def factory(attempt):
        yield runner(runtime, clock, worker, limits)

    result = await run_investigation(
        runtime,
        "case",
        runner=runner(runtime, clock, planner, limits),
        telemetry=telemetry,
        catalog=telemetry,
        runner_factory=factory,
    )
    state = runtime.get_case("case")
    assert result.stop_reason == "checkpoint_steps"
    assert state.investigation_status == "paused"
    assert state.tasks[0].status == "ready" and state.tasks[0].active_attempt_id is None
    assert state.attempts[0].status == "cancelled"
    assert state.findings == () and state.reports[-1].kind == "progress"
    assert runtime.store.usage_totals("case")[0] == 2
    assert {tool.name for tool in worker.calls[0][3]} == {
        "telemetry_query",
        "save_progress",
        "context_read",
    }
