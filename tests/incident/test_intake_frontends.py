import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tau_coding.incident.actions import IncidentAction, IncidentQuery
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.incident.intake import normalize, process_pending, receive
from tau_coding.incident.rpc import IncidentDispatcher
from tau_coding.incident.settings import AutoStart, HostSettings
from tau_incident.store import IdempotencyConflict


@pytest.fixture
def host(tmp_path, clock):
    host = IncidentHost(
        IncidentConfig(tmp_path, "test", "validation", tmp_path / "host"),
        clock=clock,
        settings=HostSettings(environment="test", max_pending_alerts=2),
    )
    yield host
    host.close()


def alert(status="firing", start="2026-09-27T00:00:00Z"):
    return {
        "version": "4",
        "alerts": [
            {
                "status": status,
                "fingerprint": "checkout-error",
                "labels": {
                    "environment": "test",
                    "service": "checkout",
                    "alertname": "Errors",
                    "severity": "critical",
                },
                "startsAt": start,
                "endsAt": "2026-09-27T00:05:00Z" if status == "resolved" else None,
            }
        ],
    }


@pytest.mark.anyio
async def test_durable_intake_deduplication_resolution_and_recurrence(host):
    payload = alert()
    receipt = receive(host, payload, delivery_id="delivery")
    assert (
        host.store._connection.execute("SELECT status FROM alert_inbox").fetchone()[0] == "pending"
    )
    assert host.store._connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
    assert receive(host, payload, delivery_id="delivery")["duplicate"]
    changed = deepcopy(payload)
    changed["alerts"][0]["annotations"] = {"summary": "changed"}
    with pytest.raises(IdempotencyConflict):
        receive(host, changed, delivery_id="delivery")
    await process_pending(host)
    case_id = host.store._connection.execute("SELECT case_id FROM cases").fetchone()[0]
    assert len(host.get_case(case_id).observations) == 1
    receive(host, payload, delivery_id="different-delivery")
    await process_pending(host)
    assert len(host.get_case(case_id).observations) == 1
    receive(host, alert("resolved"))
    await process_pending(host)
    case = host.get_case(case_id)
    assert len(case.observations) == 2
    assert case.investigation_status == "open" and case.impact_status == "unknown"
    recurrence = alert(start="2026-09-28T00:00:00Z")
    assert (
        normalize(recurrence, source="alertmanager", environment="test")[0].occurrence_key
        != normalize(payload, source="alertmanager", environment="test")[0].occurrence_key
    )
    receive(host, recurrence)
    await process_pending(host)
    assert host.store._connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 2
    assert receipt["inbox_id"]


def test_inbox_storm_capacity_prevents_acknowledgement(host):
    receive(host, alert(), delivery_id="1")
    receive(host, alert(), delivery_id="2")
    with pytest.raises(ValueError, match="capacity"):
        receive(host, alert(), delivery_id="3")
    assert host.store._connection.execute("SELECT COUNT(*) FROM alert_inbox").fetchone()[0] == 2


