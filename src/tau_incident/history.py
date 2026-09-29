"""Read-only v1 archive access. Never opens the mutable CaseStore or runs migrations.

Stored JSON is returned verbatim: new domain defaults and reducers must not change
the meaning of a historical record. Stop the old writer before archiving a store.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Self


class HistoricalCases:
    def __init__(self, path: Path, artifacts: Path) -> None:
        self.artifacts = artifacts.resolve()
        self.connection: sqlite3.Connection | None = None
        self.schema_version = 1
        self._snapshot: tempfile.TemporaryDirectory[str] | None = None
        if path.is_file():
            # SQLite may create/update WAL shared-memory files even on a read-only
            # connection. Read a private snapshot so archive bytes stay untouched.
            self._snapshot = tempfile.TemporaryDirectory(prefix="amadeus-v1-read-")
            copied = Path(self._snapshot.name) / path.name
            sources = [item for item in (path, Path(str(path) + "-wal")) if item.exists()]
            before = [(item.stat().st_size, item.stat().st_mtime_ns) for item in sources]
            for item in sources:
                shutil.copyfile(item, copied if item == path else Path(str(copied) + "-wal"))
            if before != [(item.stat().st_size, item.stat().st_mtime_ns) for item in sources]:
                self._snapshot.cleanup()
                raise ValueError(
                    "historical database has an active writer; stop the old daemon "
                    "before archive access"
                )
            self.connection = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA query_only=ON")
            if self.connection.execute("PRAGMA user_version").fetchone()[0] >= 6:
                self.schema_version = 2

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self._snapshot is not None:
            self._snapshot.cleanup()
            self._snapshot = None

    def contains(self, case_id: str) -> bool:
        return (
            self.connection is not None
            and self.connection.execute(
                "SELECT 1 FROM cases WHERE case_id=?", (case_id,)
            ).fetchone()
            is not None
        )

    def _one(self, sql: str, parameters: tuple[str, ...]) -> dict[str, Any]:
        row = self.connection.execute(sql, parameters).fetchone() if self.connection else None
        if row is None:
            raise KeyError("unknown historical record")
        result: dict[str, Any] = json.loads(row[0])
        return result

    def case(self, case_id: str) -> dict[str, Any]:
        return self._one("SELECT body FROM cases WHERE case_id=?", (case_id,))

    def events(self, case_id: str, after: int, limit: int) -> list[dict[str, Any]]:
        return self._page("events", case_id, after, limit)

    def _page(self, table: str, case_id: str, after: int, limit: int) -> list[dict[str, Any]]:
        if table not in {"events", "executions"} or after < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid historical page")
        self.case(case_id)
        assert self.connection is not None
        return [
            json.loads(row[0])
            for row in self.connection.execute(
                f"SELECT body FROM {table} WHERE case_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                (case_id, after, limit),
            )
        ]

    def read_artifact(self, reference: dict[str, Any]) -> bytes:
        identity = reference["artifact_id"]
        if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
            raise ValueError("invalid historical artifact ID")
        path = self.artifacts / identity
        if path.is_symlink() or path.resolve().parent != self.artifacts:
            raise ValueError("historical artifact escapes archive")
        data = path.read_bytes()
        if (
            hashlib.sha256(data).hexdigest() != reference["sha256"]
            or len(data) != reference["size_bytes"]
        ):
            raise ValueError("historical artifact hash/size mismatch")
        return data

    def query(
        self,
        case_id: str,
        view: str,
        reference: str | None = None,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        case = self.case(case_id)
        if view == "case":
            return {**case, "historical_readonly": True}
        if view in {"brief", "handoff"}:
            return {"case": case, "historical_readonly": True}
        if view == "report":
            return {"reports": case.get("reports", []), "historical_readonly": True}
        if view == "budget":
            assert self.connection is not None
            tables = {
                r[0]
                for r in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            row = (
                self.connection.execute(
                    "SELECT body FROM policies WHERE case_id=?", (case_id,)
                ).fetchone()
                if "policies" in tables
                else None
            )
            usage = (
                [
                    dict(item)
                    for item in self.connection.execute(
                        "SELECT * FROM request_usage WHERE case_id=?", (case_id,)
                    )
                ]
                if "request_usage" in tables
                else []
            )
            return {
                "historical_readonly": True,
                "limits": json.loads(row[0]) if row else None,
                "requests": usage,
            }
        if view == "receipt":
            return {
                "receipt": self._one(
                    "SELECT body FROM receipts WHERE case_id=? AND command_id=?",
                    (case_id, reference or ""),
                )
            }
        if view == "export":
            import base64

            return {
                "schema_version": self.schema_version,
                "historical_readonly": True,
                "files": {
                    name: base64.b64encode(data).decode("ascii")
                    for name, data in self.export(case_id).items()
                },
            }
        if view in {"events", "timeline"}:
            key = "events" if view == "events" else "executions"
            rows = self._page(key, case_id, after, limit)
            return {
                "type": "incident." + view,
                key: rows,
                "next_cursor": rows[-1]["cursor"] if rows else after,
            }
        if view == "evidence":
            evidence = self._one(
                "SELECT body FROM evidence WHERE case_id=? AND evidence_id=?",
                (case_id, reference or ""),
            )
            return {
                "evidence": evidence,
                "raw_text": self.read_artifact(evidence["artifact"]).decode(
                    "utf-8", errors="replace"
                ),
            }
        if view == "request":
            snapshot = self._one(
                "SELECT body FROM requests WHERE case_id=? AND request_id=?",
                (case_id, reference or ""),
            )
            assert self.connection is not None
            usage = self.connection.execute(
                "SELECT * FROM request_usage WHERE case_id=? AND request_id=?",
                (case_id, reference),
            ).fetchone()
            return {
                "snapshot": snapshot,
                "provider_input": json.loads(self.read_artifact(snapshot["provider_input"])),
                "usage": dict(usage) if usage else None,
            }
        if view == "provenance":
            matches = [
                item
                for key in ("claims", "reports", "review_issues")
                for item in case.get(key, [])
                if reference in {item.get("claim_id"), item.get("report_id"), item.get("review_id")}
            ]
            return {"historical_readonly": True, "records": matches}
        raise ValueError(f"unsupported historical view: {view}")

    def export(self, case_id: str) -> dict[str, bytes]:
        """Export original rows plus hash-checked artifacts, without new-model decoding."""
        case = self.case(case_id)
        assert self.connection is not None
        result = {"case.json": json.dumps(case, ensure_ascii=False).encode()}
        objects: list[Any] = [case]
        tables = {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in (
            "events",
            "executions",
            "requests",
            "request_usage",
            "receipts",
            "evidence",
            "policies",
        ):
            if table not in tables:
                continue
            rows = [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM {table} WHERE case_id=?", (case_id,)
                )
            ]
            result[table + ".json"] = json.dumps(rows, ensure_ascii=False).encode()
            objects.extend(json.loads(row["body"]) for row in rows if "body" in row)
        if "request_results" in tables:
            rows = [
                dict(row)
                for row in self.connection.execute(
                    (
                        "SELECT r.* FROM request_results r JOIN requests q ON "
                        "r.request_id=q.request_id WHERE q.case_id=?"
                    ),
                    (case_id,),
                )
            ]
            result["request_results.json"] = json.dumps(rows, ensure_ascii=False).encode()
            objects.extend(json.loads(row["body"]) for row in rows)

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if {"artifact_id", "sha256", "size_bytes"} <= value.keys():
                    result["artifacts/" + value["artifact_id"]] = self.read_artifact(value)
                for identity in value.get("artifact_ids", []):
                    if (
                        not isinstance(identity, str)
                        or re.fullmatch(r"[0-9a-f]{64}", identity) is None
                    ):
                        raise ValueError("invalid historical artifact ID")
                    path = self.artifacts / identity
                    if path.is_symlink() or path.resolve().parent != self.artifacts:
                        raise ValueError("historical artifact escapes archive")
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != identity:
                        raise ValueError("historical artifact hash mismatch")
                    result["artifacts/" + identity] = content
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(objects)
        result["manifest.json"] = json.dumps(
            {
                "schema_version": self.schema_version,
                "case_id": case_id,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in result.items()},
            }
        ).encode()
        return result
