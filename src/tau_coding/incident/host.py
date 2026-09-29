"""One explicitly owned local runtime; no coding session, provider or extension loading."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, uuid4, uuid5

from tau_coding.incident.actions import IncidentAction, IncidentQuery
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.settings import HostSettings
from tau_incident.coordinator import IncidentRuntime
from tau_incident.events import Command, CreateCase, DomainEvent, Receipt
from tau_incident.evidence import ArtifactStore, EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.models import ExecutionRecord, IncidentCase, OperationKind, VersionRef
from tau_incident.reporting import CaseBrief, ProgressReport
from tau_incident.store import CaseStore


def utc_now() -> datetime:
    return datetime.now(UTC)


class IncidentHost:
    def __init__(
        self,
        config: IncidentConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
        mode: Literal["embedded", "daemon"] = "embedded",
        settings: HostSettings | None = None,
    ) -> None:
        self.settings = settings or HostSettings(environment=config.environment)
        self.children: dict[str, IncidentHost] = {}
        self.jobs: set[asyncio.Task[None]] = set()
        self.scheduler_task: asyncio.Task[None] | None = None
        self.stopping = False
        self.instance_id = uuid4().hex
        self.closed = False
        self.config = config
        self.mode = mode
        from tau_incident.history import HistoricalCases

        self.history = HistoricalCases(
            config.data_dir.parent / "cases.sqlite3", config.data_dir.parent / "artifacts"
        )
        self.artifacts = ArtifactStore(config.data_dir / "artifacts")
        self.store = CaseStore(
            config.data_dir / "cases.sqlite3", artifacts=self.artifacts, clock=clock
        )
        self.runtime = IncidentRuntime(
            self.store,
            EvidenceRecorder(self.artifacts),
            ExecutionRecorder(self.store, clock=clock),
            clock=clock,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        if self.scheduler_task is not None or self.children:
            raise RuntimeError("active host requires await shutdown()")
        if any(not task.done() for task in self.runtime.pending_workers):
            raise RuntimeError("worker cleanup is pending; store remains open for reconciliation")
        if self.runtime.running_task is not None and not self.runtime.running_task.done():
            raise RuntimeError("active incident host requires await host.shutdown() before close")
        if self.runtime.owner is not None:
            try:
                self.runtime.shutdown(self.runtime.owner.case_id)
            finally:
                self.runtime.release_owner()
        self.store.close()
        self.history.close()
        self.closed = True

    async def shutdown(self) -> None:
        """Stop ownership and drain the owner task within its configured bound."""
        import asyncio

        from tau_incident.store.control import policy

        if self.closed:
            return
        self.stopping = True
        scheduler_error: BaseException | None = None
        if self.scheduler_task is not None:
            try:
                await self.scheduler_task
            except BaseException as exc:
                scheduler_error = exc
            finally:
                self.scheduler_task = None
        if self.mode == "embedded":
            self.store._connection.execute(
                "UPDATE incident_actions SET status='paused' WHERE status='queued' AND runner_id=?",
                (self.instance_id,),
            )
        for child in tuple(self.children.values()):
            child.frontend_detached_for_shutdown()
        if self.jobs:
            child_done, child_pending = await asyncio.wait(self.jobs, timeout=65)
            if child_pending:
                raise RuntimeError("child cleanup deadline reached; store remains open")
            for job in child_done:
                job.result()
            self.jobs.clear()
        running = self.runtime.running_task
        lease = self.runtime.owner
        timeout = 5.0
        if lease is not None:
            timeout = policy(self.store, lease.case_id).shutdown_seconds
            receipt = self.runtime.shutdown(lease.case_id)
            if receipt.status != "accepted":
                raise RuntimeError(receipt.reason)
        if running is not None and running is not asyncio.current_task():
            _, pending = await asyncio.wait({running}, timeout=timeout + 1)
            if pending:
                raise RuntimeError(
                    "shutdown deadline reached; case is fenced and store remains open"
                )
        if self.runtime.pending_workers:
            _, pending_workers = await asyncio.wait(self.runtime.pending_workers, timeout=timeout)
            if pending_workers:
                raise RuntimeError("worker shutdown deadline reached; store remains open")
        self.close()
        if scheduler_error is not None:
            raise RuntimeError("incident scheduler failed during shutdown") from scheduler_error

    def frontend_detached_for_shutdown(self) -> None:
        self.stopping = True
        if self.runtime.owner is not None:
            self.runtime.shutdown(self.runtime.owner.case_id)

    def ensure_scheduler(self) -> None:
        from tau_coding.incident.api import scheduler

        if self.scheduler_task is None:
            self.scheduler_task = asyncio.create_task(scheduler(self))

    async def dispatch(self, action: IncidentAction) -> dict[str, Any]:
        from tau_coding.incident.api import dispatch

        if self.history.contains(action.case_id):
            if action.operation in {"bind", "report", "handoff"}:
                result = self.query(
                    IncidentQuery.model_validate(
                        {
                            "case_id": action.case_id,
                            "view": "case" if action.operation == "bind" else action.operation,
                        }
                    )
                )
                if action.operation == "bind":
                    return {"request_id": action.request_id, "case": result}
                return result
            raise ValueError("historical case is read-only; create a new v2 case")
        if action.operation not in {"run", "resume"}:
            return await dispatch(self, action)
        self.get_case(action.case_id)
        operation = self.runtime.execution.start(
            "dispatch", case_id=action.case_id, command_id=action.request_id
        )
        try:
            result = await dispatch(self, action)
            self.runtime.execution.finish(
                operation.operation_id,
                status="succeeded",
                result=str(result.get("status", "accepted")),
            )
            return result
        except BaseException as exc:
            self.runtime.execution.finish(
                operation.operation_id, status="failed", result="dispatch_failed", detail=str(exc)
            )
            raise

    def query(self, request: IncidentQuery) -> dict[str, Any]:
        from tau_coding.incident.api import query

        if self.history.contains(request.case_id):
            case = self.history.case(request.case_id)
            if (
                case["project_key"] != self.config.project_key
                or case["scope"]["environment"] != self.config.environment
            ):
                raise ValueError("case does not belong to the selected project/environment")
            return self.history.query(
                request.case_id,
                request.view,
                request.reference,
                request.after_cursor,
                request.limit,
            )
        return query(self, request)

    def frontend_detached(self) -> None:
        """Daemon frontend detach does not end the host-owned runtime."""
        if self.mode == "embedded":
            for child in self.children.values():
                child.frontend_detached()
        if self.mode == "embedded" and self.runtime.owner is not None:
            self.runtime.shutdown(self.runtime.owner.case_id)

    def case_id_for_command(self, command_id: str) -> str:
        """Stable creation identity permits a CLI retry after its first response was lost."""
        return uuid5(NAMESPACE_URL, f"tau-incident:{self.config.project_key}:{command_id}").hex

    def get_case(self, case_id: str) -> IncidentCase:
        if self.history.contains(case_id):
            raise ValueError("historical case is read-only; use incident.query for archive access")
        case = self.runtime.get_case(case_id)
        if (
            case.project_key != self.config.project_key
            or case.scope.environment != self.config.environment
        ):
            raise ValueError("case does not belong to the selected project/environment")
        return case

    def execute(
        self,
        command: Command,
        expected_versions: tuple[VersionRef, ...] = (),
        *,
        parent: ExecutionRecord | None = None,
    ) -> Receipt:
        if self.history.contains(command.case_id):
            raise ValueError("historical case is read-only; create a new v2 case")
        if parent is not None and (
            parent.case_id != command.case_id
            or self.store.execution(parent.operation_id).status != "running"
        ):
            raise ValueError("command parent must be an active operation in this case")
        child = self.children.get(command.case_id)
        if child is not None:
            return child.execute(command, expected_versions, parent=parent)
        if isinstance(command.payload, CreateCase):
            if (
                command.payload.project_key != self.config.project_key
                or command.payload.scope.environment != self.config.environment
            ):
                raise ValueError("new case must match the host project/environment")
        else:
            self.get_case(command.case_id)
        if isinstance(command.payload, CreateCase) or self.runtime.owner is not None:
            return self.runtime.execute(command, expected_versions, parent=parent)
        existing = self.store.matching_receipt(command, expected_versions)
        if existing is not None:
            return existing
        remote = self.owner_service(command.case_id)
        if remote is not None:
            import httpx

            with httpx.Client(trust_env=False, timeout=30, follow_redirects=False) as client:
                response = client.post(
                    remote.endpoint.rstrip("/") + "/actions",
                    headers={"Authorization": f"Bearer {remote.credential()}"},
                    json=IncidentAction(
                        request_id=command.command_id,
                        operation="command",
                        case_id=command.case_id,
                        command=command,
                        expected_versions=expected_versions,
                    ).model_dump(mode="json"),
                )
                response.raise_for_status()
                return Receipt.model_validate(response.json()["receipt"])
        self.runtime.acquire_owner(command.case_id)
        try:
            from tau_incident.recovery import recover

            owner = self.runtime.owner
            if owner is not None:
                recover(self.runtime, owner.generation, active_parent=parent)
            return self.runtime.execute(command, expected_versions, parent=parent)
        finally:
            self.runtime.release_owner()

    def owner_service(self, case_id: str) -> HostSettings | None:
        from tau_incident.store.control import owner

        lease = owner(self.store, case_id)
        if lease is None or lease.expires_at <= self.runtime.clock():
            return None
        if self.mode == "daemon":
            raise ValueError("case is owned by another runtime")
        descriptor = self.config.data_dir / "incident-service.json"
        if not descriptor.exists():
            raise ValueError("case has an active owner; connect to its service")
        remote = HostSettings.from_file(descriptor)
        if remote.environment != self.config.environment:
            raise ValueError("owner service environment differs")
        return remote

    def brief(self, case_id: str) -> CaseBrief:
        self.get_case(case_id)
        return self.runtime.brief(case_id)

    def report(self, case_id: str) -> ProgressReport:
        self.get_case(case_id)
        return self.runtime.report(case_id)

    def events(
        self, case_id: str, after_cursor: int = 0, *, limit: int = 100
    ) -> tuple[DomainEvent, ...]:
        self.get_case(case_id)
        return self.runtime.events(case_id, after_cursor, limit=limit)

    def executions(
        self,
        case_id: str,
        *,
        attempt_id: str | None = None,
        operation_kind: OperationKind | None = None,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[ExecutionRecord, ...]:
        # Failed creation has no Case projection but still has queryable operation records.
        with suppress(KeyError):
            self.get_case(case_id)
        return self.store.executions(
            case_id=case_id,
            attempt_id=attempt_id,
            operation_kind=operation_kind,
            after_cursor=after_cursor,
            limit=limit,
        )

    def receipt(self, case_id: str, command_id: str) -> Receipt | None:
        with suppress(KeyError):
            self.get_case(case_id)
        receipt = self.store.receipt(command_id)
        if receipt is not None and receipt.case_id != case_id:
            raise ValueError("command receipt belongs to a different case")
        return receipt

    def read_evidence(self, case_id: str, evidence_id: str) -> bytes:
        self.get_case(case_id)
        return self.store.read_evidence(case_id, evidence_id)
