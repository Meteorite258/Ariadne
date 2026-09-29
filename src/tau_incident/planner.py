"""Structured planning, without tools or authority to overwrite domain state."""

from __future__ import annotations

import asyncio
from typing import Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from tau_agent.types import JSONValue
from tau_incident.context import DecisionContext
from tau_incident.executor import OutputInvalid, RoleRunner
from tau_incident.models import Model, Scope, VersionRef
from tau_incident.submission import scope_contains


class TaskProposal(Model):
    kind: Literal["explore", "distinguish", "verify", "diagnose"]
    goal: str = Field(min_length=1)
    scope: Scope
    completion_conditions: tuple[str, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = Field(min_length=1)
    related_claims: tuple[VersionRef, ...] = ()
    discriminating_question: str | None = None
    result_meaning: str | None = None
    prerequisites: tuple[VersionRef, ...] = Field(
        default=(),
        description="Completion dependencies only: kind must be task, object_id an existing "
        "task_id, version that task's contract_version, and case_id the current case. "
        "Use [] for independent work. Evidence references belong in plan basis, never here. "
        "Do not depend on a stopped task that cannot complete.",
    )


class WaitProposal(Model):
    task_id: str | None = Field(
        default=None,
        description="Only an existing ready task may wait. Never attach a wait to a running "
        "attempt; return progress while it runs.",
    )
    scope: Scope
    condition: dict[str, JSONValue] = Field(
        description='Supported conditions: {"kind":"time"}, '
        '{"kind":"evidence","after":"aware ISO timestamp"}, or '
        '{"kind":"watermark","source":"configured source",'
        '"signal":"logs|metrics|traces|configuration|deployments",'
        '"watermark":"aware ISO timestamp"}.'
    )
    next_check_at: AwareDatetime
    deadline: AwareDatetime
    on_timeout: Literal["resume", "cancel", "pause"]
    interval_seconds: float = Field(default=30, gt=0)


class PlanOutput(Model):
    choice: Literal[
        "investigate",
        "revise",
        "continue",
        "defer",
        "progress",
        "wait",
        "needs_input",
        "pause",
        "synthesize",
    ]
    reason: str = Field(min_length=1)
    basis: tuple[VersionRef, ...] = Field(
        default=(),
        description="Copy references only from authoritative context.selection. Use [] when "
        "selection is empty. Visible case, task and attempt IDs are not automatically "
        "authorized basis references.",
    )
    task: TaskProposal | None = None
    wait: WaitProposal | None = None
    task_id: str | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.choice in {"investigate", "revise", "synthesize"}) != (self.task is not None):
            raise ValueError("investigate/revise/synthesize requires a task contract")
        if (self.choice == "wait") != (self.wait is not None):
            raise ValueError("wait choice requires a structured condition")
        if (self.choice in {"continue", "revise", "defer"}) != (self.task_id is not None):
            raise ValueError("continue/revise/defer requires an existing task ID")
        if self.choice == "synthesize" and self.task and self.task.kind != "diagnose":
            raise ValueError("synthesis requires a diagnosis task")
        return self


