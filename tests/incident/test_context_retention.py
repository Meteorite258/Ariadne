import json

import pytest

from tau_agent import AssistantMessage, ToolCall, ToolResultMessage, UserMessage
from tau_agent.request_context import RequestContext
from tau_incident.context import ContextBuilder, ContextInsufficient, evidence_ref

from .test_dispatch_wait import queue_task
from .test_review_repair import candidate, observation


def test_decision_uses_latest_derived_versions_without_mutating_history(runtime, case):
    from tau_incident.memory import MemoryStore
    from tau_incident.reporting.builder import ReportBuilder

    runtime.acquire_owner(case.case_id)
    observation(runtime, case, "Independent contrary measurement remains available")
    reports = ReportBuilder(runtime)
    memory = MemoryStore(runtime)
    for _ in range(8):
        reports.commit(reports.build(case.case_id))
        memory.rebuild(case.case_id)
    committed = runtime.get_case(case.case_id)
    assert len(committed.reports) == len(committed.memories) == 8
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1000000)
    view = builder.build_decision(case.case_id, tools=(), sources="test", budget_limits={})
    assert view.brief.case.reports == (committed.reports[-1],)
    assert view.brief.case.memories == (committed.memories[-1],)
    assert view.brief.case.observations == committed.observations
    for kind, records, field in (
        ("report", committed.reports, "report_id"),
        ("memory", committed.memories, "memory_id"),
    ):
        for record in records[:-1]:
            assert any(
                f"{kind}:{getattr(record, field)}@{record.version}:" in item
                for item in view.omissions
            )
    assert runtime.get_case(case.case_id) == committed
    assert runtime.store.case_at_version(case.case_id, committed.version) == committed


def test_narrow_task_retains_critical_basis_and_records_history_omissions(runtime, create_command):
    scope = create_command.payload.scope.model_copy(update={"entities": ("checkout", "payment")})
    command = create_command.model_copy(
        update={"payload": create_command.payload.model_copy(update={"scope": scope})}
    )
    assert runtime.execute(command).status == "accepted"
    case = runtime.get_case("case").model_copy(
        update={"scope": scope.model_copy(update={"entities": ("checkout",)})}
    )
    counter = observation(runtime, case, "dependency is healthy in independent measurement")
    owner, critical = candidate(runtime, case, opposition=(evidence_ref(counter),))
    unrelated = observation(runtime, case, "unrelated measurement")
    narrow = case.model_copy(
        update={"scope": case.scope.model_copy(update={"entities": ("payment",)})}
    )
    queue_task(runtime, narrow, "narrow")
    attempt = runtime.claim_ready_tasks(owner.generation, 1)[0]
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1000000)
    view = builder.build_task(attempt)
    assert evidence_ref(critical) in view.selection
    assert evidence_ref(counter) in view.selection
    assert evidence_ref(unrelated) not in view.selection
    assert any(unrelated.evidence_id in reason for reason in view.omissions)
    assert (
        json.loads(view.premises)["claims"][0]["statement"]
        == "dependency failure caused checkout errors"
    )
    latest = UserMessage(content="Return the findings with any remaining gaps.")
    minimal, _ = builder.project_request(
        RequestContext(system="contract", messages=(latest,)), view
    )
    builder.input_limit = len(minimal.system) + 2000
    history = (
        AssistantMessage(content=[ToolCall(id="old", name="query", arguments={})]),
        ToolResultMessage(tool_call_id="old", tool_name="query", content="x" * 20000),
        latest,
    )
    projected, selection = builder.project_request(
        RequestContext(system="contract", messages=history), view
    )
    assert projected.messages == (latest,)
    assert critical.evidence_id in projected.system
    assert counter.evidence_id in projected.system
    assert "dependency failure caused checkout errors" in projected.system
    assert any("old" in reason for reason in selection.omissions)
    assert history[1].text == "x" * 20000


def test_projection_counts_json_escaped_system_before_provider_snapshot(runtime, case):
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1_000_000)
    view = builder.build_decision(case.case_id, tools=(), sources="fixture", budget_limits={})
    request = RequestContext(
        system='"' * 4000,
        messages=(UserMessage(content="Return a decision."),),
    )
    projected, _ = builder.project_request(request, view)
    raw_size = len(
        projected.system + json.dumps([m.model_dump(mode="json") for m in request.messages])
    )
    encoded_size = len(
        json.dumps(
            {
                "system": projected.system,
                "messages": [m.model_dump(mode="json") for m in request.messages],
            },
            ensure_ascii=False,
        )
    )
    assert encoded_size > raw_size + 256
    builder.input_limit = raw_size + 128
    with pytest.raises(ContextInsufficient):
        builder.project_request(request, view)


def test_request_projects_evidence_metadata_and_keeps_raw_query_in_store(runtime, case):
    recorded = observation(runtime, case, "Counterexample remains visible")
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1_000_000)
    view = builder.build_decision(case.case_id, tools=(), sources="fixture", budget_limits={})
    large = recorded.model_copy(
        update={
            "actual_query": {
                "query": {"kind": "logs", "contains": "payment error"},
                "backend": "query-marker-" * 2000,
            }
        }
    )
    projected_case = view.brief.case.model_copy(update={"observations": (large,)})
    projected_brief = view.brief.model_copy(update={"case": projected_case})
    projected_view = view.model_copy(update={"brief": projected_brief})

    projected, selection = builder.project_request(
        RequestContext(system="contract", messages=(UserMessage(content="Inspect evidence."),)),
        projected_view,
    )

    assert large.evidence_id in projected.system
    assert large.summary in projected.system
    assert large.source_operation_id in projected.system
    assert "payment error" in projected.system
    assert "query-marker" not in projected.system
    assert any(
        "raw query details available through evidence_read" in item for item in selection.omissions
    )
    assert view.brief.case.observations == (recorded,)
    assert runtime.store.get_case(case.case_id).observations == (recorded,)


def test_compacted_evidence_read_keeps_a_bounded_raw_excerpt(runtime, case):
    recorded = observation(runtime, case, "One metric row")
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=1_000_000)
    view = builder.build_decision(case.case_id, tools=(), sources="fixture", budget_limits={})
    latest = UserMessage(content="Use the metric row.")
    minimal, _ = builder.project_request(
        RequestContext(system="contract", messages=(latest,)), view
    )
    fixed_size = len(
        json.dumps(
            {"system": minimal.system, "messages": [latest.model_dump(mode="json")]},
            ensure_ascii=False,
        )
    )
    builder.input_limit = fixed_size + 5000
    raw = "ROW_VALUE=42;" + "x" * 16000
    result = ToolResultMessage(
        tool_call_id="read-1",
        tool_name="evidence_read",
        content=json.dumps(
            {"evidence": recorded.model_dump(mode="json"), "raw_text": raw, "next_offset": 16013}
        ),
    )
    request = RequestContext(
        system="contract",
        messages=(
            AssistantMessage(content=[ToolCall(id="read-1", name="evidence_read", arguments={})]),
            result,
            latest,
        ),
    )

    projected, selection = builder.project_request(request, view)

    compacted = json.loads(projected.messages[1].text)
    assert compacted["raw_text_excerpt"].startswith("ROW_VALUE=42;")
    assert len(compacted["raw_text_excerpt"]) == 1200
    assert compacted["next_offset"] == 16013
    assert any("read-1:raw details summarized" in item for item in selection.omissions)
    assert json.loads(result.text)["raw_text"] == raw
