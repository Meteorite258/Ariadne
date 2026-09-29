"""Case workspace consuming the same host query and command API as RPC and CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TabbedContent, TabPane, Tree

from tau_coding.incident.chat import query, slash
from tau_coding.incident.session import session_dispatch

if TYPE_CHECKING:
    from tau_coding.session import CodingSession


class IncidentWorkspace(ModalScreen[None]):
    DEFAULT_CSS = """
    IncidentWorkspace { background: $surface; }
    IncidentWorkspace TabbedContent { height: 1fr; }
    IncidentWorkspace #incident-error { height: auto; max-height: 5; }
    IncidentWorkspace Input { dock: bottom; }
    """
    BINDINGS = [("escape", "dismiss", "Close")]

    def __init__(self, session: CodingSession, case_id: str) -> None:
        super().__init__()
        self.session, self.case_id = session, case_id
        self.cursor = 0
        self.records: dict[str, dict[str, Any]] = {}
        self.refreshing = False

    def compose(self) -> ComposeResult:
        yield Static(
            f"Case {self.case_id} — execution links describe operations, not fault causality",
            id="incident-title",
            markup=False,
        )
        with Horizontal():
            for action in ("run", "pause", "resume", "report", "handoff"):
                yield Button(action, id=f"incident-{action}")
        yield Static("", id="incident-error", markup=False)
        with TabbedContent():
            for name in ("Overview", "Hypotheses", "Evidence", "Tasks", "Reports"):
                with TabPane(name, id=f"incident-tab-{name.lower()}"), VerticalScroll():
                    yield Static("", id=f"incident-{name.lower()}-body", markup=False)
            with TabPane("Details", id="incident-details-tab"), VerticalScroll():
                yield Static(
                    "Select a report, claim or execution to inspect its references.",
                    id="incident-details",
                    markup=False,
                )
            with TabPane("References"):
                yield Tree("Reports and claims", id="incident-references")
            with TabPane("Timeline"):
                yield Input(
                    placeholder="Filter: task, attempt, operation, status or error",
                    id="incident-filter",
                )
                yield Tree("Execution operations", id="incident-timeline")
        yield Input(
            placeholder="observe / explain / constraint <text>; evidence / request / receipt <id>",
            id="incident-input",
        )

    async def on_mount(self) -> None:
        await self.refresh_case()
        self.set_interval(2, self.refresh_case)

    async def refresh_case(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        case_id = self.case_id
        try:
            case = await query(self.session, case_id)
            budget = await query(self.session, case_id, "budget")
            views = {
                "overview": {
                    key: case[key]
                    for key in (
                        "symptoms",
                        "scope",
                        "investigation_status",
                        "impact_status",
                        "version",
                        "constraints",
                        "waits",
                    )
                },
                "hypotheses": {
                    key: case[key]
                    for key in ("candidate_explanations", "claims", "findings", "review_issues")
                },
                "evidence": case["observations"],
                "tasks": {"tasks": case["tasks"], "attempts": case["attempts"], "budget": budget},
                "reports": case["reports"],
            }
            page = await session_dispatch(
                self.session,
                {
                    "type": "incident.timeline",
                    "query": {"case_id": case_id, "after_cursor": self.cursor, "limit": 100},
                },
            )
            if case_id != self.case_id:
                return
            for record in page["executions"]:
                self.records[record["operation_id"]] = record
            self.cursor = page["next_cursor"]
            views["overview"]["export_gaps"] = page["export_gaps"]
            for name, value in views.items():
                self.query_one(f"#incident-{name}-body", Static).update(
                    json.dumps(value, ensure_ascii=False, indent=2)
                )
            references: Tree[dict[str, Any]] = self.query_one("#incident-references", Tree)
            references.clear()
            for item in (*case["reports"], *case["claims"]):
                identity = item.get("report_id") or item.get("claim_id")
                references.root.add_leaf(
                    f"{identity} v{item['version']}", data={"reference": identity}
                )
            references.root.expand()
            self.render_timeline()
            self.query_one("#incident-error", Static).update("")
        except Exception as exc:
            self.query_one("#incident-error", Static).update(
                f"Refresh failed; cursor retained for reconnect: {exc}"
            )
        finally:
            self.refreshing = False

    def render_timeline(self) -> None:
        tree: Tree[dict[str, Any]] = self.query_one("#incident-timeline", Tree)
        expanded = {
            node.data["operation_id"]
            for node in tree.root.children
            if node.is_expanded and node.data is not None
        }
        tree.clear()
        needle = self.query_one("#incident-filter", Input).value.lower()
        for record in self.records.values():
            if needle and needle not in json.dumps(record).lower():
                continue
            node = tree.root.add(
                f"{record['operation_kind']} · {record['status']} · {record['duration_ms']} ms",
                data=record,
                expand=record["operation_id"] in expanded,
            )
            for key in (
                "task_id",
                "attempt_id",
                "request_id",
                "command_id",
                "receipt_id",
                "references",
                "links",
                "result",
                "error_category",
                "detail",
                "jaeger_url",
            ):
                if record.get(key) is not None:
                    node.add_leaf(f"{key}: {record[key]}")
        tree.root.expand()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "incident-filter":
            self.render_timeline()
            return
        try:
            output, selected_case = await slash(self.session, event.value)
            if event.value.strip() == "coding":
                self.dismiss()
                return
            if selected_case is not None and selected_case != self.case_id:
                self.case_id = selected_case
                self.cursor = 0
                self.records.clear()
                self.query_one("#incident-title", Static).update(
                    f"Case {self.case_id} — execution links describe operations, "
                    "not fault causality"
                )
                await self.refresh_case()
            self.show_details(output)
            event.input.value = ""
        except Exception as exc:
            self.query_one("#incident-error", Static).update(str(exc))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        try:
            output, _ = await slash(self.session, (event.button.id or "").removeprefix("incident-"))
            self.show_details(output)
        except Exception as exc:
            self.query_one("#incident-error", Static).update(str(exc))

    def show_details(self, text: str) -> None:
        self.query_one("#incident-details", Static).update(text)
        self.query_one(TabbedContent).active = "incident-details-tab"

    async def on_tree_node_selected(self, event: Tree.NodeSelected[dict[str, Any]]) -> None:
        data = event.node.data
        if data is None:
            return
        try:
            if "reference" in data:
                result = await query(self.session, self.case_id, "provenance", data["reference"])
            else:
                result = {"operation": data}
                if data.get("request_id"):
                    result["request"] = await query(
                        self.session, self.case_id, "request", data["request_id"]
                    )
                if data.get("command_id"):
                    result["submission"] = await query(
                        self.session, self.case_id, "receipt", data["command_id"]
                    )
            self.show_details(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.query_one("#incident-error", Static).update(str(exc))
