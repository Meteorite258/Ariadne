"""Short-transaction control helpers. Called only by the authoritative CaseStore."""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from tau_incident.events import (
    AddObservation,
    Command,
    DispatchTasks,
    ExtendBudget,
    FinishAttempt,
    Lifecycle,
    RecordPlan,
    RecordRead,
    RecordWait,
    RetryTask,
    ReviseTask,
    SetTaskDisposition,
    SubmitFinding,
    UpdateTaskProgress,
)
from tau_incident.models import IncidentCase, InvestigationTask, OwnerLease

if TYPE_CHECKING:
    from tau_incident.budget import RunLimits
    from tau_incident.store import CaseStore


class OwnershipConflict(ValueError):
    pass


def ready_tasks(case: IncidentCase) -> tuple[InvestigationTask, ...]:
    ready = tuple(
        t
        for t in case.tasks
        if t.status == "ready"
        and all(
            any(
                d.task_id == r.object_id
                and d.status == "completed"
                and d.contract_version == r.version
                for d in case.tasks
            )
            for r in t.prerequisites
        )
    )
    exploration = [t for t in ready if t.kind not in {"review", "repair"}]
    quality = [t for t in ready if t.kind in {"review", "repair"}]
    required = {c.claim_id for c in case.claims if c.judgment == "diagnosis"}
    while True:
        ancestors = {r.object_id for c in case.claims if c.claim_id in required for r in c.premises}
        if ancestors <= required:
            break
        required.update(ancestors)
    quality.sort(key=lambda t: not any(r.object_id in required for r in t.related_claims))
    last = next(
        (t for a in reversed(case.attempts) for t in case.tasks if t.task_id == a.task_id), None
    )
    turn_quality = last is not None and last.kind not in {"review", "repair"}
    ordered: list[InvestigationTask] = []
    while exploration or quality:
        queue = quality if turn_quality else exploration
        fallback = exploration if turn_quality else quality
        ordered.append((queue or fallback).pop(0))
        turn_quality = not turn_quality
    return tuple(ordered)


