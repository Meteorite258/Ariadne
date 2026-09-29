"""Read an existing incident in the Textual workspace without changing the case."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from textual.app import App
from textual.widgets import Input, Static, Tree

from tau_agent.session.entries import CustomEntry
from tau_agent.types import JSONValue
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.rpc import IncidentDispatcher
from tau_coding.incident.session import bind
from tau_coding.incident.settings import HostSettings
from tau_coding.paths import TauPaths
from tau_coding.tui.incident import IncidentWorkspace


async def validate(
    case_id: str, environment: str, evidence_id: str, host_config: Path
) -> dict[str, object]:
    config = IncidentConfig.resolve(project=Path.cwd(), environment=environment, paths=TauPaths())
    settings = HostSettings.from_file(host_config)
    assert settings.environment == environment and settings.jaeger_url
    with IncidentHost(config, settings=settings) as host:
        before = host.get_case(case_id)

        class Session:
            state = SimpleNamespace(entries=[])
            incident_dispatcher = IncidentDispatcher(host)
            cwd = Path.cwd()
            session_id = "stage7-real-tui"

            async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:
                self.state.entries.append(CustomEntry(namespace=namespace, data=data))

        session = Session()
        await bind(session, case_id)  # type: ignore[arg-type]
        app: App[None] = App()
        async with app.run_test(size=(140, 45)) as pilot:
            screen = IncidentWorkspace(session, case_id)  # type: ignore[arg-type]
            await app.push_screen(screen)
            await pilot.pause()
            assert not str(screen.query_one("#incident-error", Static).render())
            overview = str(screen.query_one("#incident-overview-body", Static).render())
            evidence = str(screen.query_one("#incident-evidence-body", Static).render())
            tasks = str(screen.query_one("#incident-tasks-body", Static).render())
            assert before.symptoms in overview
            assert evidence_id in evidence
            assert before.tasks[0].task_id in tasks
            assert len(screen.records) > 0 and screen.cursor > 0
            while len(screen.records) < len(host.executions(case_id, limit=1000)):
                old = screen.cursor
                await screen.refresh_case()
                assert screen.cursor > old, "timeline stopped before the last page"
            total_records = len(screen.records)
            jaeger_links = [
                row["jaeger_url"] for row in screen.records.values() if row.get("jaeger_url")
            ]
            assert jaeger_links and all(
                link.startswith(settings.jaeger_url + "/trace/") for link in jaeger_links
            )
            route_status = httpx.get(jaeger_links[0], timeout=5).status_code
            assert route_status == 200
            filter_input = screen.query_one("#incident-filter", Input)
            filter_input.value = "model_request"
            screen.render_timeline()
            timeline = screen.query_one("#incident-timeline", Tree)
            model_nodes = [node for node in timeline.root.children if node.data is not None]
            assert model_nodes and all(
                node.data is not None and node.data["operation_kind"] == "model_request"
                for node in model_nodes
            )
            assert any(
                "jaeger_url" in str(child.label) for node in model_nodes for child in node.children
            )
            field = screen.query_one("#incident-input", Input)
            field.value = f"evidence {evidence_id}"
            field.focus()
            await pilot.press("enter")
            await pilot.pause()
            details = str(screen.query_one("#incident-details", Static).render())
            assert evidence_id in details
            assert not str(screen.query_one("#incident-error", Static).render())
            after = host.get_case(case_id)
            assert after == before
            return {
                "case_id": case_id,
                "case_version": after.version,
                "observations": len(after.observations),
                "timeline_records": total_records,
                "model_request_nodes": len(model_nodes),
                "jaeger_links": len(jaeger_links),
                "jaeger_visible_in_timeline": True,
                "jaeger_route_status": route_status,
                "evidence_details_read": evidence_id,
                "case_unchanged": True,
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--host-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(
        validate(args.case_id, args.environment, args.evidence_id, args.host_config)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
