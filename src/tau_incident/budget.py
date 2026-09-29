"""Persistent case budgets, attempt reservations and request usage reconciliation."""

from collections.abc import Callable
from datetime import datetime

from pydantic import AwareDatetime, Field, model_validator

from tau_incident.models import Model, TaskAttempt
from tau_incident.store import CaseStore


class BudgetExceeded(RuntimeError):
    pass


class StepCheckpoint(RuntimeError):
    """This run reached its optional model-step checkpoint; the case remains resumable."""


class RunLimits(Model):
    concurrency: int = Field(default=2, ge=1, le=32)
    global_concurrency: int = Field(default=8, ge=1, le=256)
    lease_seconds: float = Field(default=30, ge=3)
    shutdown_seconds: float = Field(default=5, ge=0, le=60)
    deadline: AwareDatetime | None = None
    format_repairs: int = Field(default=2, ge=0, le=5)
    max_repair_rounds: int = Field(default=2, ge=0, le=10)
    checkpoint_steps: int | None = Field(default=None, ge=1)
    token_limit: int | None = Field(default=None, ge=1)
    context_tokens: int = Field(default=32768, ge=1024)
    output_tokens: int = Field(default=4096, ge=128)

    @model_validator(mode="before")
    @classmethod
    def removed_limits(cls, value: object) -> object:
        removed = {
            "max_turns",
            "role_timeout_seconds",
            "max_tasks",
            "call_limit",
            "report_reserve_calls",
            "report_reserve_tokens",
        }
        if isinstance(value, dict) and removed.intersection(value):
            raise ValueError(
                "Removed incident limits: "
                + ", ".join(sorted(removed.intersection(value)))
                + (
                    ". Use optional checkpoint_steps to pause a run, token_limit for "
                    "a case allowance, "
                    "or deadline for an explicit case deadline. Historical cases are read-only."
                )
            )
        return value


class RequestBudget:
    def __init__(self, store: CaseStore, limits: RunLimits, clock: Callable[[], datetime]) -> None:
        self.store, self.limits, self.clock = store, limits, clock
        self.owner_id: str | None = None
        self.owner_generation: int | None = None
        self.checkpoint_start_calls: int | None = None
        if limits.output_tokens >= limits.context_tokens:
            raise ValueError("output allowance must be smaller than context window")

    def reserve(
        self,
        request_id: str,
        case_id: str,
        input_tokens: int,
        attempt: TaskAttempt | None,
        *,
        role_operation_id: str | None = None,
    ) -> None:
        limits = self.limits
        if input_tokens + limits.output_tokens > limits.context_tokens:
            from tau_incident.context import ContextInsufficient

            raise ContextInsufficient(
                "final serialized provider input exceeds context window",
                task_id=attempt.task_id if attempt else None,
                required_tokens=input_tokens,
                input_limit=limits.context_tokens - limits.output_tokens,
            )
        if limits.checkpoint_steps is not None and self.checkpoint_start_calls is None:
            self.checkpoint_start_calls = self.store.usage_totals(case_id)[0]
        self.store.reserve_request(
            request_id=request_id,
            case_id=case_id,
            attempt_id=attempt.attempt_id if attempt else None,
            tokens=input_tokens + limits.output_tokens,
            checkpoint_start_calls=self.checkpoint_start_calls,
            checkpoint_steps=limits.checkpoint_steps,
            owner_id=self.owner_id,
            owner_generation=self.owner_generation,
            role_operation_id=role_operation_id,
        )

    def remaining_requests(self, case_id: str, task_id: str | None = None) -> int | None:
        """Return the tightest request allowance for this run, case and task."""
        calls, _, _ = self.store.usage_totals(case_id)
        allowances: list[int] = []
        if self.limits.checkpoint_steps is not None:
            if self.checkpoint_start_calls is None:
                self.checkpoint_start_calls = calls
            allowances.append(self.limits.checkpoint_steps - (calls - self.checkpoint_start_calls))
        return min(allowances) if allowances else None

    def settle(self, request_id: str, tokens: int | None, usage: str | None) -> None:
        self.store.settle_request(request_id, tokens, usage)

    def reconcile(self, request_id: str) -> None:
        """Reconcile only a durable response; absence retains the conservative reservation."""
        from tau_agent.messages import AssistantMessage
        from tau_incident.models import ArtifactRef

        result = self.store.request_result(request_id)
        if result is None:
            self.settle(request_id, None, None)
            return
        artifact = ArtifactRef.model_validate(result["artifact"])
        message = AssistantMessage.model_validate_json(self.store.artifacts.read(artifact))
        usage = message.usage if message.usage.total_tokens > 0 else None
        self.settle(
            request_id,
            usage.total_tokens if usage else None,
            usage.model_dump_json() if usage else None,
        )