class Planner:
    def __init__(self, runner: RoleRunner) -> None:
        self.runner = runner

    async def plan(self, context: DecisionContext) -> PlanOutput:
        case = context.brief.case
        operation = self.runner.recorder.start(
            "planning",
            case_id=case.case_id,
            command_id=uuid4().hex,
            runtime_generation=self.runner.budget.owner_generation or 0,
        )

        def validate(output: PlanOutput) -> None:
            from tau_incident.information import information_signature

            if output.choice == "defer":
                selected = next((t for t in case.tasks if t.task_id == output.task_id), None)
                if (
                    selected is None
                    or selected.kind in {"review", "repair"}
                    or selected.status == "running"
                ):
                    raise OutputInvalid(
                        "defer requires a stopped investigation task and a relevance reason"
                    )

            signature = information_signature(self.runner.recorder.store, case)
            if (
                output.choice == "progress"
                and case.decisions
                and (
                    case.decisions[-1].choice == "progress"
                    and case.decisions[-1].information_signature == signature
                    and not any(t.status == "running" for t in case.tasks)
                )
            ):
                raise OutputInvalid(
                    "No new information since the previous idle progress decision. "
                    "Choose a different check, a testable wait, a specific user "
                    "question or an explicit pause."
                )
            if any(ref not in context.selection for ref in output.basis):
                raise OutputInvalid("plan basis was not supplied in this decision context")
            task = output.task
            if (
                task is not None
                and output.choice == "investigate"
                and any(
                    t.goal == task.goal
                    and t.scope == task.scope
                    and t.completion_conditions == task.completion_conditions
                    and t.status in {"ready", "running", "completed"}
                    for t in case.tasks
                )
                and case.decisions
                and case.decisions[-1].information_signature == signature
            ):
                raise OutputInvalid(
                    "Repeated plan on unchanged information. Continue or revise the "
                    "existing task, or propose a discriminating check."
                )
            if output.task_id is not None and not any(
                t.task_id == output.task_id
                and (
                    t.status in {"ready", "blocked", "cancelled"}
                    or output.choice in {"revise", "defer"}
                    and t.status == "completed"
                )
                and t.kind not in {"review", "repair"}
                for t in case.tasks
            ):
                raise OutputInvalid("continue/revise requires stopped or ready investigation work")
            if output.choice == "continue" and any(
                a.task_id == output.task_id and a.error_category == "context"
                for a in case.attempts[-1:]
            ):
                raise OutputInvalid(
                    "Task input still exceeds the window. Revise or split its "
                    "contract instead of retrying unchanged work."
                )
            if task is not None and any(
                r.kind != "claim" or r not in context.selection for r in task.related_claims
            ):
                raise OutputInvalid("related claims must use supplied claim revisions")
            if task is not None and (
                not scope_contains(case.scope, task.scope)
                or not set(task.allowed_tools) <= set(context.available_tools)
            ):
                raise OutputInvalid("plan exceeds case scope or available capabilities")
            if task is not None and any(
                r.kind != "task"
                or r.case_id != case.case_id
                or not any(
                    t.task_id == r.object_id and t.contract_version == r.version for t in case.tasks
                )
                for r in task.prerequisites
            ):
                raise OutputInvalid(
                    "task prerequisite does not identify an existing contract; use kind=task, "
                    "the existing task_id and its contract_version, or [] for independent work. "
                    "Evidence is not a task prerequisite."
                )
            if output.wait is not None:
                from tau_incident.models import Source, WaitCondition

                wait = output.wait
                WaitCondition(
                    wait_id="validation",
                    case_id=case.case_id,
                    version=1,
                    source=Source(kind="runtime", actor="planner"),
                    **wait.model_dump(),
                )
                if (
                    not scope_contains(case.scope, wait.scope)
                    or wait.deadline <= self.runner.budget.clock()
                    or wait.next_check_at > wait.deadline
                ):
                    raise OutputInvalid("wait scope or time bounds are invalid")
                if wait.task_id is not None and not any(
                    t.task_id == wait.task_id and t.status == "ready" for t in case.tasks
                ):
                    raise OutputInvalid("only a queued task can wait")
                if task is not None and (
                    wait.task_id is not None or not scope_contains(task.scope, wait.scope)
                ):
                    raise OutputInvalid(
                        "new task wait must use that task's scope and runtime identity"
                    )

        try:
            output = await self.runner.run(
                PlanOutput,
                view=context,
                tools=[],
                parent=operation,
                instructions="Choose a read-only task, a structured wait, or return progress. "
                "If existing running tasks cover the useful work, return progress; Runtime will "
                "wait for them and ask you again after they finish. A structured wait is only "
                "for external time/evidence availability, not for running workers. "
                "A task in blocked, cancelled or completed is not running. If a stopped "
                "task has evidence but no Finding, propose independent follow-up analysis "
                "when useful; do not wait for the stopped attempt or make it a prerequisite. "
                "Progress records an update and continues; it does not end the run. "
                "Use pause for a deliberate stop, needs_input for a specific user question, "
                "continue with task_id to resume work, or revise with task_id and a new contract. "
                "Use defer with task_id and a concrete reason only for unrelated follow-up work; "
                "the diagnosis reviewer must assess this relevance decision before delivery. "
                "State the discriminating question and how each result would "
                "change the causal explanation. "
                "For error_category=context, narrow or split the contract using "
                "the recorded required input size. "
                "Check previous tasks and Findings; avoid queries that cannot add information. "
                "Catalog metadata identifies available sources, not the incident cause. "
                "When the case asks for incident signal analysis, include bounded signal reads "
                "in the first task together with any needed source discovery. A catalog-only "
                "task is appropriate only when source discovery itself is the requested goal "
                "or signal queries are unavailable. "
                "For causal symptoms, propose bounded telemetry and deployment reads after "
                "discovering sources; do not repeat catalog-only work when those sources "
                "are already known. Unresolved review of a catalog interpretation does not "
                "block an independent task to collect incident signal evidence. "
                "If proposing a task, set choice to investigate and provide task; progress "
                "requires task=null and wait=null. A structured wait requires choice=wait, "
                "wait non-null, and task=null. "
                "A proposed task scope must stay inside the case scope; do not add entities "
                "that the case did not authorize. Put prerequisites inside task, and keep "
                "wait at the top level. "
                "When reviewed interpretations support synthesis, choose a diagnose task. "
                "Its output must be a diagnosis claim with explicit premises and current evidence. "
                "Important conclusions require independent review; never declare the case solved. "
                "Current runtime time: "
                + self.runner.budget.clock().isoformat()
                + ". Currently running tasks: "
                + str([t.task_id for t in case.tasks if t.status == "running"]),
                validate=validate,
            )
            self.runner.recorder.finish(
                operation.operation_id,
                status="succeeded",
                result=output.choice,
                artifact_ids=self.runner.recorder.store.execution(
                    operation.operation_id
                ).artifact_ids,
            )
            return output
        except BaseException as exc:
            self.runner.recorder.finish(
                operation.operation_id,
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                result="planning_failed",
                error_category="planning",
                detail=str(exc),
                artifact_ids=self.runner.recorder.store.execution(
                    operation.operation_id
                ).artifact_ids,
            )
            raise
