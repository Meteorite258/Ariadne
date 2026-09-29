from types import SimpleNamespace

import pytest
from textual.app import App
from textual.widgets import Input, Static

from tau_agent.session.entries import CustomEntry
from tau_coding.incident.chat import slash
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.rpc import IncidentDispatcher
from tau_coding.incident.session import binding
from tau_coding.tui.incident import IncidentWorkspace


@pytest.mark.anyio
async def test_workspace_human_semantics_case_switch_and_return_to_coding(tmp_path):
    with IncidentHost(IncidentConfig(tmp_path, "test", "validation", tmp_path / "host")) as host:

        class Session:
            state = SimpleNamespace(entries=[])
            incident_dispatcher = IncidentDispatcher(host)
            cwd = tmp_path
            session_id = "ui-validation"

            async def append_custom_entry(self, namespace, data):
                self.state.entries.append(CustomEntry(namespace=namespace, data=data))

        session = Session()
        _, case_id = await slash(session, "new Checkout fails")
        app = App()
        async with app.run_test(size=(140, 45)) as pilot:
            screen = IncidentWorkspace(session, case_id)
            await app.push_screen(screen)
            await pilot.pause()
            field = screen.query_one("#incident-input", Input)

            async def enter(text):
                field.value = text
                field.focus()
                await pilot.press("enter")
                await pilot.pause()

            await enter("observe [bold]failure[/bold] observed")
            await enter("explain dependency may be unavailable")
            await enter("constraint only read telemetry")
            state = host.get_case(case_id)
            assert (
                len(state.observations)
                == len(state.candidate_explanations)
                == len(state.constraints)
                == 1
            )
            assert state.observations[0].summary == "[bold]failure[/bold] observed"
            assert not state.claims
            await pilot.click("#incident-pause")
            await pilot.pause()
            assert host.get_case(case_id).investigation_status == "paused"
            await screen.refresh_case()
            assert screen.cursor > 0 and screen.records
            await enter("evidence missing")
            assert "missing" in str(screen.query_one("#incident-error", Static).render())
            await enter("new A different incident")
            second = await binding(session)
            assert second != case_id and screen.case_id == second
            assert all(record["case_id"] == second for record in screen.records.values())
            assert "A different incident" in str(
                screen.query_one("#incident-overview-body", Static).render()
            )
            await enter("coding")
            assert await binding(session) is None
            assert app.screen is not screen
            assert not host.closed
