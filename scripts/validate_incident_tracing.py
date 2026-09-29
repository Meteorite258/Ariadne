"""Real Collector/Jaeger acceptance, including scenario-event isolation regression."""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from tau_coding.incident.otel import ExecutionExporter, ExportSettings
from tau_incident.evidence import ArtifactStore
from tau_incident.execution import ExecutionRecorder
from tau_incident.store import CaseStore
from tau_incident.telemetry.live import Endpoint


async def main(collector: str, jaeger: str, output: Path) -> None:
    def clock():
        return datetime.now(UTC)

    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:

        async def trace(trace_id):
            async with asyncio.timeout(20):
                while True:
                    response = await client.get(f"{jaeger}/api/traces/{trace_id}")
                    if response.status_code == 200 and response.json().get("data"):
                        return response.json()["data"][0]
                    await asyncio.sleep(0.2)

        with TemporaryDirectory(prefix="amadeus-trace-validation-") as temporary:
            directory = Path(temporary)
            store = CaseStore(
                directory / "records.sqlite",
                artifacts=ArtifactStore(directory / "artifacts"),
                clock=clock,
            )
            recorder = ExecutionRecorder(store, clock=clock)
            exporter = ExecutionExporter(
                ExportSettings(endpoint=Endpoint(url=collector)), store, directory / "gaps.jsonl"
            )
            exporter.start()
            recorder.on_finished = exporter.enqueue
            root = recorder.start("tool", case_id=None, command_id="stage7-tracing")
            child = recorder.start(
                "tool", case_id=None, command_id="stage7-child", parent=root, tool_call_id="query"
            )
            recorder.link(child.operation_id, root.operation_id, "retry_of")
            recorder.finish(
                child.operation_id,
                status="failed",
                result="private-sentinel",
                detail="private-sentinel",
            )
            recorder.finish(root.operation_id, status="succeeded", result="private-sentinel")
            await exporter.close()
            assert not exporter.gaps, exporter.gaps
            agent_trace = await trace(root.trace_id)
            spans = {span["spanID"]: span for span in agent_trace["spans"]}
            assert set(spans) == {root.span_id, child.span_id}
            assert all(
                proc["serviceName"] == "amadeus-agent" for proc in agent_trace["processes"].values()
            )
            assert any(ref["spanID"] == root.span_id for ref in spans[child.span_id]["references"])
            assert "private-sentinel" not in json.dumps(agent_trace)
            failed_exporter = ExecutionExporter(
                ExportSettings(
                    endpoint=Endpoint(url="http://127.0.0.1:1/v1/traces", timeout_seconds=0.5),
                    retries=0,
                ),
                store,
                directory / "failed.jsonl",
            )
            before = store.executions(limit=1000)
            failed_exporter.start()
            failed_exporter.enqueue(store.execution(root.operation_id))
            await failed_exporter.close()
            assert failed_exporter.gaps and store.executions(limit=1000) == before
            store.close()

        business_id, span_id = uuid4().hex, uuid4().hex[:16]
        now = time.time_ns()
        attributes = [
            {"key": "service.name", "value": {"stringValue": "amadeus-validation-business"}},
            {"key": "deployment.environment.name", "value": {"stringValue": "validation-business"}},
        ]
        span = {
            "traceId": business_id,
            "spanId": span_id,
            "name": "isolation-regression",
            "startTimeUnixNano": str(now),
            "endTimeUnixNano": str(now + 1000000),
            "events": [
                {"name": "prepared", "timeUnixNano": str(now)},
                {
                    "name": "feature_flag",
                    "timeUnixNano": str(now),
                    "attributes": [
                        {"key": "feature_flag.key", "value": {"stringValue": "paymentUnreachable"}}
                    ],
                },
            ],
        }
        response = await client.post(
            collector,
            json={
                "resourceSpans": [
                    {"resource": {"attributes": attributes}, "scopeSpans": [{"spans": [span]}]}
                ]
            },
        )
        response.raise_for_status()
        business_trace = await trace(business_id)
        assert "paymentUnreachable" not in json.dumps(business_trace)
        assert "feature_flag" not in json.dumps(business_trace)
        assert "prepared" in json.dumps(business_trace)
        assert all(
            proc["serviceName"] != "amadeus-agent" for proc in business_trace["processes"].values()
        )
        result = {
            "agent_trace": root.trace_id,
            "business_trace": business_id,
            "checks": [
                "parent-link",
                "metadata-redaction",
                "export-failure-independent",
                "business-agent-separation",
                "feature-flag-events-removed",
                "ordinary-events-preserved",
            ],
        }
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector", default="http://127.0.0.1:4318/v1/traces")
    parser.add_argument("--jaeger", default="http://127.0.0.1:16686/jaeger/ui")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.collector, args.jaeger, args.output))
