import pytest

from tau_agent.messages import Usage
from tau_incident.budget import BudgetExceeded, RequestBudget, RunLimits, StepCheckpoint
from tau_incident.events import Command, ExtendBudget
from tau_incident.recovery import recover
from tau_incident.store.control import (
    OwnershipConflict,
    acquire,
    capacity_used,
    policy,
    release_role,
    require_owner,
    reserve_role,
)


def setup_budget(runtime, clock, limits):
    lease = runtime.acquire_owner("case", limits)
    budget = RequestBudget(runtime.store, limits, clock)
    budget.owner_id, budget.owner_generation = lease.owner_id, lease.generation
    reserve_role(runtime.store, "case", "role", lease.owner_id, lease.generation)
    return lease, budget


def test_unknown_usage_preserved_and_settlement_idempotent(runtime, case, clock):
    _, budget = setup_budget(runtime, clock, RunLimits(output_tokens=128))
    budget.reserve("request", "case", 100, None, role_operation_id="role")
    budget.settle("request", None, None)
    assert runtime.store.request_usage("case", "request")["charged_tokens"] == 228
    assert runtime.store.request_usage("case", "request")["state"] == "unknown"
    usage = Usage(input=10, output=5, total_tokens=15)
    budget.settle("request", 15, usage.model_dump_json())
    totals = runtime.store.usage_totals("case")
    budget.settle("request", None, None)
    budget.settle("request", 15, usage.model_dump_json())
    assert runtime.store.usage_totals("case") == totals
    assert runtime.store.request_usage("case", "request")["charged_tokens"] == 15


def test_budget_reservations_compete_at_actual_request_size(runtime, case, clock):
    limits = RunLimits(output_tokens=128, token_limit=476)
    _, budget = setup_budget(runtime, clock, limits)
    budget.reserve("first", "case", 200, None, role_operation_id="role")
    with pytest.raises(BudgetExceeded):
        budget.reserve("second", "case", 200, None, role_operation_id="role")
    assert runtime.store.usage_totals("case")[0] == 1


def test_new_case_has_no_cumulative_call_ceiling_or_dead_report_call_reserve(runtime, case, clock):
    limits = RunLimits(output_tokens=128)
    _, budget = setup_budget(runtime, clock, limits)
    assert limits.token_limit is None
    assert "call_limit" not in RunLimits.model_fields
    for index in range(3):
        budget.reserve(f"request-{index}", "case", 100, None, role_operation_id="role")
    summary = runtime.store.budget_summary("case")
    assert summary["calls"] == 3
    assert summary["available_tokens"] is None


def test_case_policy_can_be_set_extended_and_cleared_without_resetting_usage(runtime, case, clock):
    _, budget = setup_budget(runtime, clock, RunLimits(output_tokens=128))
    budget.reserve("charged", "case", 100, None, role_operation_id="role")
    totals = runtime.store.usage_totals("case")
    for identity, changes, expected in (
        ("set", {"token_limit": 1000}, 1000),
        ("extend", {"tokens": 500}, 1500),
        ("clear", {"clear_token_limit": True}, None),
    ):
        command = Command(
            command_id=identity,
            case_id="case",
            payload=ExtendBudget(scope=case.scope, reason=identity, **changes),
        )
        assert runtime.execute(command).status == "accepted"
        assert policy(runtime.store, "case").token_limit == expected
        assert runtime.store.usage_totals("case") == totals


def test_optional_step_checkpoint_is_atomic_and_preserves_case_usage(runtime, case, clock):
    limits = RunLimits(output_tokens=128, checkpoint_steps=1)
    _, budget = setup_budget(runtime, clock, limits)
    budget.checkpoint_start_calls = 0
    competing = RequestBudget(runtime.store, limits, clock)
    competing.owner_id, competing.owner_generation = budget.owner_id, budget.owner_generation
    competing.checkpoint_start_calls = 0
    budget.reserve("first", "case", 100, None, role_operation_id="role")
    with pytest.raises(StepCheckpoint, match="checkpoint"):
        competing.reserve("second", "case", 100, None, role_operation_id="role")
    assert runtime.store.usage_totals("case")[0] == 1
    assert runtime.store.budget_summary("case")["available_tokens"] is None


def test_expired_owner_is_fenced_and_budget_survives_takeover(runtime, case, clock):
    lease = runtime.acquire_owner("case", RunLimits(token_limit=7000))
    with pytest.raises(OwnershipConflict):
        acquire(runtime.store, "case", "other", 30, RunLimits())
    clock.advance(31)
    replacement = acquire(runtime.store, "case", "other", 30, RunLimits(token_limit=100000))
    assert replacement.generation == lease.generation + 1
    assert policy(runtime.store, "case").token_limit == 7000
    with pytest.raises(OwnershipConflict):
        require_owner(runtime.store, "case", lease.owner_id, lease.generation)
    runtime.release_owner()
    require_owner(runtime.store, "case", replacement.owner_id, replacement.generation)


def test_planning_and_review_share_concurrency_capacity(runtime, case):
    lease = runtime.acquire_owner("case", RunLimits(concurrency=2))
    for role in ("planning", "review"):
        reserve_role(runtime.store, "case", role, lease.owner_id, lease.generation)
    assert capacity_used(runtime.store, "case") == 2
    with pytest.raises(OwnershipConflict, match="capacity"):
        reserve_role(runtime.store, "case", "repair", lease.owner_id, lease.generation)
    release_role(runtime.store, "planning")
    reserve_role(runtime.store, "case", "repair", lease.owner_id, lease.generation)
    assert capacity_used(runtime.store, "case") == 2


def test_recovery_preserves_unknown_end_and_links(runtime, case, clock):
    old = runtime.acquire_owner("case", RunLimits())
    operation = runtime.execution.start(
        "tool", case_id="case", command_id="uncompleted", runtime_generation=old.generation
    )
    clock.advance(31)
    new = runtime.acquire_owner("case", RunLimits())
    recover(runtime, new.generation)
    record = runtime.store.execution(operation.operation_id)
    assert record.status == "interrupted"
    assert record.finished_at is None
    assert record.duration_ms is None
    records = runtime.store.executions(case_id="case")
    recovery = next(r for r in records if r.operation_kind == "recovery")
    assert any(link.operation_id == operation.operation_id for link in recovery.links)
    recover(runtime, new.generation)
    assert runtime.store.execution(operation.operation_id) == record