def migrate(store: CaseStore) -> None:
    db = store._connection
    db.execute(
        "CREATE TABLE owners (case_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
        "generation INTEGER NOT NULL, expires_at TEXT NOT NULL)"
    )
    db.execute("CREATE TABLE policies (case_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    db.execute("CREATE TABLE request_results (request_id TEXT PRIMARY KEY, body TEXT NOT NULL)")
    db.execute(
        "CREATE TABLE role_slots (operation_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
        "generation INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE attempt_reservations (attempt_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
        "calls INTEGER NOT NULL, tokens INTEGER NOT NULL, state TEXT NOT NULL)"
    )
    db.execute("ALTER TABLE request_usage ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
    # Rebuild the Stage 2 projection under compatible reducers; preserve all event identities.
    from tau_incident.events import DomainEvent, reduce_case

    for row in db.execute("SELECT case_id FROM cases").fetchall():
        state = None
        for item in db.execute(
            "SELECT body FROM events WHERE case_id=? ORDER BY case_version", (row["case_id"],)
        ).fetchall():
            state = reduce_case(state, DomainEvent.model_validate_json(item["body"]))
        if state is not None:
            db.execute(
                "UPDATE cases SET body=? WHERE case_id=?", (state.model_dump_json(), state.case_id)
            )
            for finding in state.findings:
                store._save_version(state.case_id, "finding", finding.finding_id, 1)
    db.execute("PRAGMA user_version = 3")


def owner(store: CaseStore, case_id: str) -> OwnerLease | None:
    row = store._connection.execute("SELECT * FROM owners WHERE case_id=?", (case_id,)).fetchone()
    return OwnerLease.model_validate(dict(row)) if row else None


def require_owner(
    store: CaseStore, case_id: str, owner_id: str | None, generation: int | None
) -> None:
    lease = owner(store, case_id)
    if (
        lease is None
        or (lease.owner_id, lease.generation) != (owner_id, generation)
        or lease.expires_at <= store.clock()
    ):
        raise OwnershipConflict(
            "case ownership is absent, expired or belongs to another coordinator"
        )


def acquire(
    store: CaseStore, case_id: str, owner_id: str, seconds: float, limits: RunLimits | None
) -> OwnerLease:
    if seconds <= 0:
        raise ValueError("lease duration must be positive")
    with store._transaction():
        store.get_case(case_id)
        previous = owner(store, case_id)
        now = store.clock().astimezone(UTC)
        if previous and previous.expires_at > now:
            raise OwnershipConflict("case already has a live coordinator")
        lease = OwnerLease(
            case_id=case_id,
            owner_id=owner_id,
            generation=previous.generation + 1 if previous else 1,
            expires_at=now + timedelta(seconds=seconds),
        )
        store._connection.execute(
            "INSERT INTO owners VALUES (?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET "
            "owner_id=excluded.owner_id,generation=excluded.generation,"
            "expires_at=excluded.expires_at",
            (case_id, owner_id, lease.generation, lease.expires_at.isoformat()),
        )
        # Resume cannot silently reset or enlarge an established case budget.
        if limits is not None:
            store._connection.execute(
                "INSERT OR IGNORE INTO policies VALUES (?,?)", (case_id, limits.model_dump_json())
            )
            persisted = policy(store, case_id)
            configured = limits.model_copy(
                update={
                    "token_limit": persisted.token_limit,
                    "deadline": persisted.deadline,
                }
            )
            store._connection.execute(
                "UPDATE policies SET body=? WHERE case_id=?",
                (configured.model_dump_json(), case_id),
            )
        return lease


def policy(store: CaseStore, case_id: str) -> RunLimits:
    from tau_incident.budget import RunLimits

    row = store._connection.execute(
        "SELECT body FROM policies WHERE case_id=?", (case_id,)
    ).fetchone()
    if row is None:
        raise ValueError("case budget has not been configured")
    return RunLimits.model_validate_json(row[0])


def held(store: CaseStore, case_id: str, except_attempt: str = "") -> tuple[int, int]:
    row = store._connection.execute(
        "SELECT COALESCE(SUM(calls),0),COALESCE(SUM(tokens),0) FROM attempt_reservations "
        "WHERE case_id=? AND state='reserved' AND attempt_id!=?",
        (case_id, except_attempt),
    ).fetchone()
    return int(row[0]), int(row[1])


def capacity_used(store: CaseStore, case_id: str | None = None) -> int:
    restriction = " AND r.case_id=?" if case_id else ""
    now = store.clock().astimezone(UTC).isoformat()
    parameters = (now, case_id) if case_id else (now,)
    workers = store._connection.execute(
        "SELECT COUNT(*) FROM attempt_reservations r JOIN owners o ON r.case_id=o.case_id "
        "WHERE r.state='reserved' AND o.expires_at>?" + restriction,
        parameters,
    ).fetchone()[0]
    roles = store._connection.execute(
        "SELECT COUNT(*) FROM role_slots r JOIN owners o ON r.case_id=o.case_id "
        "WHERE r.generation=o.generation AND o.expires_at>?" + restriction,
        parameters,
    ).fetchone()[0]
    return int(workers + roles)


def capacity_limit(store: CaseStore, configured: int) -> int:
    """The strictest live coordinator setting governs this store's shared capacity."""
    from tau_incident.budget import RunLimits

    rows = store._connection.execute(
        "SELECT p.body FROM policies p JOIN owners o ON p.case_id=o.case_id WHERE o.expires_at>?",
        (store.clock().astimezone(UTC).isoformat(),),
    ).fetchall()
    return min(
        [configured, *(RunLimits.model_validate_json(r[0]).global_concurrency for r in rows)]
    )


def reserve_role(
    store: CaseStore, case_id: str, operation_id: str, owner_id: str | None, generation: int | None
) -> None:
    with store._transaction():
        require_owner(store, case_id, owner_id, generation)
        limits = policy(store, case_id)
        if (
            capacity_used(store) >= capacity_limit(store, limits.global_concurrency)
            or capacity_used(store, case_id) >= limits.concurrency
        ):
            raise OwnershipConflict("role concurrency capacity unavailable")
        store._connection.execute(
            "INSERT INTO role_slots VALUES (?,?,?)", (operation_id, case_id, generation)
        )


def release_role(store: CaseStore, operation_id: str) -> None:
    with store._transaction():
        store._connection.execute("DELETE FROM role_slots WHERE operation_id=?", (operation_id,))


def validate(store: CaseStore, command: Command, case: IncidentCase) -> str | None:
    payload = command.payload
    from tau_incident.submission import scope_contains

    if not isinstance(payload, AddObservation) and not scope_contains(case.scope, payload.scope):
        return "command scope exceeds case"
    lease = owner(store, case.case_id)
    # Late observations may survive their worker, but cannot change task completion.
    late_observation = isinstance(payload, AddObservation) and payload.attempt_id is not None
    if not late_observation and (
        lease is not None
        or isinstance(
            payload,
            (
                DispatchTasks,
                Lifecycle,
                RecordWait,
                ExtendBudget,
                RecordPlan,
                SubmitFinding,
                UpdateTaskProgress,
                FinishAttempt,
                RecordRead,
                ReviseTask,
                RetryTask,
                SetTaskDisposition,
            ),
        )
    ):
        try:
            require_owner(store, case.case_id, command.owner_id, command.owner_generation)
        except OwnershipConflict as exc:
            return str(exc)
    if isinstance(payload, (SubmitFinding, FinishAttempt, RecordRead, UpdateTaskProgress)):
        if isinstance(
            payload, (SubmitFinding, RecordRead, UpdateTaskProgress)
        ) and case.investigation_status in {
            "paused",
            "completed",
        }:
            return "case is not accepting active worker results"
        aid = (
            payload.finding.attempt_id if isinstance(payload, SubmitFinding) else payload.attempt_id
        )
        attempt = next((a for a in case.attempts if a.attempt_id == aid), None)
        if attempt is None or attempt.runtime_generation != command.owner_generation:
            return "stale attempt generation"
        task = next((t for t in case.tasks if t.task_id == attempt.task_id), None)
        if (
            task is None
            or task.active_attempt_id != aid
            or task.contract_version != attempt.contract_version
        ):
            return "attempt no longer owns the task contract"
        if (
            isinstance(payload, SubmitFinding)
            and store._connection.execute(
                "SELECT 1 FROM attempt_reservations "
                "WHERE attempt_id=? AND case_id=? AND state='reserved'",
                (aid, case.case_id),
            ).fetchone()
            is None
        ):
            return "attempt has no active budget reservation"
    if isinstance(payload, UpdateTaskProgress):
        attempt = next(a for a in case.attempts if a.attempt_id == payload.attempt_id)
        task = next(t for t in case.tasks if t.task_id == attempt.task_id)
        progress = payload.progress
        if (
            attempt.status != "running"
            or attempt.execution_token != payload.execution_token
            or task.task_id != payload.task_id
            or task.scope != payload.scope
            or task.contract_version != payload.contract_version
            or task.progress.version != payload.expected_progress_version
            or progress.version != task.progress.version + 1
            or progress.attempt_id != attempt.attempt_id
        ):
            return "progress contract, version or active execution conflict"
        allowed = set(attempt.basis) | set(attempt.reads)
        from tau_incident.context import evidence_ref

        allowed.update(
            evidence_ref(e) for e in case.observations if e.attempt_id == attempt.attempt_id
        )
        refs = (*progress.observations, *progress.basis, *progress.counterevidence)
        if any(r not in allowed for r in refs):
            return "progress cites material outside authorized reads"
        error = store._version_error(case.case_id, refs)
        if error:
            return error
        from tau_incident.progress import validate_work_facts

        error = validate_work_facts(store, case, attempt, progress)
        if error:
            return error
    if isinstance(payload, RecordPlan):
        if case.investigation_status in {"paused", "completed"} or payload.attempt is not None:
            return "planning requires an active case; dispatch uses DispatchTasks"
        if payload.constraint_ids is not None and set(payload.constraint_ids) != {
            c.input_id for c in case.constraints
        }:
            return "execution constraints changed during planning"
        if payload.wait is not None:
            wait_case = case
            if payload.task is not None:
                if payload.wait.task_id != payload.task.task_id:
                    return "new task wait identity mismatch"
                wait_case = case.model_copy(update={"tasks": (*case.tasks, payload.task)})
            error = validate(
                store,
                command.model_copy(
                    update={"payload": RecordWait(scope=payload.scope, wait=payload.wait)}
                ),
                wait_case,
            )
            if error:
                return error
    if isinstance(payload, SetTaskDisposition):
        task = next((t for t in case.tasks if t.task_id == payload.task_id), None)
        if (
            task is None
            or task.contract_version != payload.contract_version
            or task.status == "running"
            or task.active_attempt_id is not None
        ):
            return "branch disposition requires the current stopped task contract"
        if task.kind in {"review", "repair"}:
            return "quality work is resolved through its review issue"
    if isinstance(payload, RetryTask):
        task = next((t for t in case.tasks if t.task_id == payload.task_id), None)
        if task is None or task.status not in {"blocked", "cancelled"}:
            return "retry requires a stopped task and explicit reason"
        if task.kind in {"review", "repair"}:
            return "quality tasks require a new issue cycle via recheck"
    if isinstance(payload, ExtendBudget):
        modes = sum(
            (payload.token_limit is not None, payload.clear_token_limit, payload.tokens > 0)
        )
        if modes > 1:
            return "choose one token policy change: set, increase or clear"
        if not modes and payload.deadline is None:
            return "budget policy requires an explicit limit or deadline change"
        if payload.tokens and policy(store, case.case_id).token_limit is None:
            return "unlimited case has no cap to increase; set token_limit explicitly"
    if isinstance(payload, ExtendBudget) and payload.deadline is not None:
        row = store._connection.execute(
            "SELECT body FROM policies WHERE case_id=?", (case.case_id,)
        ).fetchone()
        current_deadline = policy(store, case.case_id).deadline if row else None
        if payload.deadline <= store.clock() or (
            current_deadline is not None and payload.deadline < current_deadline
        ):
            return "budget deadline must be in the future and cannot shorten the current deadline"
    if isinstance(payload, ReviseTask):
        from tau_incident.telemetry.tools import TOOL_NAMES

        task = payload.task
        previous_task = next((t for t in case.tasks if t.task_id == task.task_id), None)
        if previous_task is None or task.contract_version != previous_task.contract_version + 1:
            return "task revision requires the next contract version"
        if previous_task.kind in {"review", "repair"}:
            return "quality contracts are revised through their review issue cycle"
        if (
            task.case_id != case.case_id
            or not scope_contains(case.scope, task.scope)
            or task.status != "ready"
            or task.active_attempt_id is not None
            or task.kind not in {"explore", "distinguish", "verify", "diagnose"}
            or not task.allowed_tools
            or not set(task.allowed_tools) <= set(TOOL_NAMES)
            or not task.completion_conditions
        ):
            return "revised task has invalid scope, capabilities, state or changed budget"
        if any(
            r.kind != "task"
            or r.case_id != case.case_id
            or r.object_id == task.task_id
            or not any(
                t.task_id == r.object_id and t.contract_version == r.version for t in case.tasks
            )
            for r in task.prerequisites
        ):
            return "revision has invalid prerequisites"
    if isinstance(payload, DispatchTasks):
        from tau_incident.context import ContextBuilder

        limits = policy(store, case.case_id)
        if case.investigation_status in {"paused", "completed"}:
            return "case does not permit dispatch"
        if limits.deadline and store.clock() >= limits.deadline:
            return "case deadline reached"
        active = capacity_used(store, case.case_id)
        global_active = capacity_used(store)
        if active + len(payload.attempts) > limits.concurrency or global_active + len(
            payload.attempts
        ) > capacity_limit(store, limits.global_concurrency):
            return "concurrency capacity unavailable"
        seen: set[str] = set()
        for attempt in payload.attempts:
            task = next((t for t in case.tasks if t.task_id == attempt.task_id), None)
            if task is None or task.status != "ready" or task.task_id in seen:
                return "task is not ready or duplicated"
            seen.add(task.task_id)
            if any(
                a.attempt_id == attempt.attempt_id or a.execution_token == attempt.execution_token
                for a in case.attempts
            ):
                return "attempt identity already exists"
            if (
                attempt.case_id != case.case_id
                or attempt.scope != task.scope
                or attempt.contract_version != task.contract_version
                or attempt.runtime_generation != command.owner_generation
                or attempt.status != "running"
                or attempt.starting_case_version != case.version
                or attempt.basis != ContextBuilder.task_basis_for_dispatch(case, task.scope, task)
            ):
                return "dispatch identity, contract or basis mismatch"
            for ref in task.prerequisites:
                dependency = next((t for t in case.tasks if t.task_id == ref.object_id), None)
                if (
                    ref.kind != "task"
                    or ref.case_id != case.case_id
                    or dependency is None
                    or dependency.contract_version != ref.version
                    or dependency.status != "completed"
                ):
                    return "task prerequisites are not satisfied"
    if isinstance(payload, RecordWait):
        wait = payload.wait
        previous = next((w for w in case.waits if w.wait_id == wait.wait_id), None)
        if (
            wait.case_id != case.case_id
            or not scope_contains(case.scope, wait.scope)
            or wait.version != (previous.version + 1 if previous else 1)
        ):
            return "wait identity, scope or revision mismatch"
        if previous and (
            previous.status != "pending"
            or wait.task_id != previous.task_id
            or wait.condition != previous.condition
            or wait.deadline != previous.deadline
            or wait.on_timeout != previous.on_timeout
            or wait.scope != previous.scope
            or wait.source != previous.source
        ):
            return "wait already closed or contract changed"
        if previous is None and wait.status != "pending":
            return "new wait must be pending"
        if (
            previous is not None
            and wait.status == "timed_out"
            and store.clock() < previous.deadline
        ):
            return "wait deadline has not been reached"
        task = next((t for t in case.tasks if t.task_id == wait.task_id), None)
        if wait.task_id is not None and (task is None or task.status not in {"ready", "waiting"}):
            return "only queued tasks can wait"
        if previous is None and any(
            w.status == "pending" and w.task_id == wait.task_id for w in case.waits
        ):
            return "target already has a pending wait"
    if isinstance(payload, Lifecycle):
        if payload.action == "waiting" and case.investigation_status in {"paused", "completed"}:
            return "stopped cases must be explicitly resumed before waiting"
        if payload.action == "recover" and any(
            a.status == "running" and a.runtime_generation >= (command.owner_generation or 0)
            for a in case.attempts
        ):
            return "recovery cannot interrupt a current-generation attempt"
        if payload.action == "waiting" and (
            any(a.status == "running" for a in case.attempts) or ready_tasks(case)
        ):
            return "case has ready or in-flight work"
        if payload.action == "waiting" and not any(w.status == "pending" for w in case.waits):
            return "case waiting requires a durable condition"
        if payload.task_id is not None and not any(
            t.task_id == payload.task_id for t in case.tasks
        ):
            return "unknown task"
    return None


def apply(store: CaseStore, command: Command, before: IncidentCase, after: IncidentCase) -> None:
    payload = command.payload
    db = store._connection
    if isinstance(payload, DispatchTasks):
        for attempt in payload.attempts:
            db.execute(
                "INSERT INTO attempt_reservations VALUES (?,?,0,0,'reserved')",
                (attempt.attempt_id, command.case_id),
            )
            store._save_version(command.case_id, "attempt", attempt.attempt_id, 1)
    if isinstance(payload, ExtendBudget):
        from tau_incident.budget import RunLimits

        db.execute(
            "INSERT OR IGNORE INTO policies VALUES (?,?)",
            (command.case_id, RunLimits().model_dump_json()),
        )
        limits = policy(store, command.case_id)
        limits = limits.model_copy(
            update={
                "token_limit": (
                    None
                    if payload.clear_token_limit
                    else payload.token_limit
                    if payload.token_limit is not None
                    else (limits.token_limit or 0) + payload.tokens
                    if payload.tokens
                    else limits.token_limit
                ),
                "deadline": payload.deadline or limits.deadline,
            }
        )
        db.execute(
            "UPDATE policies SET body=? WHERE case_id=?",
            (limits.model_dump_json(), command.case_id),
        )
    for attempt in after.attempts:
        if attempt.status != "running":
            db.execute(
                "UPDATE attempt_reservations SET state='released',calls=0,tokens=0 "
                "WHERE attempt_id=?",
                (attempt.attempt_id,),
            )
            db.execute(
                "UPDATE request_usage SET state='unknown' WHERE attempt_id=? AND state='reserved'",
                (attempt.attempt_id,),
            )
    if isinstance(payload, Lifecycle) and payload.action in {
        "pause",
        "cancel",
        "recover",
        "budget_stop",
    }:
        db.execute(
            "UPDATE request_usage SET state='unknown' "
            "WHERE case_id=? AND state='reserved' AND attempt_id IS NULL",
            (command.case_id,),
        )
    for task in after.tasks:
        store._save_version(command.case_id, "task", task.task_id, task.contract_version)
    for wait in after.waits:
        store._save_version(command.case_id, "wait", wait.wait_id, wait.version)
