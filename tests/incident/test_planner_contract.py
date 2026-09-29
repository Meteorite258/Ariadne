import json

import pytest

from pi_event_helpers import assistant_done
from tau_agent import AssistantMessage
from tau_ai import FakeProvider
from tau_incident.budget import RunLimits
from tau_incident.executor import OutputInvalid
from tau_incident.planner import Planner

from .test_dispatch_wait import queue_task
from .test_investigation import runner


@pytest.mark.anyio
async def test_visible_case_id_does_not_authorize_plan_basis(runtime, case, clock):
    limits = RunLimits(format_repairs=0)
    lease = runtime.acquire_owner("case", limits)
    output = {
        "choice": "progress",
        "reason": "record progress",
        "basis": [{"case_id": "case", "kind": "case", "object_id": "case", "version": 1}],
    }
    provider = FakeProvider([[assistant_done(AssistantMessage(content=json.dumps(output)))]])
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    view = role.builder.build_decision("case", tools=(), sources="fixture", budget_limits={})
    assert view.selection == ()
    with pytest.raises(OutputInvalid, match="plan basis was not supplied"):
        await Planner(role).plan(view)
    assert not runtime.get_case("case").decisions
    assert len(provider.calls) == 1
    system = provider.calls[0][1]
    assert "Copy references only from authoritative context.selection" in system
    assert "do not add entities that the case did not authorize" in system
    assert "progress requires task=null and wait=null" in system
    assert "Catalog metadata identifies available sources, not the incident cause" in system
    assert "include bounded signal reads" in system
    assert "does not block an independent task to collect incident signal evidence" in system
    assert "return progress" in system and clock().isoformat() in system


@pytest.mark.anyio
async def test_progress_with_proposed_task_is_rejected_without_dispatch(runtime, case, clock):
    limits = RunLimits(format_repairs=0)
    lease = runtime.acquire_owner("case", limits)
    output = {
        "choice": "progress",
        "reason": "I will propose an initial exploration task",
        "basis": [],
        "task": {
            "kind": "explore",
            "goal": "inspect checkout",
            "scope": case.scope.model_dump(mode="json"),
            "completion_conditions": ["inspect telemetry"],
            "allowed_tools": ["telemetry_query"],
            "prerequisites": [],
        },
        "wait": None,
    }
    provider = FakeProvider([[assistant_done(AssistantMessage(content=json.dumps(output)))]])
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    view = role.builder.build_decision(
        "case", tools=("telemetry_query",), sources="fixture", budget_limits={}
    )
    with pytest.raises(OutputInvalid, match="requires a task contract"):
        await Planner(role).plan(view)
    assert not runtime.get_case("case").tasks
    assert len(provider.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind,identity,version",
    [("evidence", "existing", 1), ("task", "missing", 1), ("task", "existing", 2)],
)
async def test_task_prerequisites_require_existing_contract(
    runtime, case, clock, kind, identity, version
):
    limits = RunLimits(format_repairs=0)
    lease = runtime.acquire_owner("case", limits)
    queue_task(runtime, case, "existing")
    output = {
        "choice": "investigate",
        "reason": "follow up",
        "task": {
            "kind": "explore",
            "goal": "inspect",
            "scope": case.scope.model_dump(mode="json"),
            "completion_conditions": ["inspect"],
            "allowed_tools": ["telemetry_query"],
            "prerequisites": [
                {"case_id": "case", "kind": kind, "object_id": identity, "version": version}
            ],
        },
    }
    provider = FakeProvider([[assistant_done(AssistantMessage(content=json.dumps(output)))]])
    role = runner(runtime, clock, provider, limits)
    role.budget.owner_id, role.budget.owner_generation = lease.owner_id, lease.generation
    view = role.builder.build_decision(
        "case", tools=("telemetry_query",), sources="fixture", budget_limits={}
    )
    with pytest.raises(OutputInvalid, match="task prerequisite does not identify"):
        await Planner(role).plan(view)
    assert len(runtime.get_case("case").tasks) == 1
    assert len(provider.calls) == 1
    assert "Evidence references belong in plan basis, never here" in provider.calls[0][1]
