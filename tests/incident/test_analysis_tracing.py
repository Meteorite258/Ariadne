import asyncio
import json

import pytest

from tau_coding.incident.analysis import AnalysisCancelled, DockerAnalysisExecutor
from tau_coding.incident.otel import ExecutionExporter, ExportSettings
from tau_incident.analysis import AnalysisLimits
from tau_incident.telemetry.live import Endpoint


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [None, "timeout", "cancel", "cleanup"])
async def test_analysis_fixed_policy_and_cleanup(runtime, case, tmp_path, monkeypatch, failure):
    executor = DockerAnalysisExecutor(
        runtime.store,
        runtime.store.artifacts,
        image="analysis@sha256:" + "a" * 64,
        workspace=tmp_path / "staging",
    )
    script = runtime.store.artifacts.put(b"print('safe')")
    commands = []

    async def command(*args, **kwargs):
        commands.append(args)
        if args[0] == "create":
            mount = args[args.index("--mount") + 1]
            assert mount.endswith(",dst=/inputs,readonly")
            assert "--network=none" in args and "--read-only" in args
            assert "--cap-drop=ALL" in args and "--user=65534:65534" in args
            assert "--pids-limit" in args and "--memory-swap" in args
            assert "--security-opt=no-new-privileges:true" in args
            assert args[args.index("--entrypoint") + 1] == "python"
            return 0, b"created", b""
        if args[0] == "start":
            if failure == "timeout":
                raise TimeoutError()
            if failure == "cancel":
                raise asyncio.CancelledError()
            return (
                0,
                json.dumps(
                    dict(outputs={}, exit_code=0, truncated=False, stdout="safe", stderr="")
                ).encode(),
                b"",
            )
        if args[0] == "inspect":
            return 0, b'{"Running":false,"ExitCode":0}', b""
        if args[0] == "rm":
            return (1 if failure == "cleanup" else 0), b"", b""
        raise AssertionError(args)

    monkeypatch.setattr(executor, "_command", command)
    if failure == "cancel":
        with pytest.raises(AnalysisCancelled) as caught:
            await executor.run(script, (), AnalysisLimits(), case_id="case")
        result = caught.value.result
        assert result.status == "cancelled"
    else:
        result = await executor.run(script, (), AnalysisLimits(), case_id="case")
        expected = {None: "succeeded", "timeout": "failed", "cleanup": "unknown"}
        assert result.status == expected[failure]
    assert result.cleanup_confirmed == (failure != "cleanup")
    assert commands[-1][0] == "rm"
    assert not list((tmp_path / "staging").iterdir())


def test_analysis_rejects_unpinned_image_and_mount_injection(runtime, tmp_path):
    with pytest.raises(ValueError, match="pinned"):
        DockerAnalysisExecutor(
            runtime.store, runtime.store.artifacts, image="python:latest", workspace=tmp_path
        )
    with pytest.raises(ValueError, match="separators"):
        DockerAnalysisExecutor(
            runtime.store,
            runtime.store.artifacts,
            image="analysis@sha256:" + "a" * 64,
            workspace=tmp_path / "a,readonly=false",
        )


def test_trace_export_whitelist_queue_failure_and_receipts(runtime, case, tmp_path):
    exporter = ExecutionExporter(
        ExportSettings(endpoint=Endpoint(url="http://localhost:4318/v1/traces"), queue_size=1),
        runtime.store,
        tmp_path / "gaps.jsonl",
    )
    runtime.execution.on_finished = exporter.enqueue
    first = runtime.execution.start("tool", case_id="case", command_id="one", attempt_id="a")
    second = runtime.execution.start("tool", case_id="case", command_id="two", attempt_id="b")
    assert first.trace_id != second.trace_id
    for op in (first, second):
        runtime.execution.finish(
            op.operation_id,
            status="failed",
            result="secret-result",
            detail="secret-body",
            error_category="secret-error",
        )
    assert exporter.gaps[0]["reason"] == "queue full"
    assert (tmp_path / "gaps.jsonl").exists()
    payload = json.dumps(exporter.payload(runtime.store.execution(first.operation_id)))
    assert "secret" not in payload
    assert first.trace_id in payload and first.attempt_id in payload
    assert runtime.get_case("case") == case
    assert runtime.store.receipt("create").status == "accepted"
