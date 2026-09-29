import pytest

from tau_incident.events import AddObservation, Command
from tau_incident.memory import MemoryStore, retrieve
from tau_incident.models import ObservationInput, Source
from tau_incident.reporting.builder import ReportBuilder

from .test_review_repair import candidate, observation, review


def test_incomplete_report_memory_filter_and_immediate_invalidation(runtime, case):
    runtime.acquire_owner("case")
    builder = ReportBuilder(runtime)
    report = builder.commit(builder.build("case"))
    assert report.kind == "progress" and report.verification == "unverified"
    memory = MemoryStore(runtime)
    card = memory.rebuild("case")
    assert card.kind == "incomplete"
    assert memory.rebuild("case") == card
    args = dict(project_key=case.project_key, scope=case.scope, symptoms=case.symptoms)
    assert retrieve(runtime.store, **args)[0].card == card
    assert not retrieve(runtime.store, **{**args, "project_key": "other"})
    other = {**args, "scope": case.scope.model_copy(update={"environment": "production"})}
    assert not retrieve(runtime.store, **other)
    assert retrieve(runtime.store, **other, cross_environment=True)[0].differences
    memory.invalidate("case", "new contradictory evidence")
    assert not retrieve(runtime.store, **args)
    state = runtime.get_case("case")
    assert state.reports[-1].state == state.memories[-1].state == "stale"
    assert runtime.store.case_at_version("case", state.version) == state


def test_report_rechecks_new_evidence_before_commit(runtime, case):
    runtime.acquire_owner("case")
    builder = ReportBuilder(runtime)
    stale = builder.build("case")
    runtime.execute(
        Command(
            command_id="observation",
            case_id="case",
            payload=AddObservation(
                observation=ObservationInput(
                    summary="new",
                    raw_text="new data",
                    scope=case.scope,
                    source=Source(kind="human", actor="operator"),
                )
            ),
        )
    )
    with pytest.raises(ValueError):
        builder.commit(stale)
    assert not runtime.get_case("case").reports
    fresh = builder.commit(builder.build("case"))
    assert fresh.input_evidence


@pytest.mark.anyio
async def test_historical_diagnosis_is_only_a_lead_and_rechecked_each_request(
    runtime, case, clock, create_command
):
    from tau_agent import UserMessage
    from tau_agent.request_context import RequestContext
    from tau_incident.context import ContextBuilder, evidence_ref

    lease, old_evidence = candidate(runtime, case)
    result = await review(runtime, case, clock, lease, "accepted")
    assert (
        runtime.execute(Command(command_id="review", case_id="case", payload=result)).status
        == "accepted"
    )
    reports = ReportBuilder(runtime)
    reports.commit(reports.build("case"))
    memory = MemoryStore(runtime)
    old_card = memory.rebuild("case")
    assert old_card.kind == "diagnosis"
    runtime.release_owner()
    new_command = create_command.model_copy(update={"case_id": "new", "command_id": "create-new"})
    assert runtime.execute(new_command).status == "accepted"
    runtime.acquire_owner("new")
    current = runtime.get_case("new")
    contrary = observation(
        runtime, current, "Current dependency is healthy; old diagnosis is misleading"
    )
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1000000)
    view = builder.build_decision("new", tools=(), sources="current measurements", budget_limits={})
    request = RequestContext(
        system="contract", messages=(UserMessage(content="Investigate current incident"),)
    )
    projected, selected = builder.project_request(request, view)
    assert old_card.summary in projected.system
    assert "Historical leads, never current evidence" in projected.system
    assert contrary.summary in projected.system
    assert evidence_ref(contrary) in selected.selection
    assert evidence_ref(old_evidence) not in selected.selection
    assert len(selected.memory_selection) == 1
    assert selected.memory_selection[0].case_id == "case"
    committed_new = runtime.get_case("new")
    runtime.release_owner()
    runtime.acquire_owner("case")
    memory.invalidate("case", "historical report source was corrected")
    # Even a view constructed before invalidation must recheck historical sources.
    refreshed, next_selection = builder.project_request(request, view)
    assert not next_selection.memory_selection
    assert old_card.summary not in refreshed.system
    assert contrary.summary in refreshed.system
    assert runtime.get_case("new") == committed_new
    assert runtime.get_case("case").memories[-1].state == "stale"
