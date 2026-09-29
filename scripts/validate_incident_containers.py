"""Explicit real-Docker Stage 7 acceptance; run with uv and a pinned image."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from tau_coding.incident.analysis import AnalysisCancelled, DockerAnalysisExecutor
from tau_incident.analysis import AnalysisLimits
from tau_incident.budget import RunLimits
from tau_incident.context import evidence_ref
from tau_incident.coordinator import IncidentRuntime
from tau_incident.events import AddObservation, Command, CreateCase, RecordPlan
from tau_incident.evidence import ArtifactStore, EvidenceRecorder
from tau_incident.execution import ExecutionRecorder
from tau_incident.models import (
    Decision,
    Finding,
    InvestigationTask,
    ObservationInput,
    Scope,
    Source,
)
from tau_incident.store import CaseStore
from tau_incident.submission import SubmissionService
from tau_incident.telemetry import FixtureProvider, ReplayData
from tau_incident.telemetry.tools import worker_tools

BOUNDARIES = """
import json, os, socket
from pathlib import Path
assert os.getuid() == 65534
assert not any("KEY" in key or "TOKEN" in key for key in os.environ)
assert not Path("/var/run/docker.sock").exists()
manifest = json.loads(Path("/inputs/manifest.json").read_text())
assert len(manifest) == 1
assert Path(next(iter(manifest.values()))).read_text() == "registered input"
identity = next(iter(manifest))
assert load_evidence(identity) == "registered input"
assert describe_evidence(identity)["format"] == "text"
try:
    load_evidence("not-authorized")
except ValueError:
    pass
else:
    raise AssertionError("helper allowed unauthorized evidence")
for path in ("/inputs/forbidden", "/forbidden", "/inputs/../forbidden"):
    try:
        Path(path).write_text("must not write")
    except OSError:
        pass
    else:
        raise AssertionError(path)
with socket.socket() as sock:
    sock.settimeout(1)
    try:
        sock.connect(("1.1.1.1", 80))
    except OSError:
        pass
    else:
        raise AssertionError("network available")
