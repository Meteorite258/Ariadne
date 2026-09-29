import json

import pytest

from tau_incident.analysis import AnalysisLimits, AnalysisResult
from tau_incident.budget import RunLimits
from tau_incident.context import evidence_ref
from tau_incident.telemetry import FixtureProvider, ReplayData
from tau_incident.telemetry.tools import worker_tools

from .test_dispatch_wait import queue_task
from .test_review_repair import observation


@pytest.mark.anyio
async def test_analysis_outputs_register_distinct_evidence_with_actual_tool_provenance(
    runtime, case, clock
):
    owner = runtime.acquire_owner("case", RunLimits())
    evidence = observation(runtime, case, "measurement")
    queue_task(runtime, case, "analyze", allowed_tools=("python_analysis",))
    attempt = runtime.claim_ready_tasks(owner.generation, 1)[0]
    parent = runtime.execution.start(
        "investigation",
        case_id="case",
        command_id="worker",
        attempt_id=attempt.attempt_id,
        trace_id=attempt.trace_id,
        runtime_generation=attempt.runtime_generation,
    )

    class Executor:
        async def run(self, script_ref, evidence_ids, limits, *, case_id):
            return AnalysisResult(
                status="succeeded",
                image="analysis@sha256:" + "a" * 64,
                script=script_ref,
                inputs=evidence_ids,
                cleanup_confirmed=True,
                outputs={
                    name: runtime.store.artifacts.put(name.encode())
                    for name in ("a.json", "b.json")
                },
            )

    runtime.analysis, runtime.analysis_limits = Executor(), AnalysisLimits()
    fixture = FixtureProvider(ReplayData(name="empty", rows=()), clock=clock, source="fixture")
    tool = next(
        t
        for t in worker_tools(runtime, attempt, parent, fixture, fixture)
        if t.name == "python_analysis"
    )
    assert "/inputs/manifest.json" in tool.description
    assert "evidence_id" in tool.description
    assert "load_evidence(evidence_id)" in tool.description
    assert "describe_evidence(evidence_id)" in tool.description
    assert "load_evidence" in tool.parameters["properties"]["script"]["description"]
    assert "/outputs" in tool.description
    result = await tool.execute(
        "analysis", {"script": "print('analysis')", "evidence_ids": [evidence.evidence_id]}
    )
    summary = json.loads(result.text)["evidence"]
    payload = json.loads(runtime.store.read_evidence("case", summary["evidence_id"]))
    refs = payload["derived_evidence"]
    assert set(refs) == {"a.json", "b.json"}
    assert refs["a.json"]["object_id"] != refs["b.json"]["object_id"]
    for name, reference in refs.items():
        derived = runtime.store.evidence("case", reference["object_id"])
        assert derived.actual_query["output"] == name
        assert derived.inputs == (evidence_ref(evidence),)
        operation = runtime.store.execution(derived.source.reference)
        assert operation.operation_kind == "tool" and operation.tool_call_id == "analysis"
        assert operation.attempt_id == attempt.attempt_id
        assert derived.source_revision == "analysis@sha256:" + "a" * 64
