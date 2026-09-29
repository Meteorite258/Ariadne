"""Bounded read-only HTTP adapters. Unknown backend coverage stays unknown."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import Field, model_validator

from tau_agent.types import JSONValue
from tau_incident.models import Model, Scope, WaitCondition
from tau_incident.telemetry import QueryResult, SignalKind, TelemetryQuery, TelemetryRow


class Endpoint(Model):
    url: str
    token_env: str | None = None
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    max_bytes: int = Field(default=8388608, ge=1024, le=67108864)

    @model_validator(mode="after")
    def safe_url(self) -> Endpoint:
        url = httpx.URL(self.url)
        if url.scheme not in {"http", "https"} or url.username or url.password or url.query:
            raise ValueError("endpoint must be HTTP(S) without credentials or query")
        return self


class LiveSettings(Model):
    environment: str
    services: tuple[str, ...] = Field(min_length=1)
    prometheus: Endpoint | None = None
    jaeger: Endpoint | None = None
    opensearch: Endpoint | None = None
    metric_names: tuple[str, ...] = ()
    metric_units: dict[str, str] = Field(default_factory=dict)
    metric_service_label: str = "service_name"
    metric_environment_label: str = "deployment_environment_name"
    step_seconds: int = Field(default=15, ge=1, le=3600)
    trace_environment_tag: str = "deployment.environment.name"
    log_index: str = "otel"
    log_service_field: str = "resource.service.name"
    log_environment_field: str = "resource.deployment.environment.name"
    log_keyword_suffix: str = ".keyword"
    log_time_field: str = "@timestamp"
    max_rows: int = Field(default=5000, ge=200, le=10000)
    max_window_seconds: int = Field(default=3600, ge=1, le=86400)
    agent_service: str = "amadeus-agent"

    @model_validator(mode="after")
    def isolated(self) -> LiveSettings:
        if self.agent_service in self.services:
            raise ValueError("agent service cannot be a business service")
        for name in (*self.metric_names, self.metric_service_label, self.metric_environment_label):
            if not re.fullmatch(r"[a-zA-Z_:][a-zA-Z0-9_:]*", name):
                raise ValueError("invalid Prometheus identifier")
        if not re.fullmatch(r"[a-z0-9_-]+", self.log_index):
            raise ValueError("one explicit log index is required")
        return self


class LiveProvider:
    def __init__(
        self,
        settings: LiveSettings,
        *,
        clock: Callable[[], datetime],
        headers: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.settings, self.clock = settings, clock
        self.headers = headers or {}
        self.source = "live:" + settings.environment

    def capabilities(self) -> tuple[SignalKind, ...]:
        kinds: list[SignalKind] = []
        if self.settings.prometheus and self.settings.metric_names:
            kinds.append("metrics")
        if self.settings.jaeger:
            kinds.append("traces")
        if self.settings.opensearch:
            kinds.append("logs")
        return tuple(kinds)

    def describe(self) -> dict[str, JSONValue]:
        return {
            "source": self.source,
            "signals": list(self.capabilities()),
            "services": list(self.settings.services),
            "metric_names": list(self.settings.metric_names),
            "step_seconds": self.settings.step_seconds,
            "max_window_seconds": self.settings.max_window_seconds,
            "query": "bounded time/entity/literal contains; coverage unknown",
        }

    def scope(self, scope: Scope) -> tuple[str, ...]:
        s = self.settings
        if scope.environment != s.environment or not set(scope.entities) <= set(s.services):
            raise ValueError("query outside configured business environment/services")
        w = scope.time_window
        if w.start is None or w.end is None:
            raise ValueError("live queries require a bounded time window")
        if (w.end - w.start).total_seconds() > s.max_window_seconds:
            raise ValueError("query window exceeds configured maximum")
        return scope.entities or s.services

    async def request(
        self,
        endpoint: Endpoint,
        kind: str,
        path: str,
        payload: dict[str, Any],
        *,
        post: bool = False,
    ) -> Any:
        async with (
            httpx.AsyncClient(
                timeout=endpoint.timeout_seconds, trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                "POST" if post else "GET",
                endpoint.url.rstrip("/") + path,
                headers=self.headers.get(kind, {}),
                json=payload if post else None,
                params=None if post else payload,
            ) as response,
        ):
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > endpoint.max_bytes:
                    raise ValueError("backend response exceeds byte limit")
            return json.loads(content)

    async def query(self, query: TelemetryQuery) -> QueryResult:
        now = self.clock()
        actual: dict[str, JSONValue] = {}
        raw: list[JSONValue] = []
        try:
            entities = self.scope(query.scope)
            if query.kind not in self.capabilities():
                raise ValueError("signal is not configured")
            rows, capped, sampled = await self._rows(query, entities, now, actual, raw)
            rows = [
                r for r in rows if query.contains is None or query.contains in r.model_dump_json()
            ]
            rows.sort(key=lambda r: (r.data_at, r.entity, r.model_dump_json()))
            page = rows[query.offset : query.offset + query.limit]
            more = query.offset + len(page) < len(rows)
            return QueryResult(
                query=query,
                rows=tuple(page),
                result="partial" if page else "no_match",
                actual_coverage=None,
                sampled=sampled,
                truncated=capped or more,
                next_offset=query.offset + len(page) if more else None,
                note="Coverage unknown; availability is first observation"
                + ("; backend cap/partial response, narrow the window" if capped else ""),
                source=self.source + ":" + query.kind,
                collected_at=now,
                actual_query=actual,
                raw=raw,
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            # Never put URLs, auth headers or arbitrary backend errors in evidence metadata.
            return QueryResult(
                query=query,
                rows=(),
                result="failed",
                actual_coverage=None,
                sampled=None,
                truncated=False,
                next_offset=None,
                note="Backend/query failure: " + type(exc).__name__,
                source=self.source + ":" + query.kind,
                collected_at=now,
                actual_query=actual,
                raw=raw,
            )

    async def _rows(
        self,
        q: TelemetryQuery,
        entities: tuple[str, ...],
        now: datetime,
        actual: dict[str, JSONValue],
        raw: list[JSONValue],
    ) -> tuple[list[TelemetryRow], bool, bool | None]:
        s, w = self.settings, q.scope.time_window
        assert w.start is not None and w.end is not None
        start, end = w.start, w.end
        rows: list[TelemetryRow] = []
        capped = False
        requests: list[JSONValue] = []
        actual.update(api=q.kind, requests=requests)

        def add(entity: str, at: datetime, body: dict[str, Any], units: str | None = None) -> None:
            nonlocal capped
            if entity not in entities or not start <= at <= end:
                return
            if len(rows) >= s.max_rows:
                capped = True
                return
            rows.append(
                TelemetryRow(
                    kind=q.kind,
                    environment=s.environment,
                    entity=entity,
                    data_at=at,
                    available_at=now,
                    body=body,
                    units=units,
                    source=self.source + ":" + q.kind,
                )
            )

        if q.kind == "metrics":
            assert s.prometheus is not None
            for entity in entities:
                for metric in s.metric_names:
                    expr = metric + "{" + s.metric_service_label + "=" + json.dumps(entity)
                    expr += "," + s.metric_environment_label + "=" + json.dumps(s.environment) + "}"
                    params: dict[str, Any] = {
                        "query": expr,
                        "start": w.start.timestamp(),
                        "end": w.end.timestamp(),
                        "step": s.step_seconds,
                        "timeout": "10s",
                    }
                    requests.append(params)
                    data = await self.request(
                        s.prometheus, "metrics", "/api/v1/query_range", params
                    )
                    raw.append(data)
                    if data.get("status") != "success":
                        raise ValueError("Prometheus error")
                    capped |= bool(data.get("warnings"))
                    for series in data["data"]["result"]:
                        for at, value in series.get("values", []):
                            add(
                                entity,
                                datetime.fromtimestamp(float(at), UTC),
                                {
                                    "metric": metric,
                                    "labels": series["metric"],
                                    "value": value,
                                    "step_seconds": s.step_seconds,
                                },
                                s.metric_units.get(metric),
                            )
            return rows, capped, True
        if q.kind == "traces":
            assert s.jaeger is not None
            seen: set[tuple[str, str]] = set()
            for entity in entities:
                params = {
                    "service": entity,
                    "start": int(w.start.timestamp() * 1000000),
                    "end": int(w.end.timestamp() * 1000000),
                    "limit": s.max_rows,
                    "tags": json.dumps({s.trace_environment_tag: s.environment}),
                }
                requests.append(params)
                data = await self.request(s.jaeger, "traces", "/api/traces", params)
                if data.get("errors"):
                    raise ValueError("Jaeger error")
                # Project complete traces onto authorized spans before persisting raw data.
                projected: list[JSONValue] = []
                capped |= len(data.get("data", [])) >= s.max_rows
                for trace in data.get("data", []):
                    processes = trace.get("processes", {})
                    for span in trace.get("spans", []):
                        proc = processes.get(span["processID"], {})
                        service = proc.get("serviceName", "")
                        tags = {t["key"]: t.get("value") for t in proc.get("tags", [])}
                        tags.update({t["key"]: t.get("value") for t in span.get("tags", [])})
                        key = (span["traceID"], span["spanID"])
                        if (
                            service not in entities
                            or tags.get(s.trace_environment_tag) != s.environment
                        ):
                            continue
                        if key in seen:
                            continue
                        seen.add(key)
                        body = {**span, "service": service, "process": proc}
                        projected.append(body)
                        add(
                            service,
                            datetime.fromtimestamp(span["startTime"] / 1000000, UTC),
                            body,
                            "microseconds",
                        )
                raw.append(projected)
            return rows, capped, None
        assert s.opensearch is not None
        filters: list[dict[str, Any]] = [
            {"terms": {s.log_service_field + s.log_keyword_suffix: list(entities)}},
            {"term": {s.log_environment_field + s.log_keyword_suffix: s.environment}},
            {"range": {s.log_time_field: {"gte": w.start.isoformat(), "lte": w.end.isoformat()}}},
        ]
        params = {
            "size": s.max_rows,
            "track_total_hits": True,
            "sort": [{s.log_time_field: "asc"}],
            "query": {"bool": {"filter": filters}},
        }
        requests.append(params)
        data = await self.request(
            s.opensearch, "logs", "/" + s.log_index + "/_search", params, post=True
        )
        raw.append(data)
        hits = data["hits"]
        total = hits.get("total", {})
        capped = bool(data.get("timed_out") or data.get("_shards", {}).get("failed"))
        capped |= total.get("value", 0) > len(hits["hits"]) or total.get("relation") == "gte"
        for hit in hits["hits"]:
            source = hit["_source"]

            def field(path: str, source: Any = source) -> Any:
                value: Any = source
                if path in value:
                    return value[path]
                parts = path.split(".")
                for index, part in enumerate(parts):
                    rest = ".".join(parts[index:])
                    if isinstance(value, dict) and rest in value:
                        return value[rest]
                    value = value[part]
                return value

            if field(s.log_environment_field) != s.environment:
                continue
            at = datetime.fromisoformat(str(field(s.log_time_field)).replace("Z", "+00:00"))
            add(str(field(s.log_service_field)), at, {"id": hit["_id"], **source})
        return rows, capped, None

    async def check_wait(self, condition: WaitCondition) -> bool:
        if condition.condition.get("source") != self.source:
            raise ValueError("wait source does not match live provider")
        result = await self.query(
            TelemetryQuery.model_validate(
                {
                    "kind": condition.condition["signal"],
                    "scope": condition.scope.model_dump(),
                    "limit": 200,
                }
            )
        )
        watermark = datetime.fromisoformat(str(condition.condition["watermark"]))
        return any(row.data_at >= watermark for row in result.rows)
