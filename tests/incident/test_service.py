import httpx
import pytest

from tau_coding.incident.actions import IncidentAction
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.intake import process_pending
from tau_coding.incident.service import create_app
from tau_coding.incident.settings import HostSettings

from .test_intake_frontends import alert


@pytest.mark.anyio
async def test_service_auth_webhook_durability_restart_and_client_reconnect(
    tmp_path, monkeypatch, create_command
):
    monkeypatch.setenv("AMADEUS_INCIDENT_TOKEN", "a" * 24)
    monkeypatch.setenv("AMADEUS_ALERT_TOKEN", "b" * 24)
    config = IncidentConfig(tmp_path, "test", "validation", tmp_path / "service")
    settings = HostSettings(environment="test")
    app = create_app(config, settings)
    assert not config.data_dir.exists()
    action = IncidentAction(
        request_id="create", operation="command", case_id="case", command=create_command
    )
    headers = {"Authorization": "Bearer " + "a" * 24}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/capabilities")).status_code == 401
            assert (
                await client.post("/webhook/alertmanager", headers=headers, json=alert())
            ).status_code == 401
            created = await client.post(
                "/actions", headers=headers, json=action.model_dump(mode="json")
            )
            assert created.status_code == 200 and created.json()["receipt"]["status"] == "accepted"
            response = await client.post(
                "/webhook/alertmanager",
                headers={"Authorization": "Bearer " + "b" * 24, "X-Delivery-Id": "delivery"},
                json=alert(),
            )
            assert response.status_code == 202
            assert (
                app.state.host.store._connection.execute(
                    "SELECT COUNT(*) FROM alert_inbox"
                ).fetchone()[0]
                == 1
            )
            await process_pending(app.state.host)
        # A detached client never shuts down the daemon or loses its committed receipt.
        assert not app.state.host.closed
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
            repeated = await client.post("/actions", json=action.model_dump(mode="json"))
            assert repeated.json() == created.json()
            events = (
                await client.post("/query", json={"case_id": "case", "view": "events"})
            ).json()
            cursor = events["next_cursor"]
        descriptor = config.data_dir / "incident-service.json"
        assert "a" * 24 not in descriptor.read_text()
    assert app.state.host.closed and not descriptor.exists()
    restarted = create_app(config, settings)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test", headers=headers
        ) as client,
    ):
        repeated = await client.post("/actions", json=action.model_dump(mode="json"))
        assert repeated.json() == created.json()
        resumed = await client.post(
            "/query", json={"case_id": "case", "view": "events", "after_cursor": cursor}
        )
        assert resumed.json()["events"] == []
        assert (
            restarted.state.host.store._connection.execute(
                "SELECT COUNT(*) FROM alert_inbox"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.anyio
async def test_webhook_storage_failure_is_not_acknowledged(tmp_path, monkeypatch):
    monkeypatch.setenv("AMADEUS_INCIDENT_TOKEN", "a" * 24)
    monkeypatch.setenv("AMADEUS_ALERT_TOKEN", "b" * 24)
    config = IncidentConfig(tmp_path, "test", "validation", tmp_path / "service")
    app = create_app(config, HostSettings(environment="test", max_body_bytes=1024))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer " + "b" * 24},
        ) as client,
    ):
        oversized = await client.post("/webhook/alertmanager", content=b"x" * 1025)
        assert oversized.status_code == 413
        app.state.host.store._connection.execute(
            "CREATE TRIGGER fail_intake BEFORE INSERT ON alert_inbox "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        response = await client.post("/webhook/alertmanager", json=alert())
        assert response.status_code == 503
        assert (
            app.state.host.store._connection.execute("SELECT COUNT(*) FROM alert_inbox").fetchone()[
                0
            ]
            == 0
        )
