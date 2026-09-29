"""Lease-owned bounded dispatch, model planning, waiting and persistent recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING
from uuid import uuid4

from tau_incident.budget import BudgetExceeded, StepCheckpoint
from tau_incident.events import Command, FinishAttempt, RecordPlan, RetryTask, ReviseTask
from tau_incident.executor import InvestigatorExecutor, OutputInvalid, RoleRunner
from tau_incident.models import (
    Decision,
    DiagnosisReport,
    InvestigationTask,
    Model,
    Source,
    TaskAttempt,
    VersionRef,
    WaitCondition,
)
from tau_incident.planner import Planner
from tau_incident.recovery import WaitProbe, check_wait, recover
from tau_incident.reporting import ProgressReport
from tau_incident.store.control import OwnershipConflict, policy, ready_tasks, require_owner
from tau_incident.submission import SubmissionService
from tau_incident.telemetry import ServiceCatalog, TelemetryProvider
from tau_incident.telemetry.tools import TOOL_NAMES

if TYPE_CHECKING:
    from tau_incident.coordinator import IncidentRuntime

RunnerFactory = Callable[[TaskAttempt], AbstractAsyncContextManager[RoleRunner]]


class RunResult(Model):
    stop_reason: str
    tasks_submitted: int
    usage: tuple[int, int, int]
    report: ProgressReport
    diagnosis: DiagnosisReport | None = None


async def run_investigation(
    runtime: IncidentRuntime,
    case_id: str,
    *,
    runner: RoleRunner,
    telemetry: TelemetryProvider,
    catalog: ServiceCatalog,
    runner_factory: RunnerFactory | None = None,
    resume: bool = False,
    wait_probe: WaitProbe | None = None,
) -> RunResult:
    lease = runtime.acquire_owner(case_id, runner.budget.limits)
    limits = policy(runtime.store, case_id)
    checkpoint_start_calls = runtime.store.usage_totals(case_id)[0]
    runner.budget.limits = limits
    runner.budget.checkpoint_start_calls = checkpoint_start_calls
    runner.budget.owner_id, runner.budget.owner_generation = lease.owner_id, lease.generation
    active: dict[asyncio.Task[bool], TaskAttempt] = {}
    workers: dict[str, RoleRunner] = {}
    submitted = 0
    stop = "execution_limit"
    planning_done = False
    planned_inputs: tuple[object, ...] | None = None
    lost: Exception | None = None
    supervisor = asyncio.current_task()
    runtime.running_task = supervisor

    def planning_inputs() -> tuple[object, ...]:
        state = runtime.get_case(case_id)
        return (
            tuple((t.task_id, t.progress.version) for t in state.tasks),
            tuple((e.evidence_id, e.version) for e in state.observations),
            tuple((w.wait_id, w.version) for w in state.waits),
            tuple((c.claim_id, c.version) for c in state.claims),
        )

    def checkpoint_reached() -> bool:
        return (
            limits.checkpoint_steps is not None
            and runtime.store.usage_totals(case_id)[0] - checkpoint_start_calls
            >= limits.checkpoint_steps
        )

    def stop_work(task_id: str | None) -> None:
        if task_id is None:
            runner.cancel()
        for future, attempt in active.items():
            if task_id in {None, attempt.task_id}:
                worker = workers.get(attempt.attempt_id)
                if worker is not None:
                    worker.cancel()
                future.cancel()
        if task_id is None and supervisor is not None and asyncio.current_task() is not supervisor:
            supervisor.cancel()

    runtime.on_stop = stop_work

    async def heartbeat() -> None:
        nonlocal lost
        while True:
            await asyncio.sleep(limits.lease_seconds / 3)
            try:
                runtime.renew_owner(limits.lease_seconds)
            except Exception as exc:
                lost = exc
                runner.cancel()
                for worker in workers.values():
                    worker.cancel()
                for future in active:
                    future.cancel()
                if supervisor is not None:
                    supervisor.cancel()
                return

    async def work(attempt: TaskAttempt) -> bool:
        async def execute(worker: RoleRunner) -> bool:
            worker.budget.limits = limits
            worker.budget.checkpoint_start_calls = checkpoint_start_calls
            worker.budget.owner_id, worker.budget.owner_generation = (
                lease.owner_id,
                lease.generation,
            )
            workers[attempt.attempt_id] = worker
            task = next(t for t in runtime.get_case(case_id).tasks if t.task_id == attempt.task_id)
            if task.kind == "review":
                from tau_incident.review import Reviewer

                result = await Reviewer(runtime, worker, telemetry, catalog).review(attempt)
                receipt = runtime.execute(
                    Command(
                        command_id=f"review:{attempt.attempt_id}",
                        case_id=case_id,
                        payload=result,
                    )
                )
                if receipt.status != "accepted":
                    raise ValueError(f"Review {receipt.status}: {receipt.reason}")
                return True
            finding = await InvestigatorExecutor(runtime, worker, telemetry, catalog).run(attempt)
            receipt = SubmissionService(runtime).submit(
                finding, execution_token=attempt.execution_token
            )
            if receipt.status != "accepted":
                raise ValueError(f"Finding {receipt.status}: {receipt.reason}")
            return True

        try:
            if runner_factory is not None:
                async with runner_factory(attempt) as worker:
                    return await execute(worker)
            return await execute(runner)
        except (Exception, asyncio.CancelledError) as exc:
            import sqlite3

            from tau_incident.coordinator import LocalRecordingError
            from tau_incident.failure import classify_failure
            from tau_incident.store import CommitUnknown

            category = classify_failure(exc)

            current = next(
                a for a in runtime.get_case(case_id).attempts if a.attempt_id == attempt.attempt_id
            )
            if current.final_command_id is not None:
                if isinstance(exc, (LocalRecordingError, CommitUnknown, sqlite3.Error, OSError)):
                    raise
                return True
            if isinstance(exc, OutputInvalid) and checkpoint_reached():
                raise StepCheckpoint("final model step ended without valid role output") from exc
            if isinstance(exc, StepCheckpoint):
                raise
            if current.status == "running" and lost is None:
                receipt = runtime.execute(
                    Command(
                        command_id=uuid4().hex,
                        case_id=case_id,
                        payload=FinishAttempt(
                            scope=attempt.scope,
                            attempt_id=attempt.attempt_id,
                            execution_token=attempt.execution_token,
                            status="cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "unknown"
                            if isinstance(exc, (LocalRecordingError, CommitUnknown))
                            else "failed",
                            reason=f"{type(exc).__name__}: {exc}",
                            error_category=category,
                        ),
                    )
                )
                if receipt.status != "accepted":
                    raise ValueError(
                        f"cannot persist worker termination: {receipt.reason}"
                    ) from exc
            if isinstance(exc, BudgetExceeded) or category in {"persistence", "commit_unknown"}:
                raise
            return False
        finally:
            workers.pop(attempt.attempt_id, None)

    pulse = asyncio.create_task(heartbeat())
    try:
        recover(runtime, lease.generation)
        if resume:
            receipt = runtime.resume(case_id)
            if receipt.status != "accepted":
                raise ValueError(receipt.reason)
        while True:
            if lost is not None:
                raise lost
            require_owner(runtime.store, case_id, lease.owner_id, lease.generation)
            case = runtime.get_case(case_id)
            if planning_done and planned_inputs is not None and planned_inputs != planning_inputs():
                planning_done = False
            if case.investigation_status in {"paused", "completed"}:
                stop = "case_" + case.investigation_status
                break
            if limits.deadline and runtime.clock() >= limits.deadline:
                raise BudgetExceeded("case deadline reached")
            for future in tuple(active):
                current = next(
                    a for a in case.attempts if a.attempt_id == active[future].attempt_id
                )
                if current.status != "running" and not future.done():
                    future.cancel()
                if future.done():
                    del active[future]
                    # Every terminal worker outcome changes what the planner can do.
                    planning_done = False
                    if not future.cancelled():
                        try:
                            accepted = future.result()
                        except StepCheckpoint:
                            # A sibling may already have a request in flight. Drain it
                            # before recording the case pause.
                            continue
                        submitted += int(accepted)
            for condition in case.waits:
                if await check_wait(runtime, condition, runtime.clock, wait_probe):
                    planning_done = False
            case = runtime.get_case(case_id)
            if case.investigation_status == "paused":
                continue
            if any(c.judgment == "diagnosis" and c.validity == "current" for c in case.claims):
                from tau_incident.reporting import ReportBuilder

                builder = ReportBuilder(runtime)
                report = builder.build(case_id)
                if report.kind == "diagnosis":
                    builder.commit(report)
                    stop = "case_completed"
                    break
            if checkpoint_reached():
                if active:
                    await asyncio.wait(
                        active,
                        timeout=min(1.0, limits.lease_seconds / 3),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue
                stop = "checkpoint_steps"
                receipt = runtime.pause(case_id, "optional model-step checkpoint reached")
                if receipt.status != "accepted":
                    raise ValueError(receipt.reason)
                break
            capacity = (limits.concurrency if runner_factory else 1) - len(active)
            from tau_incident.review import schedule

            if capacity > 0:
                while schedule(runtime, case_id):
                    pass
            case = runtime.get_case(case_id)
            last_task = next(
                (t for a in reversed(case.attempts) for t in case.tasks if t.task_id == a.task_id),
                None,
            )
            plan_before_quality = (
                not planning_done
                and capacity > 0
                and last_task is not None
                and last_task.kind in {"review", "repair"}
                and not any(t.kind not in {"review", "repair"} for t in ready_tasks(case))
            )
            if capacity > 0 and not plan_before_quality:
                attempts = runtime.claim_ready_tasks(
                    lease.generation,
                    capacity,
                )
                for attempt in attempts:
                    active[asyncio.create_task(work(attempt))] = attempt
            case = runtime.get_case(case_id)
            if (
                not planning_done
                and len(active) < (limits.concurrency if runner_factory else 1)
                and (plan_before_quality or not any(t.status == "ready" for t in case.tasks))
                and not any(w.status == "pending" and w.task_id is None for w in case.waits)
            ):
                context = runner.builder.build_decision(
                    case_id,
                    tools=tuple(
                        name
                        for name in TOOL_NAMES
                        if name != "python_analysis" or runtime.analysis is not None
                    ),
                    sources="Queryable telemetry: " + ", ".join(telemetry.capabilities()),
                    budget_limits={
                        **(
                            {"tokens": limits.token_limit} if limits.token_limit is not None else {}
                        ),
                        **(
                            {"checkpoint_steps": limits.checkpoint_steps}
                            if limits.checkpoint_steps is not None
                            else {}
                        ),
                    },
                )
                try:
                    plan = await Planner(runner).plan(context)
                except Exception as exc:
                    from tau_incident.failure import classify_failure

                    if isinstance(exc, OutputInvalid) and checkpoint_reached():
                        raise StepCheckpoint("checkpoint without valid planner output") from exc
                    if classify_failure(exc) in {"transport", "output", "context"} and (
                        active or ready_tasks(runtime.get_case(case_id))
                    ):
                        # Preserve independent work after a bounded planning repair fails.
                        # A task result or new progress can make planning useful again.
                        planning_done = True
                        planned_inputs = planning_inputs()
                        stop = f"planner_blocked: {type(exc).__name__}: {exc}"
                        continue
                    raise
                source = Source(kind="runtime", actor="planner")
                from tau_incident.information import information_signature

                decision = Decision(
                    decision_id=uuid4().hex,
                    case_id=case_id,
                    version=1,
                    scope=case.scope,
                    source=source,
                    choice=plan.choice,
                    reason=plan.reason,
                    basis=plan.basis,
                    information_signature=information_signature(runtime.store, case),
                )
                task = None
                if plan.task:
                    task = InvestigationTask(
                        task_id=plan.task_id or uuid4().hex,
                        case_id=case_id,
                        contract_version=(
                            next(
                                t.contract_version for t in case.tasks if t.task_id == plan.task_id
                            )
                            + 1
                        )
                        if plan.choice == "revise"
                        else 1,
                        source=source,
                        kind=plan.task.kind,
                        goal=plan.task.goal,
                        scope=plan.task.scope,
                        completion_conditions=plan.task.completion_conditions,
                        allowed_tools=plan.task.allowed_tools,
                        prerequisites=plan.task.prerequisites,
                        related_claims=plan.task.related_claims,
                        discriminating_question=plan.task.discriminating_question,
                        result_meaning=plan.task.result_meaning,
                        rationale=(
                            VersionRef(
                                case_id=case_id,
                                kind="decision",
                                object_id=decision.decision_id,
                                version=decision.version,
                            ),
                        ),
                    )
                wait = None
                if plan.wait is not None:
                    wait = WaitCondition(
                        wait_id=uuid4().hex,
                        case_id=case_id,
                        version=1,
                        source=source,
                        **plan.wait.model_dump(exclude={"task_id"}),
                        task_id=task.task_id if task else plan.wait.task_id,
                    )
                receipt = runtime.execute(
                    Command(
                        command_id=uuid4().hex,
                        case_id=case_id,
                        payload=RecordPlan(
                            scope=case.scope,
                            decision=decision,
                            task=task if plan.choice != "revise" else None,
                            wait=wait,
                            read_basis=context.selection,
                            constraint_ids=tuple(
                                c.input_id for c in context.brief.case.constraints
                            ),
                        ),
                    ),
                    plan.basis,
                )
                if receipt.status != "accepted":
                    stop = f"plan_{receipt.status}: {receipt.reason}"
                    planning_done = True
                else:
                    if plan.choice == "revise" and task is not None:
                        revised = runtime.execute(
                            Command(
                                command_id=uuid4().hex,
                                case_id=case_id,
                                payload=ReviseTask(scope=case.scope, task=task, reason=plan.reason),
                            )
                        )
                        if revised.status != "accepted":
                            raise ValueError(revised.reason)
                    elif plan.choice == "continue":
                        selected_task = next(t for t in case.tasks if t.task_id == plan.task_id)
                        if selected_task.status != "ready":
                            retried = runtime.execute(
                                Command(
                                    command_id=uuid4().hex,
                                    case_id=case_id,
                                    payload=RetryTask(
                                        scope=case.scope,
                                        task_id=selected_task.task_id,
                                        reason=plan.reason,
                                    ),
                                )
                            )
                            if retried.status != "accepted":
                                raise ValueError(retried.reason)
                    elif plan.choice in {"pause", "needs_input"}:
                        runtime.pause(case_id, plan.choice + ": " + plan.reason)
                        stop = plan.choice + ": " + plan.reason
                        break
                    elif plan.choice == "defer":
                        from tau_incident.events import SetTaskDisposition

                        selected_task = next(t for t in case.tasks if t.task_id == plan.task_id)
                        deferred = runtime.execute(
                            Command(
                                command_id=uuid4().hex,
                                case_id=case_id,
                                payload=SetTaskDisposition(
                                    scope=case.scope,
                                    task_id=selected_task.task_id,
                                    contract_version=selected_task.contract_version,
                                    nonblocking=True,
                                    reason=plan.reason,
                                ),
                            )
                        )
                        if deferred.status != "accepted":
                            raise ValueError(deferred.reason)
                    elif plan.choice == "progress":
                        # Running work causes the next decision; an idle progress update asks again.
                        planning_done = bool(active or ready_tasks(case))
                planned_inputs = planning_inputs()
                continue
            if active:
                await asyncio.wait(
                    active,
                    timeout=min(1.0, limits.lease_seconds / 3),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue
            case = runtime.get_case(case_id)
            pending = [w for w in case.waits if w.status == "pending"]
            if pending:
                if not ready_tasks(case) and case.investigation_status != "waiting":
                    receipt = runtime.lifecycle(
                        case_id, "waiting", "no ready or in-flight work; durable conditions pending"
                    )
                    if receipt.status != "accepted":
                        raise ValueError(receipt.reason)
                due = min(min(w.next_check_at, w.deadline) for w in pending)
                await asyncio.sleep(max(0.05, min(1.0, (due - runtime.clock()).total_seconds())))
                continue
            if any(t.status == "ready" for t in case.tasks):
                stop = "dispatch_unavailable: dependency, capacity or budget"
            break
    except StepCheckpoint as exc:
        stop = "checkpoint_steps"
        if lost is None:
            if active:
                outcomes = await asyncio.gather(*active, return_exceptions=True)
                active.clear()
                for outcome in outcomes:
                    if isinstance(outcome, bool):
                        submitted += int(outcome)
                    elif not isinstance(outcome, StepCheckpoint):
                        stop = f"checkpoint_steps; pending execution: {outcome}"
            receipt = runtime.pause(case_id, "optional model-step checkpoint reached")
            if receipt.status != "accepted":
                raise ValueError(receipt.reason) from exc
    except asyncio.CancelledError:
        stop = "paused_on_shutdown"
        if lost is None:
            runtime.shutdown(case_id)
    except Exception as exc:
        stop = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, BudgetExceeded) and lost is None:
            runtime.lifecycle(case_id, "budget_stop", stop)
        elif not isinstance(exc, OwnershipConflict) and lost is None:
            runtime.pause(case_id, stop)
    finally:
        runtime.on_stop = None
        try:
            runner.cancel()
            for worker in workers.values():
                worker.cancel()
            try:
                if lost is None and runtime.get_case(case_id).investigation_status not in {
                    "paused",
                    "completed",
                }:
                    runtime.pause(case_id, "bounded runtime shutdown")
            finally:
                if active:
                    for future in active:
                        future.cancel()
                    done, pending_tasks = await asyncio.wait(
                        active, timeout=limits.shutdown_seconds
                    )
                    for future in done:
                        if not future.cancelled():
                            future.exception()
                    for future in pending_tasks:
                        runtime.pending_workers.add(future)
                        future.add_done_callback(runtime.pending_workers.discard)
                        future.add_done_callback(_consume_result)
                    if pending_tasks:
                        stop = (
                            "shutdown_timeout: old attempts fenced; worker cleanup remains pending"
                        )
                if lost is None and not runtime.pending_workers:
                    from tau_incident.memory import MemoryStore
                    from tau_incident.reporting import ReportBuilder

                    case = runtime.get_case(case_id)
                    if not case.reports or case.reports[-1].state != "current":
                        builder = ReportBuilder(runtime)
                        builder.commit(builder.build(case_id))
                    await MemoryStore(runtime).rebuild_pending(case_id)
        finally:
            pulse.cancel()
            try:
                await asyncio.gather(pulse, return_exceptions=True)
                runtime.release_owner()
            finally:
                runtime.running_task = None
    submitted = sum(
        a.runtime_generation == lease.generation and a.final_command_id is not None
        for a in runtime.get_case(case_id).attempts
    )
    return RunResult(
        stop_reason=stop,
        tasks_submitted=submitted,
        usage=runtime.store.usage_totals(case_id),
        report=runtime.report(case_id),
        diagnosis=runtime.get_case(case_id).reports[-1]
        if runtime.get_case(case_id).reports
        else None,
    )


def _consume_result(task: asyncio.Task[bool]) -> None:
    if not task.cancelled():
        task.exception()
