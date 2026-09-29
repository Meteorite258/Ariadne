"""Stage 6 transport and structured CLI operations."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import typer

from tau_coding.incident.client import IncidentClient
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.rpc import IncidentDispatcher
from tau_coding.incident.settings import HostSettings
from tau_coding.paths import TauPaths

OPERATIONS = {
    "serve",
    "connect",
    "dispatch",
    "query",
    "import-alert",
    "inbox",
    "associate",
    "handoff",
    "timeline",
}


def command(arguments: list[str], *, cwd: Path) -> None:
    parser = argparse.ArgumentParser(prog="tau incident")
    parser.add_argument("operation", choices=sorted(OPERATIONS))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--project", type=Path, default=cwd)
    parser.add_argument("--remote", action="store_true")
    parser.add_argument("--json-file", type=Path)
    parser.add_argument("--case-id")
    parser.add_argument("--update-id")
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(arguments)
    settings = HostSettings.from_file(args.config)
    config = IncidentConfig.resolve(
        project=args.project, environment=settings.environment, paths=TauPaths()
    )
    if args.operation == "serve":
        import uvicorn

        from tau_coding.incident.service import create_app

        uvicorn.run(create_app(config, settings), host=settings.bind, port=settings.port)
        return

    async def perform() -> dict[str, object]:
        descriptor = config.data_dir / "incident-service.json"
        effective_settings = HostSettings.from_file(descriptor) if descriptor.exists() else settings
        if effective_settings.environment != settings.environment:
            raise ValueError("service environment differs from requested environment")
        backend = (
            IncidentClient(effective_settings)
            if args.remote or args.operation == "connect" or descriptor.exists()
            else IncidentHost(config, settings=settings)
        )
        dispatcher = IncidentDispatcher(backend)
        payload = json.loads(args.json_file.read_text(encoding="utf-8")) if args.json_file else {}
        operation = args.operation
        if operation == "dispatch":
            request = {"type": "incident.dispatch", "action": payload}
        elif operation in {"query", "timeline", "handoff"}:
            query = payload or {
                "case_id": args.case_id,
                "view": operation if operation != "query" else "brief",
                "after_cursor": args.after,
            }
            request = {"type": "incident.query", "query": query}
        elif operation == "associate":
            request = {
                "type": "incident.associate",
                "payload": {"case_id": args.case_id, "update_id": args.update_id},
            }
        else:
            request = {
                "type": {
                    "connect": "incident.capabilities",
                    "import-alert": "incident.intake",
                    "inbox": "incident.inbox",
                }[operation],
                "payload": payload,
            }
        try:
            result = await dispatcher.dispatch(request)
            if isinstance(backend, IncidentHost) and operation == "dispatch":
                # A one-shot embedded run owns its workers until they settle.
                while backend.scheduler_task is not None:
                    queued = backend.store._connection.execute(
                        "SELECT 1 FROM incident_actions "
                        "WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone()
                    if not queued:
                        break
                    await asyncio.sleep(0.25)
            return result
        finally:
            await dispatcher.aclose()

    rendered = json.dumps(asyncio.run(perform()), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        typer.echo(rendered)
