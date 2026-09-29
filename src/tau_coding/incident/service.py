"""Authenticated ASGI transport. Disconnection never owns runtime shutdown."""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from tau_coding.incident.actions import IncidentAction, IncidentQuery, capabilities
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.intake import associate, inbox, process_pending, receive
from tau_coding.incident.settings import HostSettings


class ServiceLock:
    def __init__(self, config: IncidentConfig) -> None:
        self.path = config.data_dir / "incident-service.lock"
        self.stream: Any = None

    def acquire(self) -> None:
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import importlib

                msvcrt = importlib.import_module("msvcrt")
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import importlib

                fcntl = importlib.import_module("fcntl")
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError(
                "an incident service already owns this project; connect to it"
            ) from None

    def release(self) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def create_app(config: IncidentConfig, settings: HostSettings) -> Starlette:
    # No store, socket, provider or task is created by importing or constructing this app.
    token = settings.credential()
    alert_token = settings.credential(webhook=True)
    lock = ServiceLock(config)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        lock.acquire()
        try:
            host = IncidentHost(config, mode="daemon", settings=settings)
        except BaseException:
            lock.release()
            raise
        descriptor = config.data_dir / "incident-service.json"
        try:
            descriptor.write_text(settings.model_dump_json(), encoding="utf-8")
        except BaseException:
            host.close()
            lock.release()
            raise
        app.state.host = host
        app.state.intake_error = None
        stopping = asyncio.Event()

        async def intake_loop() -> None:
            while not stopping.is_set():
                try:
                    await process_pending(host)
                    app.state.intake_error = None
                except Exception as exc:
                    app.state.intake_error = str(exc)
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=1)

        host.ensure_scheduler()
        intake_task = asyncio.create_task(intake_loop())
        try:
            yield
        finally:
            stopping.set()
            await intake_task
            try:
                await host.shutdown()
            finally:
                descriptor.unlink(missing_ok=True)
                lock.release()

    async def endpoint(request: Request) -> Response:
        webhook = request.url.path == "/webhook/alertmanager"
        expected = alert_token if webhook else token
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {expected}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        host: IncidentHost = request.app.state.host
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > settings.max_body_bytes:
                    return JSONResponse({"error": "request body too large"}, status_code=413)
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            if request.url.path in {"/events", "/timeline"}:
                event_query = IncidentQuery.model_validate(
                    {**payload, "view": request.url.path[1:]}
                )
                host.query(IncidentQuery(case_id=event_query.case_id, view="case"))

                async def stream() -> AsyncIterator[str]:
                    cursor = event_query.after_cursor
                    while not await request.is_disconnected():
                        page = host.query(event_query.model_copy(update={"after_cursor": cursor}))
                        cursor = page["next_cursor"]
                        yield f"id: {cursor}\nevent: {page['type']}\ndata: {json.dumps(page)}\n\n"
                        await asyncio.sleep(1)

                return StreamingResponse(stream(), media_type="text/event-stream")
            result: Any
            if webhook or request.url.path == "/intake":
                result = receive(host, payload, delivery_id=request.headers.get("x-delivery-id"))
            elif request.url.path == "/actions":
                result = await host.dispatch(IncidentAction.model_validate(payload))
            elif request.url.path == "/query":
                result = host.query(IncidentQuery.model_validate(payload))
            elif request.url.path == "/associate":
                associate(host, str(payload["update_id"]), str(payload["case_id"]))
                result = {"status": "pending"}
            elif request.url.path == "/inbox":
                result = {
                    **inbox(host, str(payload["inbox_id"]) if payload.get("inbox_id") else None),
                    "intake_error": request.app.state.intake_error,
                }
            else:
                result = {
                    **capabilities(),
                    "environment": config.environment,
                    "project_key": config.project_key,
                    "scheduler": "running"
                    if host.scheduler_task is not None and not host.scheduler_task.done()
                    else "stopped",
                    "intake_error": request.app.state.intake_error,
                }
            return JSONResponse(result, status_code=202 if webhook else 200)
        except (ValueError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse(
                {"error": "local operation failed; query receipt/inbox before retry"},
                status_code=503,
            )

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route(path, endpoint, methods=["POST"])
            for path in (
                "/events",
                "/timeline",
                "/actions",
                "/query",
                "/intake",
                "/webhook/alertmanager",
                "/associate",
                "/inbox",
                "/capabilities",
            )
        ],
    )
