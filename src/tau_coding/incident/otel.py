"""Bounded OTLP/HTTP JSON export of local execution metadata, without an SDK in the core."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from tau_incident.models import ExecutionRecord, Model
from tau_incident.store import CaseStore
from tau_incident.telemetry.live import Endpoint


class ExportSettings(Model):
    endpoint: Endpoint
    service: str = "amadeus-agent"
    environment: str = "amadeus-control"
    queue_size: int = Field(default=256, ge=1, le=10000)
    retries: int = Field(default=2, ge=0, le=5)
    flush_seconds: float = Field(default=5, gt=0, le=60)


class ExecutionExporter:
    def __init__(
        self,
        config: ExportSettings,
        store: CaseStore,
        gap_file: Path,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.config, self.store, self.gap_file = config, store, gap_file
        self.headers = headers or {}
        self.queue: asyncio.Queue[ExecutionRecord] = asyncio.Queue(config.queue_size)
        self.task: asyncio.Task[None] | None = None
        self.gaps: list[dict[str, str]] = []
        self.closed = False

    def gap(self, record: ExecutionRecord, reason: str) -> None:
        gap = {"operation_id": record.operation_id, "reason": reason}
        self.gaps.append(gap)
        self.gaps[:] = self.gaps[-1000:]
        try:
            self.gap_file.parent.mkdir(parents=True, exist_ok=True)
            with self.gap_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(gap) + "\n")
        except OSError:
            # Visible in application diagnostics, never cause an accepted operation to be retried.
            self.gaps.append(
                {"operation_id": record.operation_id, "reason": "gap persistence failed"}
            )

    def enqueue(self, record: ExecutionRecord) -> None:
        if self.closed:
            self.gap(record, "exporter closed")
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self.gap(record, "queue full")

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def payload(self, record: ExecutionRecord) -> dict[str, Any]:
        attributes: list[dict[str, Any]] = []
        # Whitelist excludes detail, query, prompt, response, secrets and arbitrary results.
        for key in (
            "operation_id",
            "case_id",
            "task_id",
            "attempt_id",
            "request_id",
            "tool_call_id",
            "command_id",
            "receipt_id",
            "runtime_generation",
            "status",
        ):
            value = getattr(record, key)
            if value is not None:
                attributes.append({"key": "amadeus." + key, "value": {"stringValue": str(value)}})
        for key, values in (
            ("artifact_ids", record.artifact_ids),
            ("event_ids", record.event_ids),
            ("evidence_ids", tuple(r.object_id for r in record.references if r.kind == "evidence")),
        ):
            attributes.append(
                {
                    "key": "amadeus." + key,
                    "value": {
                        "arrayValue": {"values": [{"stringValue": value} for value in values]}
                    },
                }
            )
        links = []
        for link in record.links:
            try:
                target = self.store.execution(link.operation_id)
                links.append({"traceId": target.trace_id, "spanId": target.span_id})
            except KeyError:
                self.gap(record, "link target missing")
        finish = record.finished_at or record.started_at
        span: dict[str, Any] = {
            "traceId": record.trace_id,
            "spanId": record.span_id,
            "name": "incident." + record.operation_kind,
            "kind": 1,
            "startTimeUnixNano": str(int(record.started_at.timestamp() * 1000000000)),
            "endTimeUnixNano": str(int(finish.timestamp() * 1000000000)),
            "attributes": attributes,
            "links": links,
            "status": {"code": 1 if record.status == "succeeded" else 2},
        }
        if record.parent_span_id:
            span["parentSpanId"] = record.parent_span_id
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": self.config.service}},
                            {
                                "key": "deployment.environment.name",
                                "value": {"stringValue": self.config.environment},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "tau_coding.incident", "version": "1"}, "spans": [span]}
                    ],
                }
            ]
        }

    async def _run(self) -> None:
        async with httpx.AsyncClient(
            timeout=self.config.endpoint.timeout_seconds, trust_env=False, follow_redirects=False
        ) as client:
            while True:
                record = await self.queue.get()
                try:
                    payload = self.payload(record)
                    for retry in range(self.config.retries + 1):
                        try:
                            async with client.stream(
                                "POST", self.config.endpoint.url, json=payload, headers=self.headers
                            ) as response:
                                response.raise_for_status()
                                content = bytearray()
                                async for chunk in response.aiter_bytes():
                                    content.extend(chunk)
                                    if len(content) > self.config.endpoint.max_bytes:
                                        raise ValueError("OTLP response size limit")
                            data = json.loads(content) if content else {}
                            if data.get("partialSuccess", {}).get("rejectedSpans", 0) not in {
                                0,
                                "0",
                            }:
                                self.gap(record, "OTLP partial rejection")
                            break
                        except (httpx.HTTPError, ValueError):
                            if retry == self.config.retries:
                                self.gap(record, "export retry limit")
                            else:
                                await asyncio.sleep(min(2**retry * 0.1, 1))
                except asyncio.CancelledError:
                    self.gap(record, "flush deadline; delivery unknown")
                    raise
                except Exception:
                    self.gap(record, "export mapping failure")
                finally:
                    self.queue.task_done()

    async def close(self) -> None:
        self.closed = True
        try:
            async with asyncio.timeout(self.config.flush_seconds):
                await self.queue.join()
        except TimeoutError:
            while not self.queue.empty():
                record = self.queue.get_nowait()
                self.gap(record, "flush deadline; not sent")
                self.queue.task_done()
        finally:
            if self.task is not None:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