@pytest.mark.anyio
async def test_late_firing_after_resolution_is_preserved_without_auto_restart(host):
    receive(host, alert(), delivery_id="initial")
    await process_pending(host)
    receive(host, alert("resolved"), delivery_id="resolved")
    await process_pending(host)
    host.settings = host.settings.model_copy(
        update={
            "auto_start": AutoStart(
                environments=("test",), services=("checkout",), severities=("critical",)
            )
        }
    )
    late = alert()
    late["alerts"][0]["annotations"] = {"summary": "delayed delivery of earlier firing"}
    receive(host, late, delivery_id="late")
    await process_pending(host)
    row = host.store._connection.execute(
        "SELECT case_id,detail,status FROM alert_updates ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row["detail"] == "out_of_order_conflict" and row["status"] == "processed"
    case = host.get_case(row["case_id"])
    assert len(case.observations) == 3
    assert "out-of-order conflict" in case.observations[-1].summary
    assert case.investigation_status == "open" and case.impact_status == "unknown"
    assert (
        host.store._connection.execute("SELECT COUNT(*) FROM incident_actions").fetchone()[0] == 0
    )


@pytest.mark.anyio
async def test_rpc_and_host_share_actions_events_and_errors(host, create_command):
    dispatcher = IncidentDispatcher(host)
    action = IncidentAction(
        request_id="create", operation="command", case_id="case", command=create_command
    )
    direct = await host.dispatch(action)
    rpc = await dispatcher.dispatch({"type": "incident.dispatch", "action": action.model_dump()})
    assert direct == rpc
    query = IncidentQuery(case_id="case", view="case")
    assert await dispatcher.dispatch(
        {"type": "incident.query", "query": query.model_dump()}
    ) == host.query(query)
    events = await dispatcher.dispatch({"type": "incident.events", "query": query.model_dump()})
    assert len(events["events"]) == 1
    resumed = await dispatcher.dispatch(
        {
            "type": "incident.events",
            "query": {**query.model_dump(), "after_cursor": events["next_cursor"]},
        }
    )
    assert resumed["events"] == []
    bad = action.model_copy(update={"request_id": "wrong"})
    with pytest.raises(ValueError, match="command ID"):
        await host.dispatch(bad)
    with pytest.raises(ValueError, match="command ID"):
        await dispatcher.dispatch({"type": "incident.dispatch", "action": bad.model_dump()})


@pytest.mark.anyio
async def test_auto_start_storm_backpressure_and_running_limit(tmp_path, clock, monkeypatch):
    settings = HostSettings(
        environment="test",
        fixture=tmp_path / "unused.json",
        auto_start=AutoStart(
            environments=("test",),
            services=("checkout",),
            severities=("critical",),
            max_running_cases=1,
            max_queued_cases=1,
        ),
    )
    host = IncidentHost(
        IncidentConfig(tmp_path, "test", "validation", tmp_path / "storm"),
        clock=clock,
        settings=settings,
    )
    entered = asyncio.Queue()
    releases = [asyncio.Event() for _ in range(3)]
    active = 0
    peak = 0
    started = []

    async def controlled_investigation(child, case_id, **kwargs):
        nonlocal active, peak
        index = len(started)
        started.append(case_id)
        active += 1
        peak = max(peak, active)
        await entered.put(case_id)
        try:
            await releases[index].wait()
            return SimpleNamespace(model_dump_json=lambda: '{"validated":true}')
        finally:
            active -= 1

    monkeypatch.setattr("tau_coding.incident.investigation.investigate", controlled_investigation)
    try:
        for day in (27, 28, 29):
            receive(host, alert(start=f"2026-09-{day}T00:00:00Z"), delivery_id=str(day))
        await process_pending(host)
        assert (
            host.store._connection.execute(
                "SELECT COUNT(*) FROM incident_actions WHERE status='queued'"
            ).fetchone()[0]
            == 1
        )
        assert (
            host.store._connection.execute(
                "SELECT COUNT(*) FROM alert_updates WHERE status='associated'"
            ).fetchone()[0]
            == 2
        )
        async with asyncio.timeout(8):
            await entered.get()
            await process_pending(host)
            assert (
                host.store._connection.execute(
                    "SELECT COUNT(*) FROM incident_actions WHERE status='queued'"
                ).fetchone()[0]
                == 1
            )
            assert len(host.children) == active == 1
            releases[0].set()
            await entered.get()
            await process_pending(host)
            releases[1].set()
            await entered.get()
            releases[2].set()
            while active:
                await asyncio.sleep(0)
        assert peak == 1 and len(set(started)) == 3
        assert (
            host.store._connection.execute(
                "SELECT COUNT(*) FROM incident_actions WHERE status='finished'"
            ).fetchone()[0]
            == 3
        )
        assert (
            host.store._connection.execute(
                "SELECT COUNT(*) FROM alert_updates WHERE status='processed'"
            ).fetchone()[0]
            == 3
        )
        for case_id in started:
            assert len(host.get_case(case_id).observations) == 1
    finally:
        for release in releases:
            release.set()
        await host.shutdown()
