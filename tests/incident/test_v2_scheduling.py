from tau_incident.budget import RunLimits
from tau_incident.information import information_signature
from tau_incident.store.control import ready_tasks

from .test_dispatch_wait import queue_task


def test_repeated_task_records_do_not_count_as_new_information(runtime, case):
    runtime.acquire_owner("case", RunLimits())
    queue_task(runtime, case, "first")
    before = information_signature(runtime.store, runtime.get_case("case"))
    queue_task(runtime, case, "same-goal")
    assert information_signature(runtime.store, runtime.get_case("case")) == before


def test_quality_and_exploration_alternate_without_starvation(runtime, case):
    lease = runtime.acquire_owner("case", RunLimits(concurrency=2))
    tasks = [queue_task(runtime, case, identity) for identity in ("e1", "e2", "q1", "q2")]
    tasks[2:] = [t.model_copy(update={"kind": "review"}) for t in tasks[2:]]
    state = runtime.get_case("case").model_copy(update={"tasks": tuple(tasks)})
    assert [t.task_id for t in ready_tasks(state)] == ["e1", "q1", "e2", "q2"]
    executed = runtime.claim_ready_tasks(lease.generation, 1)[0]
    state = state.model_copy(
        update={
            "attempts": (executed,),
            "tasks": (
                tasks[0].model_copy(update={"status": "completed"}),
                *tasks[1:],
            ),
        }
    )
    assert [t.task_id for t in ready_tasks(state)] == ["q1", "e2", "q2"]


def test_initial_cap_has_policy_receipt_and_reacquire_does_not_reset_it(runtime, case):
    runtime.acquire_owner("case", RunLimits(token_limit=1000))
    receipt = runtime.store.receipt("initial-policy:case")
    assert receipt is not None and receipt.status == "accepted"
    event = next(e for e in runtime.events("case") if e.command_id == receipt.command_id)
    assert event.payload.kind == "extend_budget" and event.payload.token_limit == 1000
    runtime.release_owner()
    runtime.acquire_owner("case", RunLimits(token_limit=999999))
    assert runtime.store.receipt("initial-policy:case") == receipt
    assert runtime.store.budget_summary("case")["limits"]["token_limit"] == 1000


def test_competing_requests_reserve_one_case_cap_atomically(runtime, case, clock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tau_incident.budget import BudgetExceeded, RequestBudget
    from tau_incident.store import CaseStore
    from tau_incident.store.control import reserve_role

    limits = RunLimits(token_limit=500, output_tokens=128)
    lease = runtime.acquire_owner("case", limits)
    reserve_role(runtime.store, "case", "role-a", lease.owner_id, lease.generation)
    reserve_role(runtime.store, "case", "role-b", lease.owner_id, lease.generation)
    barrier = Barrier(2, timeout=5)

    def send(identity):
        store = CaseStore(runtime.store.path, artifacts=runtime.store.artifacts, clock=clock)
        try:
            budget = RequestBudget(store, limits, clock)
            budget.owner_id, budget.owner_generation = lease.owner_id, lease.generation
            barrier.wait()
            try:
                budget.reserve(identity, "case", 172, None, role_operation_id=identity)
            except BudgetExceeded:
                return "limited"
            return "reserved"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, ("role-a", "role-b")))
    assert sorted(outcomes) == ["limited", "reserved"]
    assert runtime.store.usage_totals("case")[:2] == (1, 300)