status = Path("/proc/self/status").read_text()
assert "CapEff:\\t0000000000000000" in status
assert "NoNewPrivs:\\t1" in status
assert int(Path("/sys/fs/cgroup/memory.max").read_text()) == 134217728
assert int(Path("/sys/fs/cgroup/pids.max").read_text()) == 16
assert Path("/sys/fs/cgroup/cpu.max").read_text().split() == ["50000", "100000"]
Path("/outputs/verified.json").write_text(json.dumps({"boundaries": "passed"}))
print("boundary checks passed")
"""


async def main(image: str, output: Path) -> None:
    checks = []

    def clock():
        return datetime.now(UTC)

    with TemporaryDirectory(prefix="amadeus-container-validation-") as directory:
        root = Path(directory)
        artifacts = ArtifactStore(root / "artifacts")
        store = CaseStore(root / "case.sqlite", artifacts=artifacts, clock=clock)
        runtime = IncidentRuntime(
            store, EvidenceRecorder(artifacts), ExecutionRecorder(store, clock=clock), clock=clock
        )
        scope = Scope(environment="validation", entities=("checkout",))
        source = Source(kind="human", actor="validation")
        assert (
            runtime.execute(
                Command(
                    command_id="new",
                    case_id="container-case",
                    payload=CreateCase(
                        project_key="validation",
                        scope=scope,
                        source=source,
                        symptoms="boundary test",
                        impact="test",
                    ),
                )
            ).status
            == "accepted"
        )
        assert (
            runtime.execute(
                Command(
                    command_id="input",
                    case_id="container-case",
                    payload=AddObservation(
                        observation=ObservationInput(
                            scope=scope, source=source, summary="input", raw_text="registered input"
                        )
                    ),
                )
            ).status
            == "accepted"
        )
        evidence = store.get_case("container-case").observations[0]
        driver = DockerAnalysisExecutor(store, artifacts, image=image, workspace=root / "staging")
        limits = AnalysisLimits(memory_mb=128, pids=16, cpus=0.5, timeout_seconds=15)
        os.environ["AMADEUS_VALIDATION_SECRET_KEY"] = "host-only-sentinel"

        async def run(name: str, script: str, run_limits=limits):
            reference = artifacts.put(script.encode(), media_type="text/x-python")
            result = await driver.run(
                reference, (evidence.evidence_id,), run_limits, case_id="container-case"
            )
            checks.append({"name": name, "result": result.model_dump(mode="json")})
            assert result.cleanup_confirmed, result
            assert result.image == image and result.inputs == (evidence.evidence_id,)
            return result

        try:
            samples = (
                ("json", '{"rows":[{"value":42}]}', {"rows": [{"value": 42}]}),
                ("ndjson", '{"value":1}\n{"value":2}\n', [{"value": 1}, {"value": 2}]),
                (
                    "csv",
                    'service,value\n"checkout,api",42\n',
                    [{"service": "checkout,api", "value": "42"}],
                ),
                ("tsv", "service\tvalue\npayment\t7\n", [{"service": "payment", "value": "7"}]),
                ("text", "原始日志内容", "原始日志内容"),
            )
            inputs, expected = [], {}
            for kind, content, value in samples:
                receipt = runtime.execute(
                    Command(
                        command_id=f"format:{kind}",
                        case_id="container-case",
                        payload=AddObservation(
                            observation=ObservationInput(
                                scope=scope,
                                source=source,
                                summary=kind,
                                raw_text=content,
                            )
                        ),
                    )
                )
                assert receipt.status == "accepted", receipt.reason
                identity = store.get_case("container-case").observations[-1].evidence_id
                inputs.append(identity)
                expected[identity] = (kind, value)
            script = (
                f"expected = {expected!r}\n"
                "for identity, (kind, value) in expected.items():\n"
                "    assert load_evidence(identity) == value\n"
                "    description = describe_evidence(identity)\n"
                "    assert description['format'] == kind\n"
                "    assert len(description['sample']) <= 2000\n"
            )
            result = await driver.run(
                artifacts.put(script.encode(), media_type="text/x-python"),
                tuple(inputs),
                limits,
                case_id="container-case",
            )
            assert result.status == "succeeded" and result.cleanup_confirmed, result
            checks.append({"name": "evidence_formats", "result": result.model_dump(mode="json")})
            runtime.analysis, runtime.analysis_limits = driver, limits
            owner = runtime.acquire_owner("container-case", RunLimits())
            task = InvestigationTask(
                task_id="analysis-task",
                case_id="container-case",
                contract_version=1,
                kind="explore",
                goal="derive a measured result",
                scope=scope,
                source=source,
                completion_conditions=("derive",),
                allowed_tools=("python_analysis",),
            )
            decision = Decision(
                decision_id="analysis-plan",
                case_id="container-case",
                version=1,
                scope=scope,
                source=source,
                choice="investigate",
                reason="derive",
                basis=(),
            )
            receipt = runtime.execute(
                Command(
                    command_id="analysis-plan",
                    case_id="container-case",
                    payload=RecordPlan(scope=scope, decision=decision, task=task),
                )
            )
            assert receipt.status == "accepted", receipt.reason
            attempt = runtime.claim_ready_tasks(owner.generation, 1)[0]
            parent = runtime.execution.start(
                "investigation",
                case_id="container-case",
                command_id="analysis-worker",
                attempt_id=attempt.attempt_id,
                trace_id=attempt.trace_id,
                runtime_generation=attempt.runtime_generation,
            )
            fixture = FixtureProvider(
                ReplayData(name="empty", rows=()), clock=clock, source="fixture"
            )
            tool = next(
                t
                for t in worker_tools(runtime, attempt, parent, fixture, fixture)
                if t.name == "python_analysis"
            )
            script = (
                "import json\nfrom pathlib import Path\n"
                'manifest=json.loads(Path("/inputs/manifest.json").read_text())\n'
                "value=Path(next(iter(manifest.values()))).read_text()\n"
                'Path("/outputs/result.json").write_text(json.dumps({"length":len(value)}))\n'
            )
            result = await tool.execute(
                "derive", {"script": script, "evidence_ids": [evidence.evidence_id]}
            )
            data = json.loads(result.text)
            assert "evidence" in data, data
            state = runtime.get_case("container-case")
            derived = next(
                e for e in state.observations if e.source.actor == "python_analysis_output"
            )
            assert derived.inputs == (evidence_ref(evidence),)
            assert derived.source_revision == image and derived.result == "complete"
            envelope = json.loads(store.read_evidence("container-case", derived.evidence_id))
            import base64

            assert json.loads(base64.b64decode(envelope["content"])) == {
                "length": len("registered input")
            }
            finding = Finding(
                finding_id="derived-finding",
                case_id="container-case",
                attempt_id=attempt.attempt_id,
                scope=scope,
                source=Source(kind="runtime", actor="investigator", reference=attempt.attempt_id),
                observations=(evidence_ref(derived),),
                completion="complete",
                satisfied_conditions=task.condition_ids,
            )
            receipt = SubmissionService(runtime).submit(
                finding, execution_token=attempt.execution_token
            )
            assert receipt.status == "accepted", receipt.reason
            assert runtime.get_case("container-case").tasks[0].status == "completed"
            registration = store.execution(derived.source_operation_id)
            command_record = store.execution(registration.parent_operation_id)
            tool_record = store.execution(command_record.parent_operation_id)
            assert (
                tool_record.tool_call_id == "derive"
                and tool_record.parent_operation_id == parent.operation_id
            )
            assert registration.trace_id == tool_record.trace_id == attempt.trace_id
            runtime.execution.finish(
                parent.operation_id, status="succeeded", result="derived evidence submitted"
            )
            checks.append(
                {
                    "name": "tool-derived-evidence-finding",
                    "evidence_id": derived.evidence_id,
                    "trace_id": attempt.trace_id,
                    "receipt": receipt.receipt_id,
                }
            )
            result = await run("readonly-network-credentials-resources", BOUNDARIES)
            assert result.status == "succeeded", result
            assert json.loads(artifacts.read(result.outputs["verified.json"])) == {
                "boundaries": "passed"
            }
            result = await run(
                "output-symlink", 'import os; os.symlink("/etc/passwd", "/outputs/escape")'
            )
            assert result.status == "failed" and result.truncated and not result.outputs
            result = await run(
                "timeout",
                "import time; time.sleep(120)",
                limits.model_copy(update={"timeout_seconds": 1}),
            )
            assert result.status == "failed" and result.reason == "timeout"
            result = await run("nonzero-exit", "raise SystemExit(7)")
            assert result.status == "failed" and result.exit_code == 7
            result = await run("memory-limit", "data = bytearray(512 * 1024 * 1024)")
            assert result.status == "failed" and result.reason == "oom_killed"
            reference = artifacts.put(b"import time; time.sleep(120)", media_type="text/x-python")
            job = asyncio.create_task(
                driver.run(reference, (evidence.evidence_id,), limits, case_id="container-case")
            )
            try:
                async with asyncio.timeout(10):
                    while True:
                        _, stdout, _ = await driver._command(
                            "ps",
                            "--filter",
                            "label=amadeus.analysis=true",
                            "--format",
                            "{{.Names}}",
                        )
                        if stdout.strip():
                            break
                        await asyncio.sleep(0.1)
                job.cancel()
                try:
                    await job
                except AnalysisCancelled as exc:
                    assert exc.result.status == "cancelled" and exc.result.cleanup_confirmed
                    checks.append(
                        {"name": "cancellation", "result": exc.result.model_dump(mode="json")}
                    )
                else:
                    raise AssertionError("cancellation did not propagate")
            finally:
                if not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
            _, stdout, _ = await driver._command(
                "ps", "-a", "--filter", "label=amadeus.analysis=true", "--format", "{{.Names}}"
            )
            assert not stdout.strip(), "analysis container leaked"
        finally:
            output.write_text(
                json.dumps({"image": image, "checks": checks}, indent=2), encoding="utf-8"
            )
            store.close()
            os.environ.pop("AMADEUS_VALIDATION_SECRET_KEY", None)
    print(json.dumps({"passed": len(checks), "record": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.image, args.output))
