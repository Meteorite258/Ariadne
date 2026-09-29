import json

import pytest

from tau_agent import UserMessage
from tau_agent.request_context import RequestContext
from tau_incident.context import ContextBuilder, ContextInsufficient, evidence_ref
from tau_incident.context_read import context_read_tool

from .test_dispatch_wait import queue_task
from .test_review_repair import candidate, observation


@pytest.mark.anyio
async def test_long_task_keeps_opposition_and_can_retrieve_omitted_records(runtime, case):
    counter = observation(runtime, case, "critical independent counterevidence")
    lease, support = candidate(runtime, case, opposition=(evidence_ref(counter),))
    older = observation(runtime, case, "older exploratory row")
    for index in range(60):
        observation(runtime, case, f"exploration-{index}")
    queue_task(runtime, case, "focused")
    attempt = runtime.claim_ready_tasks(lease.generation, 1)[0]
    builder = ContextBuilder(runtime.store, estimate=len, input_limit=16000)
    view = builder.build_task(attempt)
    request, selection = builder.project_request(
        RequestContext(
            system="fixed instructions",
            messages=(UserMessage(content="Compare causal explanations"),),
        ),
        view,
    )
    size = len(
        json.dumps(
            {
                "system": request.system,
                "messages": [m.model_dump(mode="json") for m in request.messages],
            },
            ensure_ascii=False,
        )
    )
    assert size <= builder.input_limit
    assert support.evidence_id in request.system and counter.evidence_id in request.system
    assert evidence_ref(older) not in selection.selection
    assert any(older.evidence_id in omission for omission in selection.omissions)
    original = runtime.store.read_evidence("case", older.evidence_id)
    parent = runtime.execution.start(
        "investigation",
        case_id="case",
        command_id="index",
        task_id=attempt.task_id,
        attempt_id=attempt.attempt_id,
    )
    tool = context_read_tool(runtime, attempt, parent, [])
    offset, chunks = 0, []
    while True:
        result = json.loads((await tool.execute(f"index:{offset}", {"offset": offset})).text)
        chunks.append(result["text"])
        offset = result["next_offset"]
        if offset is None:
            break
    assert any(
        row["reference"] == evidence_ref(older).model_dump(mode="json")
        for row in json.loads("".join(chunks))
    )
    read = await tool.execute("older", {"reference": evidence_ref(older).model_dump(mode="json")})
    assert json.loads(json.loads(read.text)["text"])["evidence_id"] == older.evidence_id
    assert evidence_ref(older) in runtime.get_case("case").attempts[-1].reads
    assert runtime.store.read_evidence("case", older.evidence_id) == original
    builder.input_limit = 128
    with pytest.raises(ContextInsufficient) as failure:
        builder.project_request(
            RequestContext(system="fixed", messages=(UserMessage(content="continue"),)), view
        )
    assert failure.value.task_id == "focused"
    assert failure.value.required_tokens > failure.value.input_limit
