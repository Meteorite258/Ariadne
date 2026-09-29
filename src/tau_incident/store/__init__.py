"""SQLite authority for cases, accepted events, command receipts and local execution records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from tau_agent.messages import AssistantMessage
from tau_agent.types import JSONValue
from tau_incident.events import (
    AddConstraint,
    AddExplanation,
    AddObservation,
    CaseCreated,
    Command,
    ConstraintAdded,
    CreateCase,
    DispatchTasks,
    DomainEvent,
    EventPayload,
    ExplanationAdded,
    ExtendBudget,
    FinishAttempt,
    Lifecycle,
    ObservationAdded,
    Receipt,
    RecordPlan,
    RecordRead,
    RecordWait,
    RetryTask,
    ReviseTask,
    SetTaskDisposition,
    SubmitFinding,
    UpdateTaskProgress,
    reduce_case,
)
from tau_incident.evidence import ArtifactStore
from tau_incident.models import (
    CandidateExplanation,
    ExecutionConstraint,
    ExecutionRecord,
    IncidentCase,
    Observation,
    OperationKind,
    ReadManifest,
    RequestSnapshot,
    VersionRef,
)

SCHEMA_VERSION = 6
LiteralReceiptStatus = Literal["accepted", "rejected", "conflict"]
_SCHEMA = (
    "CREATE TABLE cases (case_id TEXT PRIMARY KEY, version INTEGER NOT NULL CHECK(version > 0), "
    "body TEXT NOT NULL)",
    "CREATE TABLE executions (operation_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
    "attempt_id TEXT, operation_kind TEXT NOT NULL, command_id TEXT NOT NULL, "
    "cursor INTEGER NOT NULL UNIQUE, body TEXT NOT NULL)",
    "CREATE TABLE execution_changes (cursor INTEGER PRIMARY KEY AUTOINCREMENT, "
    "operation_id TEXT NOT NULL)",
    "CREATE INDEX execution_case ON executions(case_id, cursor)",
    "CREATE INDEX execution_attempt ON executions(attempt_id, cursor)",
    "CREATE TABLE receipts (command_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
    "case_id TEXT NOT NULL, source_operation_id TEXT NOT NULL REFERENCES executions(operation_id), "
    "body TEXT NOT NULL)",
    "CREATE TABLE events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, "
    "case_id TEXT NOT NULL REFERENCES cases(case_id), case_version INTEGER NOT NULL, "
    "command_id TEXT NOT NULL REFERENCES receipts(command_id) DEFERRABLE INITIALLY DEFERRED, "
    "body TEXT NOT NULL, UNIQUE(case_id, case_version))",
    "CREATE TABLE artifacts (artifact_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
    "size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0))",
    "CREATE TABLE evidence (evidence_id TEXT PRIMARY KEY, "
    "case_id TEXT NOT NULL REFERENCES cases(case_id), "
    "artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id), "
    "operation_id TEXT NOT NULL REFERENCES executions(operation_id), body TEXT NOT NULL)",
    "CREATE TABLE object_versions (case_id TEXT NOT NULL REFERENCES cases(case_id), "
    "kind TEXT NOT NULL, object_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0), "
    "PRIMARY KEY(case_id, kind, object_id))",
)


class IdempotencyConflict(ValueError):
    """A stable command ID was reused for different caller intent."""


class CommitUnknown(RuntimeError):
    """The connection could not confirm COMMIT; lookup the stable command ID before retrying."""


class CaseStore:
    def __init__(
        self, path: Path, *, artifacts: ArtifactStore, clock: Callable[[], datetime]
    ) -> None:
        self.path = path
        self.artifacts = artifacts
        self.clock = clock
        if path.is_file() and path.stat().st_size:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as archive_check:
                version = archive_check.execute("PRAGMA user_version").fetchone()[0]
                if version != SCHEMA_VERSION:
                    raise ValueError(
                        "historical case store is read-only; use HistoricalCases and a "
                        "new v2 database"
                    )
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def _migrate(self) -> None:
        with self._transaction():
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    self._connection.execute(statement)
                self._connection.execute("PRAGMA user_version = 1")
                version = 1
            if version == 1:
                self._connection.execute(
                    "CREATE TABLE requests (request_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, "
                    "body TEXT NOT NULL)"
                )
                self._connection.execute(
                    "CREATE TABLE request_usage (request_id TEXT PRIMARY KEY, "
                    "case_id TEXT NOT NULL, "
                    "attempt_id TEXT, reserved INTEGER NOT NULL, charged INTEGER NOT NULL, "
                    "state TEXT NOT NULL, usage TEXT)"
                )
                self._connection.execute("PRAGMA user_version = 2")
                version = 2
            if version == 2:
                from tau_incident.store.control import migrate

                migrate(self)
                version = 3
            if version == 3:
                from tau_incident.quality import save_versions

                for row in self._connection.execute(
                    "SELECT case_id, version FROM cases"
                ).fetchall():
                    state = self.case_at_version(row["case_id"], row["version"])
                    self._connection.execute(
                        "UPDATE cases SET body=? WHERE case_id=?",
                        (state.model_dump_json(), state.case_id),
                    )
                    save_versions(self, state)
                self._connection.execute("PRAGMA user_version = 4")
                version = 4
            if version == 4:
                from tau_incident.store.intake import migrate as migrate_intake

                migrate_intake(self)
                version = 5
            if version == 5:
                self._connection.execute("PRAGMA user_version = 6")
                version = 6
            if version != SCHEMA_VERSION:
                raise ValueError(f"unsupported incident database schema {version}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        try:
            self._connection.commit()
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise CommitUnknown("SQLite commit outcome unknown; query the command receipt") from exc

    def close(self) -> None:
        self._connection.close()

    def get_case(self, case_id: str) -> IncidentCase:
        row = self._connection.execute(
            "SELECT body FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown case: {case_id}")
        return IncidentCase.model_validate_json(row["body"])

    def rebuild_case_projection(self, case_id: str) -> IncidentCase:
        """Rebuild domain query caches. Request/usage, execution and ownership ledgers survive."""
        from tau_incident.context import ContextBuilder
        from tau_incident.quality import save_versions

        with self._transaction():
            rows = self._connection.execute(
                "SELECT body FROM events WHERE case_id=? ORDER BY case_version", (case_id,)
            ).fetchall()
            case = None
            for row in rows:
                case = reduce_case(case, DomainEvent.model_validate_json(row[0]))
            if case is None:
                raise KeyError("unknown case")
            self._connection.execute(
                "UPDATE cases SET version=?,body=? WHERE case_id=?",
                (case.version, case.model_dump_json(), case_id),
            )
            self._connection.execute(
                "DELETE FROM object_versions WHERE case_id=? AND kind!='request'", (case_id,)
            )
            self._save_version(case_id, "case", case_id, case.version)
            for reference in ContextBuilder._refs(case):
                self._save_version(case_id, reference.kind, reference.object_id, reference.version)
            for attempt in case.attempts:
                self._save_version(case_id, "attempt", attempt.attempt_id, 1)
            for decision in case.decisions:
                self._save_version(case_id, "decision", decision.decision_id, decision.version)
            save_versions(self, case)
            for evidence in case.observations:
                self._connection.execute(
                    "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?)",
                    (
                        evidence.evidence_id,
                        case_id,
                        evidence.artifact.artifact_id,
                        evidence.source_operation_id,
                        evidence.model_dump_json(),
                    ),
                )
            return case

    def receipt(self, command_id: str) -> Receipt | None:
        row = self._connection.execute(
            "SELECT body FROM receipts WHERE command_id = ?", (command_id,)
        ).fetchone()
        return None if row is None else Receipt.model_validate_json(row["body"])

    def case_at_version(self, case_id: str, version: int) -> IncidentCase:
        """Rebuild fixed dispatch input, never replace worker premises with the latest case."""
        state = None
        rows = self._connection.execute(
            "SELECT body FROM events WHERE case_id=? AND case_version<=? ORDER BY case_version",
            (case_id, version),
        ).fetchall()
        for row in rows:
            state = reduce_case(state, DomainEvent.model_validate_json(row["body"]))
        if state is None or state.version != version:
            raise ValueError("dispatch case revision is unavailable")
        return state

    def matching_receipt(
        self, command: Command, expected_versions: tuple[VersionRef, ...]
    ) -> Receipt | None:
        receipt = self.receipt(command.command_id)
        if receipt is not None and receipt.content_hash != command.content_hash(expected_versions):
            raise IdempotencyConflict(f"command ID has different content: {command.command_id}")
        return receipt

    def events(
        self, case_id: str, after_cursor: int = 0, *, limit: int = 100
    ) -> tuple[DomainEvent, ...]:
        self._validate_page(after_cursor, limit)
        rows = self._connection.execute(
            "SELECT body FROM events WHERE case_id = ? AND cursor > ? ORDER BY cursor LIMIT ?",
            (case_id, after_cursor, limit),
        ).fetchall()
        return tuple(DomainEvent.model_validate_json(row["body"]) for row in rows)

    @staticmethod
    def _validate_page(after_cursor: int, limit: int) -> None:
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("cursor must be nonnegative and limit must be between 1 and 1000")

    def evidence(self, case_id: str, evidence_id: str) -> Observation:
        row = self._connection.execute(
            "SELECT body FROM evidence WHERE case_id = ? AND evidence_id = ?",
            (case_id, evidence_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"evidence is not registered in case {case_id}: {evidence_id}")
        return Observation.model_validate_json(row["body"])

    def read_evidence(self, case_id: str, evidence_id: str) -> bytes:
        return self.artifacts.read(self.evidence(case_id, evidence_id).artifact)

    def _version_error(self, case_id: str, refs: tuple[VersionRef, ...]) -> str | None:
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.kind, ref.object_id)
            if ref.case_id != case_id or key in seen:
                return "version references must be unique and belong to this case"
            seen.add(key)
            row = self._connection.execute(
                "SELECT version FROM object_versions "
                "WHERE case_id = ? AND kind = ? AND object_id = ?",
                (case_id, ref.kind, ref.object_id),
            ).fetchone()
            if row is None or row["version"] != ref.version:
                return f"stale or missing {ref.kind} revision: {ref.object_id}@{ref.version}"
        return None

    def _require_operation(self, operation_id: str, command: Command, kind: OperationKind) -> None:
        record = self.execution(operation_id)
        if (
            record.case_id != command.case_id
            or record.command_id != command.command_id
            or record.operation_kind != kind
            or record.status != "running"
        ):
            raise ValueError("command source must be a matching, running runtime operation")

    def commit(
        self,
        command: Command,
        expected_versions: tuple[VersionRef, ...] = (),
        *,
        source_operation_id: str,
        observation: Observation | None = None,
    ) -> Receipt:
        """No network/model calls or artifact writes inside this short transaction.

        A receipt binds both accepted and rejected intent. A changed precondition requires
        a new command ID. Empty preconditions serialize append-only manual input against
        the current aggregate; explicit references request optimistic version checks.
        """
        existing = self.matching_receipt(command, expected_versions)
        if existing is not None:
            return existing
        if observation is not None:
            # Verification happens before BEGIN; this store never deletes published artifacts.
            self.artifacts.read(observation.artifact)
        now = self.clock()
        with self._transaction():
            existing = self.matching_receipt(command, expected_versions)
            if existing is not None:
                return existing
            self._require_operation(source_operation_id, command, "submission")
            try:
                current = self.get_case(command.case_id)
            except KeyError:
                current = None
            error = self._version_error(command.case_id, expected_versions)
            status: LiteralReceiptStatus = "conflict" if error else "accepted"
            payload = command.payload
            if not error:
                error = self._validate_input(command, current, observation)
                if error:
                    status = "rejected"
            event_ids: tuple[str, ...] = ()
            version = current.version if current else None
            checked: list[VersionRef] = []
            if current is not None:
                checked.append(
                    VersionRef(
                        case_id=current.case_id,
                        kind="case",
                        object_id=current.case_id,
                        version=current.version,
                    )
                )
            requested = expected_versions
            if isinstance(payload, AddObservation):
                requested += payload.observation.inputs
            elif isinstance(payload, RecordPlan):
                requested += (*payload.decision.basis, *payload.read_basis)
                if payload.attempt is not None:
                    requested += payload.attempt.basis
            elif isinstance(payload, RecordRead):
                requested += (payload.reference,)
            elif isinstance(payload, SubmitFinding):
                finding = payload.finding
                requested += (*finding.observations, *finding.basis, *finding.counterevidence)
                for claim in finding.proposed_claims:
                    requested += (*claim.support, *claim.opposition, *claim.premises)
                if current is not None and error is None:
                    materialized = self._finding_manifest(payload, current)
                    if materialized.manifest is not None:
                        manifest = materialized.manifest
                        requested += (*manifest.dispatch, *manifest.dynamic, *manifest.changed)
                        for rid in manifest.requests:
                            requested += self.request(command.case_id, rid).selection
            for ref in requested:
                if ref.case_id != command.case_id:
                    continue
                row = self._connection.execute(
                    "SELECT version FROM object_versions "
                    "WHERE case_id = ? AND kind = ? AND object_id = ?",
                    (command.case_id, ref.kind, ref.object_id),
                ).fetchone()
                if row is not None:
                    actual_ref = ref.model_copy(update={"version": row["version"]})
                    if actual_ref not in checked:
                        checked.append(actual_ref)
            if error is None:
                event_payload = self._event_payload(command, observation, now)
                if (
                    isinstance(event_payload, (RecordPlan, RecordWait))
                    and event_payload.wait is not None
                    and event_payload.wait.operation_id is None
                ):
                    event_payload = event_payload.model_copy(
                        update={
                            "wait": event_payload.wait.model_copy(
                                update={"operation_id": source_operation_id}
                            )
                        }
                    )
                if isinstance(event_payload, SubmitFinding) and current is not None:
                    event_payload = self._finding_manifest(event_payload, current)
                event = DomainEvent(
                    event_id=uuid4().hex,
                    case_id=command.case_id,
                    case_version=1 if current is None else current.version + 1,
                    command_id=command.command_id,
                    source_operation_id=source_operation_id,
                    occurred_at=now,
                    payload=event_payload,
                )
                updated = reduce_case(current, event)
                self._connection.execute(
                    "INSERT INTO cases(case_id, version, body) VALUES (?, ?, ?) "
                    "ON CONFLICT(case_id) DO UPDATE SET "
                    "version=excluded.version, body=excluded.body",
                    (updated.case_id, updated.version, updated.model_dump_json()),
                )
                self._save_version(updated.case_id, "case", updated.case_id, updated.version)
                from tau_incident.quality import save_versions

                save_versions(self, updated)
                if current is not None:
                    from tau_incident.store.control import apply

                    apply(self, command, current, updated)
                if isinstance(event_payload, RecordPlan):
                    self._save_version(
                        command.case_id, "decision", event_payload.decision.decision_id, 1
                    )
                    if event_payload.task is not None and event_payload.attempt is not None:
                        self._save_version(command.case_id, "task", event_payload.task.task_id, 1)
                        self._save_version(
                            command.case_id, "attempt", event_payload.attempt.attempt_id, 1
                        )
                elif isinstance(event_payload, SubmitFinding):
                    self._save_version(
                        command.case_id, "finding", event_payload.finding.finding_id, 1
                    )
                    for claim in event_payload.finding.proposed_claims:
                        self._save_version(command.case_id, "claim", claim.claim_id, claim.version)
                    for review in event_payload.reviews:
                        self._save_version(
                            command.case_id, "review", review.review_id, review.version
                        )
                if observation is not None:
                    artifact = observation.artifact
                    self._connection.execute(
                        "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?)",
                        (artifact.artifact_id, artifact.sha256, artifact.size_bytes),
                    )
                    self._connection.execute(
                        "INSERT INTO evidence VALUES (?, ?, ?, ?, ?)",
                        (
                            observation.evidence_id,
                            command.case_id,
                            artifact.artifact_id,
                            observation.source_operation_id,
                            observation.model_dump_json(),
                        ),
                    )
                    self._save_version(command.case_id, "evidence", observation.evidence_id, 1)
                if isinstance(event_payload, ExplanationAdded):
                    self._save_version(
                        command.case_id, "input", event_payload.explanation.input_id, 1
                    )
                elif isinstance(event_payload, ConstraintAdded):
                    self._save_version(
                        command.case_id, "input", event_payload.constraint.input_id, 1
                    )
                cursor = self._connection.execute(
                    "INSERT INTO events(event_id, case_id, case_version, command_id, body) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.case_id,
                        event.case_version,
                        event.command_id,
                        event.model_dump_json(),
                    ),
                ).lastrowid
                event = event.model_copy(update={"cursor": cursor})
                self._connection.execute(
                    "UPDATE events SET body = ? WHERE event_id = ?",
                    (event.model_dump_json(), event.event_id),
                )
                event_ids = (event.event_id,)
                version = updated.version
            receipt = Receipt(
                receipt_id=uuid4().hex,
                command_id=command.command_id,
                content_hash=command.content_hash(expected_versions),
                case_id=command.case_id,
                status=status,
                reason=error,
                expected_versions=expected_versions,
                checked_versions=tuple(checked),
                case_version=version,
                event_ids=event_ids,
                source_operation_id=source_operation_id,
                committed_at=now,
            )
            self._connection.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                (
                    command.command_id,
                    receipt.content_hash,
                    command.case_id,
                    source_operation_id,
                    receipt.model_dump_json(),
                ),
            )
        return receipt

    def _validate_input(
        self,
        command: Command,
        current: IncidentCase | None,
        observation: Observation | None,
    ) -> str | None:
        payload = command.payload
        if isinstance(payload, CreateCase):
            if current is not None:
                return "case already exists"
            if observation is not None:
                return "case creation cannot attach prepared evidence"
            return None
        if current is None:
            return "case does not exist"
        from tau_incident.quality import QUALITY_COMMANDS, validate_quality

        if isinstance(payload, QUALITY_COMMANDS):
            return validate_quality(self, command, current)
        from tau_incident.store.control import validate

        error = validate(self, command, current)
        if error:
            return error
        if isinstance(
            payload,
            (
                DispatchTasks,
                Lifecycle,
                RecordWait,
                ExtendBudget,
                ReviseTask,
                RetryTask,
                UpdateTaskProgress,
                SetTaskDisposition,
            ),
        ):
            return None
        if isinstance(payload, RecordRead):
            from tau_incident.context import relevant

            attempt = next(
                (a for a in current.attempts if a.attempt_id == payload.attempt_id), None
            )
            if attempt is None or attempt.status != "running":
                return "read requires a running attempt and evidence reference"
            from tau_incident.context_read import lookup

            try:
                value = lookup(current, payload.reference)
            except ValueError as exc:
                return str(exc)
            from tau_incident.models import Scope

            scope = Scope.model_validate(value.model_dump()["scope"])
            if payload.reference not in attempt.basis and not relevant(scope, attempt.scope):
                return "domain read outside task scope"
            return self._version_error(command.case_id, (payload.reference,))
        if isinstance(payload, (RecordPlan, SubmitFinding, FinishAttempt)):
            from tau_incident.submission import validate_submission

            error = validate_submission(current, payload)
            if error:
                return error
            references: tuple[VersionRef, ...] = ()
            if isinstance(payload, RecordPlan):
                references = (*payload.decision.basis, *payload.read_basis)
                if payload.attempt is not None:
                    references += payload.attempt.basis
            elif isinstance(payload, SubmitFinding):
                finding = payload.finding
                from tau_incident.progress import validate_work_facts

                attempt = next(a for a in current.attempts if a.attempt_id == finding.attempt_id)
                error = validate_work_facts(self, current, attempt, finding)
                if error:
                    return error
                pending = self._connection.execute(
                    "SELECT COUNT(*) FROM request_usage "
                    "WHERE case_id=? AND attempt_id=? AND state='reserved'",
                    (command.case_id, finding.attempt_id),
                ).fetchone()[0]
                if pending:
                    return "attempt has unsettled request reservations"
                references = (*finding.observations, *finding.basis, *finding.counterevidence)
                for claim in finding.proposed_claims:
                    references += (*claim.support, *claim.opposition, *claim.premises)
            if isinstance(payload, SubmitFinding):
                # Known historical references remain auditable; relevant changes mark review.
                return None
            return self._version_error(command.case_id, tuple(dict.fromkeys(references)))
        scope = payload.observation.scope if isinstance(payload, AddObservation) else payload.scope
        if scope.environment != current.scope.environment:
            return "input environment differs from the case environment"
        if isinstance(payload, AddObservation):
            if observation is None or observation.case_id != command.case_id:
                return "observation requires runtime evidence provenance"
            if observation.version != 1 or observation.attempt_id != payload.attempt_id:
                return "evidence version or attempt mismatch"
            if payload.attempt_id is not None:
                from tau_incident.submission import scope_contains

                attempt = next(
                    (a for a in current.attempts if a.attempt_id == payload.attempt_id), None
                )
                if attempt is None or not scope_contains(attempt.scope, scope):
                    return "evidence requires an originating attempt and authorized scope"
                source_id = observation.source.reference
                if observation.source.kind != "tool" or source_id is None:
                    return "attempt observations require an actual tool operation"
                source = self.execution(source_id)
                if (
                    source.operation_kind != "tool"
                    or source.attempt_id != attempt.attempt_id
                    or source.runtime_generation != attempt.runtime_generation
                    or source.case_id != command.case_id
                ):
                    return "evidence tool provenance does not match the originating attempt"
            self._require_operation(
                observation.source_operation_id, command, "evidence_registration"
            )
            expected = payload.observation.model_dump(exclude={"raw_text", "collected_at"})
            actual = observation.model_dump(include=set(expected))
            if actual != expected or (
                payload.observation.collected_at is not None
                and observation.collected_at != payload.observation.collected_at
            ):
                return "prepared evidence does not match command input"
            if (
                observation.artifact.sha256
                != hashlib.sha256(payload.observation.raw_text.encode("utf-8")).hexdigest()
            ):
                return "artifact does not match command content"
            if (
                observation.actual_coverage is not None
                and observation.actual_coverage.environment != scope.environment
            ):
                return "actual coverage belongs to a different environment"
            return self._version_error(command.case_id, payload.observation.inputs)
        if observation is not None:
            return "only observation commands may register evidence"
        return None

    @staticmethod
    def _event_payload(
        command: Command, observation: Observation | None, now: datetime
    ) -> EventPayload:
        payload = command.payload
        from tau_incident.quality import QUALITY_COMMANDS

        if isinstance(payload, QUALITY_COMMANDS):
            return payload
        if isinstance(
            payload,
            (
                RecordPlan,
                SubmitFinding,
                UpdateTaskProgress,
                FinishAttempt,
                RecordRead,
                DispatchTasks,
                Lifecycle,
                RecordWait,
                ExtendBudget,
                ReviseTask,
                RetryTask,
                SetTaskDisposition,
            ),
        ):
            return payload
        if isinstance(payload, CreateCase):
            return CaseCreated(
                case=IncidentCase(
                    case_id=command.case_id,
                    project_key=payload.project_key,
                    scope=payload.scope,
                    symptoms=payload.symptoms,
                    impact=payload.impact,
                    source=payload.source,
                    created_at=now,
                    updated_at=now,
                )
            )
        if isinstance(payload, AddObservation):
            if observation is None:
                raise ValueError("missing prepared observation")
            return ObservationAdded(observation=observation)
        if isinstance(payload, AddExplanation):
            return ExplanationAdded(
                explanation=CandidateExplanation(
                    input_id=uuid4().hex,
                    case_id=command.case_id,
                    text=payload.text,
                    scope=payload.scope,
                    source=payload.source,
                    created_at=now,
                )
            )
        if isinstance(payload, AddConstraint):
            return ConstraintAdded(
                constraint=ExecutionConstraint(
                    input_id=uuid4().hex,
                    case_id=command.case_id,
                    text=payload.text,
                    scope=payload.scope,
                    source=payload.source,
                    created_at=now,
                )
            )
        raise ValueError("unsupported command")

    def _save_version(self, case_id: str, kind: str, object_id: str, version: int) -> None:
        self._connection.execute(
            "INSERT INTO object_versions VALUES (?, ?, ?, ?) "
            "ON CONFLICT(case_id, kind, object_id) DO UPDATE SET version=excluded.version",
            (case_id, kind, object_id, version),
        )

    def save_request(self, snapshot: RequestSnapshot) -> None:
        self.artifacts.read(snapshot.provider_input)
        with self._transaction():
            from tau_incident.store.control import owner, require_owner

            lease = owner(self, snapshot.case_id)
            require_owner(
                self,
                snapshot.case_id,
                lease.owner_id if lease else None,
                snapshot.runtime_generation,
            )
            case = self.get_case(snapshot.case_id)
            for reference in snapshot.memory_selection:
                source = self.get_case(reference.case_id)
                if (
                    reference.kind != "memory"
                    or source.project_key != case.project_key
                    or not source.memories
                    or not source.reports
                ):
                    raise ValueError("historical selection is outside project or unavailable")
                card, report = source.memories[-1], source.reports[-1]
                if (
                    card.memory_id != reference.object_id
                    or card.version != reference.version
                    or card.state != "current"
                    or report.state != "current"
                    or card.source_report.version != report.version
                ):
                    raise ValueError("historical source changed during context preparation")
            if case.investigation_status in {"paused", "completed"}:
                raise ValueError("case stopped before request snapshot")
            if snapshot.attempt_id is not None and not any(
                a.attempt_id == snapshot.attempt_id
                and a.status == "running"
                and a.runtime_generation == snapshot.runtime_generation
                for a in case.attempts
            ):
                raise ValueError("attempt stopped before request snapshot")
            artifact = snapshot.provider_input
            self._connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?)",
                (artifact.artifact_id, artifact.sha256, artifact.size_bytes),
            )
            self._save_version(snapshot.case_id, "request", snapshot.request_id, snapshot.version)
            self._connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?)",
                (snapshot.request_id, snapshot.case_id, snapshot.model_dump_json()),
            )

    def request(self, case_id: str, request_id: str) -> RequestSnapshot:
        row = self._connection.execute(
            "SELECT body FROM requests WHERE case_id=? AND request_id=?", (case_id, request_id)
        ).fetchone()
        if row is None:
            raise KeyError("unknown request")
        return RequestSnapshot.model_validate_json(row["body"])

    def usage_totals(
        self, case_id: str, attempt_id: str | None = None, *, task_id: str | None = None
    ) -> tuple[int, int, int]:
        query = (
            "SELECT COUNT(*), COALESCE(SUM(charged),0), COALESCE(SUM(state='unknown'),0) "
            "FROM request_usage WHERE case_id=? AND state!='not_sent'"
        )
        parameters = [case_id]
        if attempt_id is not None:
            query += " AND attempt_id=?"
            parameters.append(attempt_id)
        if task_id is not None:
            case = self.get_case(case_id)
            if not any(task.task_id == task_id for task in case.tasks):
                raise KeyError("unknown task")
            attempts = [a.attempt_id for a in case.attempts if a.task_id == task_id]
            if not attempts:
                return 0, 0, 0
            query += " AND attempt_id IN (" + ",".join("?" for _ in attempts) + ")"
            parameters.extend(attempts)
        row = self._connection.execute(query, parameters).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def request_usage(self, case_id: str, request_id: str) -> dict[str, JSONValue]:
        row = self._connection.execute(
            "SELECT * FROM request_usage WHERE case_id=? AND request_id=?", (case_id, request_id)
        ).fetchone()
        if row is None:
            raise KeyError("request has no budget reservation")
        return {
            "request_id": request_id,
            "attempt_id": row["attempt_id"],
            "reserved_tokens": row["reserved"],
            "charged_tokens": row["charged"],
            "state": row["state"],
            "usage": json.loads(row["usage"]) if row["usage"] else None,
        }

    def budget_summary(self, case_id: str) -> dict[str, JSONValue]:
        from tau_incident.store.control import held, policy

        row = self._connection.execute(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CASE WHEN state='settled' THEN charged ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN state='reserved' THEN charged ELSE 0 END),0), "
            "COALESCE(SUM(CASE WHEN state='unknown' THEN charged ELSE 0 END),0), "
            "COALESCE(SUM(state='unknown'),0) FROM request_usage "
            "WHERE case_id=? AND state!='not_sent'",
            (case_id,),
        ).fetchone()
        hc, ht = held(self, case_id)
        summary: dict[str, JSONValue] = {
            "calls": int(row[0]),
            "settled_tokens": int(row[1]),
            "reserved_tokens": int(row[2]),
            "unknown_usage_reserved_tokens": int(row[3]),
            "unknown_usage_calls": int(row[4]),
            "attempt_reserved_calls": hc,
            "attempt_reserved_tokens": ht,
        }
        try:
            limits = policy(self, case_id)
        except ValueError:
            summary["limits"] = None
        else:
            summary.update(
                limits=limits.model_dump(mode="json"),
                available_tokens=(
                    max(0, limits.token_limit - sum(int(row[i]) for i in (1, 2, 3)))
                    if limits.token_limit is not None
                    else None
                ),
            )
        from tau_agent.messages import Usage

        usage_rows = self._connection.execute(
            "SELECT usage FROM request_usage WHERE case_id=? AND state!='not_sent'", (case_id,)
        ).fetchall()
        costs = [Usage.model_validate_json(r[0]).cost.total if r[0] else 0 for r in usage_rows]
        summary["reported_cost_usd"] = sum(cost for cost in costs if cost > 0)
        summary["cost_unknown_calls"] = sum(cost <= 0 for cost in costs)
        summary["cost_note"] = (
            "Missing or default zero cost is unknown; tokens include conservative estimates."
        )
        return summary

    def reserve_request(
        self,
        *,
        request_id: str,
        case_id: str,
        attempt_id: str | None,
        tokens: int,
        checkpoint_start_calls: int | None = None,
        checkpoint_steps: int | None = None,
        owner_id: str | None = None,
        owner_generation: int | None = None,
        role_operation_id: str | None = None,
    ) -> None:
        from tau_incident.budget import BudgetExceeded, StepCheckpoint

        if tokens <= 0:
            raise ValueError("request reservation must have a positive estimate")
        with self._transaction():
            from tau_incident.store.control import policy, require_owner

            require_owner(self, case_id, owner_id, owner_generation)
            if self.get_case(case_id).investigation_status in {"paused", "completed"}:
                raise ValueError("case does not allow model calls")
            if (
                attempt_id is None
                and self._connection.execute(
                    "SELECT 1 FROM role_slots WHERE operation_id=? AND case_id=? AND generation=?",
                    (role_operation_id, case_id, owner_generation),
                ).fetchone()
                is None
            ):
                raise ValueError("model role has no concurrency reservation")
            limits = policy(self, case_id)
            if limits.deadline and self.clock() >= limits.deadline:
                raise BudgetExceeded("case deadline reached")
            token_limit = limits.token_limit
            calls, used, _ = self.usage_totals(case_id)
            if checkpoint_steps is not None:
                if checkpoint_start_calls is None or checkpoint_start_calls > calls:
                    raise ValueError("invalid model-step checkpoint baseline")
                if calls - checkpoint_start_calls >= checkpoint_steps:
                    raise StepCheckpoint("optional model-step checkpoint reached")
            if token_limit is not None and used + tokens > token_limit:
                raise BudgetExceeded("case request budget exhausted")
            if attempt_id is not None:
                case = self.get_case(case_id)
                attempt = next(a for a in case.attempts if a.attempt_id == attempt_id)
                task = next(t for t in case.tasks if t.task_id == attempt.task_id)
                if (
                    attempt.status != "running"
                    or attempt.runtime_generation != owner_generation
                    or task.active_attempt_id != attempt_id
                    or task.contract_version != attempt.contract_version
                ):
                    raise ValueError("request from inactive attempt")
                self._connection.execute(
                    "UPDATE attempt_reservations SET calls=0,tokens=0 "
                    "WHERE attempt_id=? AND state='reserved'",
                    (attempt_id,),
                )
            self._connection.execute(
                "INSERT INTO request_usage VALUES (?, ?, ?, ?, ?, 'reserved', NULL, ?)",
                (request_id, case_id, attempt_id, tokens, tokens, owner_generation),
            )

    def settle_request(self, request_id: str, tokens: int | None, usage: str | None) -> None:
        if tokens is not None and (tokens <= 0 or usage is None):
            raise ValueError("known usage requires a positive provider usage record")
        if tokens is not None:
            from tau_agent.messages import Usage

            if Usage.model_validate_json(usage or "{}").total_tokens != tokens:
                raise ValueError("usage total does not match settlement")
        with self._transaction():
            self._connection.execute(
                "UPDATE request_usage SET charged=COALESCE(?,reserved), state=?, usage=? "
                "WHERE request_id=? AND (state='reserved' OR (state='unknown' AND ? IS NOT NULL))",
                (tokens, "unknown" if tokens is None else "settled", usage, request_id, tokens),
            )

    def abandon_request(self, request_id: str) -> None:
        """Only the pre-call boundary can prove a reserved request was never sent."""
        with self._transaction():
            self._connection.execute(
                "UPDATE request_usage SET charged=0,state='not_sent' "
                "WHERE request_id=? AND state IN ('reserved','unknown')",
                (request_id,),
            )

    def save_response(self, request_id: str, message: AssistantMessage, is_error: bool) -> str:
        artifact = self.artifacts.put(
            message.model_dump_json().encode("utf-8"), media_type="application/json"
        )
        result = json.dumps(
            {
                "artifact": artifact.model_dump(mode="json"),
                "stop_reason": message.stop_reason,
                "is_error": is_error,
                "recorded_at": self.clock().isoformat(),
            }
        )
        with self._transaction():
            row = self._connection.execute(
                "SELECT body FROM request_results WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                if json.loads(row[0])["artifact"]["artifact_id"] != artifact.artifact_id:
                    raise ValueError("request already has a different response")
                return artifact.artifact_id
            if (
                self._connection.execute(
                    "SELECT 1 FROM request_usage WHERE request_id=?", (request_id,)
                ).fetchone()
                is None
            ):
                raise ValueError("response requires a reserved request")
            self._connection.execute(
                "INSERT INTO request_results VALUES (?,?)", (request_id, result)
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?)",
                (artifact.artifact_id, artifact.sha256, artifact.size_bytes),
            )
            known = message.usage.total_tokens > 0
            self._connection.execute(
                "UPDATE request_usage SET charged=COALESCE(?,reserved),state=?,usage=? "
                "WHERE request_id=? AND state IN ('reserved','unknown')",
                (
                    message.usage.total_tokens if known else None,
                    "settled" if known else "unknown",
                    message.usage.model_dump_json(),
                    request_id,
                ),
            )
        return artifact.artifact_id

    def request_result(self, request_id: str) -> dict[str, JSONValue] | None:
        row = self._connection.execute(
            "SELECT body FROM request_results WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        result: dict[str, JSONValue] = json.loads(row[0])
        return result

    def task_usage(self, case_id: str, task_id: str) -> tuple[int, int]:
        attempts = [a.attempt_id for a in self.get_case(case_id).attempts if a.task_id == task_id]
        totals = [self.usage_totals(case_id, aid) for aid in attempts]
        return sum(t[0] for t in totals), sum(t[1] for t in totals)

    def read_manifest(self, case_id: str, attempt_id: str) -> ReadManifest:
        """Current audit view also covers interrupted attempts without a final Finding."""
        case = self.get_case(case_id)
        attempt = next((a for a in case.attempts if a.attempt_id == attempt_id), None)
        if attempt is None:
            raise KeyError("unknown attempt")
        rows = self._connection.execute(
            "SELECT r.request_id FROM requests r "
            "JOIN request_usage u ON r.request_id=u.request_id "
            "WHERE u.attempt_id=? AND u.state!='not_sent' ORDER BY r.rowid",
            (attempt_id,),
        ).fetchall()
        return attempt.manifest.model_copy(
            update={
                "dispatch": attempt.basis,
                "dynamic": attempt.reads,
                "requests": tuple(r[0] for r in rows),
            }
        )

    def _finding_manifest(self, payload: SubmitFinding, case: IncidentCase) -> SubmitFinding:
        from tau_incident.models import ReadManifest

        attempt = next(a for a in case.attempts if a.attempt_id == payload.finding.attempt_id)
        rows = self._connection.execute(
            "SELECT r.request_id,r.body FROM requests r "
            "JOIN request_usage u ON r.request_id=u.request_id "
            "WHERE u.attempt_id=? AND u.state!='not_sent' ORDER BY r.rowid",
            (attempt.attempt_id,),
        ).fetchall()
        refs = list(attempt.basis) + list(attempt.reads)
        for row in rows:
            refs.extend(RequestSnapshot.model_validate_json(row["body"]).selection)
        finding = payload.finding
        declared = (
            *finding.basis,
            *finding.observations,
            *finding.counterevidence,
            *(r for c in finding.proposed_claims for r in (*c.support, *c.opposition, *c.premises)),
        )
        refs.extend(declared)
        changed = tuple(r for r in dict.fromkeys(refs) if self._version_error(case.case_id, (r,)))
        # Native catalog/config revisions are source facts, separate from immutable evidence IDs.
        for ref in dict.fromkeys(refs):
            evidence = next(
                (
                    e
                    for e in case.observations
                    if ref.kind == "evidence" and e.evidence_id == ref.object_id
                ),
                None,
            )
            if evidence is None or evidence.source_revision is None:
                continue
            if any(
                other.source.actor == evidence.source.actor
                and other.scope == evidence.scope
                and other.source_revision is not None
                and other.source_revision != evidence.source_revision
                and (other.available_at or other.collected_at)
                >= (evidence.available_at or evidence.collected_at)
                for other in case.observations
            ):
                changed = tuple(dict.fromkeys((*changed, ref)))
        # Newly imposed execution constraints also invalidate the old interpretation context.
        known = {(r.kind, r.object_id) for r in refs}
        changed += tuple(
            VersionRef(case_id=case.case_id, kind="input", object_id=c.input_id, version=c.version)
            for c in case.constraints
            if ("input", c.input_id) not in known
        )
        from uuid import NAMESPACE_URL, uuid5

        from tau_incident.models import ReviewIssue, Source
        from tau_incident.store.control import policy

        reviews = tuple(
            r.model_copy(update={"max_repairs": policy(self, case.case_id).max_repair_rounds})
            for r in payload.reviews
        )
        # Adopting a diagnosis pulls its transitive working claims into review.
        from tau_incident.review import issue_for

        needed = {
            r.object_id
            for c in finding.proposed_claims
            if c.judgment == "diagnosis"
            for r in c.premises
        }
        seen: set[str] = set()
        dependency_reviews = []
        while needed - seen:
            identity = next(iter(needed - seen))
            seen.add(identity)
            claim = next((c for c in case.claims if c.claim_id == identity), None)
            if claim is None:
                continue
            needed.update(r.object_id for r in claim.premises)
            if claim.validity != "current" and not any(
                i.target.object_id == identity and i.target.version == claim.version
                for i in case.review_issues
            ):
                dependency_reviews.append(
                    issue_for(claim).model_copy(
                        update={"max_repairs": policy(self, case.case_id).max_repair_rounds}
                    )
                )
        reviews = (*dependency_reviews, *reviews)
        task = next(t for t in case.tasks if t.task_id == attempt.task_id)
        if changed and task.kind != "repair":
            reviews += (
                ReviewIssue(
                    review_id=uuid5(NAMESPACE_URL, "stale-basis:" + finding.finding_id).hex,
                    case_id=case.case_id,
                    version=1,
                    scope=attempt.scope,
                    source=Source(kind="runtime", actor="submission"),
                    target=VersionRef(
                        case_id=case.case_id,
                        kind="task",
                        object_id=attempt.task_id,
                        version=attempt.contract_version,
                    ),
                    gap=(
                        "Read premises changed: "
                        + ", ".join(f"{r.kind}:{r.object_id}@{r.version}" for r in changed)
                    )
                    if changed
                    else "Task incomplete: " + "; ".join(finding.gaps or (finding.completion,)),
                    impact="Finding retained for review; task completion is withheld.",
                    required_action="Re-evaluate current premises and completion conditions: "
                    + "; ".join(task.completion_conditions),
                    status="open",
                    problem="basis_changed" if changed else "repair",
                    max_repairs=policy(self, case.case_id).max_repair_rounds,
                ),
            )
        return payload.model_copy(
            update={
                "reviews": reviews,
                "manifest": ReadManifest(
                    dispatch=attempt.basis,
                    dynamic=attempt.reads,
                    requests=tuple(r["request_id"] for r in rows),
                    finding=tuple(dict.fromkeys(declared)),
                    changed=changed,
                ),
            }
        )

    def execution(self, operation_id: str) -> ExecutionRecord:
        row = self._connection.execute(
            "SELECT body FROM executions WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown operation: {operation_id}")
        return ExecutionRecord.model_validate_json(row["body"])

    def add_execution(self, record: ExecutionRecord) -> ExecutionRecord:
        with self._transaction():
            return self._write_execution(record, insert=True)

    def update_execution(
        self,
        operation_id: str,
        update: Callable[[ExecutionRecord], ExecutionRecord],
    ) -> ExecutionRecord:
        """Only execution metadata may be updated here; domain state uses commit()."""
        with self._transaction():
            original = self.execution(operation_id)
            record = update(original)
            if (record.operation_id, record.case_id, record.command_id) != (
                original.operation_id,
                original.case_id,
                original.command_id,
            ):
                raise ValueError("execution identity is immutable")
            return self._write_execution(record, insert=False)

    def _write_execution(self, record: ExecutionRecord, *, insert: bool) -> ExecutionRecord:
        cursor = self._connection.execute(
            "INSERT INTO execution_changes(operation_id) VALUES (?)", (record.operation_id,)
        ).lastrowid
        record = record.model_copy(update={"cursor": cursor})
        if insert:
            self._connection.execute(
                "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.operation_id,
                    record.case_id or "",
                    record.attempt_id,
                    record.operation_kind,
                    record.command_id,
                    cursor,
                    record.model_dump_json(),
                ),
            )
        else:
            self._connection.execute(
                "UPDATE executions SET cursor = ?, body = ? WHERE operation_id = ?",
                (cursor, record.model_dump_json(), record.operation_id),
            )
        return record

    def executions(
        self,
        *,
        case_id: str | None = None,
        attempt_id: str | None = None,
        operation_kind: OperationKind | None = None,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[ExecutionRecord, ...]:
        self._validate_page(after_cursor, limit)
        clauses = ["cursor > ?"]
        parameters: list[str | int] = [after_cursor]
        for field, value in (
            ("case_id", case_id),
            ("attempt_id", attempt_id),
            ("operation_kind", operation_kind),
        ):
            if value is not None:
                clauses.append(f"{field} = ?")
                parameters.append(value)
        parameters.append(limit)
        rows = self._connection.execute(
            "SELECT body FROM executions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY cursor LIMIT ?",
            parameters,
        ).fetchall()
        return tuple(ExecutionRecord.model_validate_json(row["body"]) for row in rows)
