"""Fixed Docker argv driver. Model inputs never select mounts, images or privileges."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path
from time import monotonic
from uuid import uuid4

from tau_incident.analysis import AnalysisLimits, AnalysisResult
from tau_incident.evidence import ArtifactStore
from tau_incident.models import ArtifactRef
from tau_incident.store import CaseStore


class DockerAnalysisExecutor:
    def __init__(
        self,
        store: CaseStore,
        artifacts: ArtifactStore,
        *,
        image: str,
        workspace: Path,
        docker: str = "docker",
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}", image):
            raise ValueError("analysis image must be pinned by sha256 digest")
        self.store, self.artifacts = store, artifacts
        self.image, self.workspace, self.docker = image, workspace.resolve(), docker
        if any(character in str(self.workspace) for character in (",", "\n", "\r")):
            raise ValueError("analysis staging path contains Docker mount separators")
        self.gaps: list[str] = []

    async def _command(
        self, *args: str, timeout: float = 10, cap: int = 1048576
    ) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            self.docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )

        async def read(stream: asyncio.StreamReader | None) -> bytes:
            assert stream is not None
            chunks = bytearray()
            while chunk := await stream.read(8192):
                chunks.extend(chunk)
                if len(chunks) > cap:
                    raise ValueError("Docker output limit reached")
            return bytes(chunks)

        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await asyncio.gather(read(proc.stdout), read(proc.stderr))
                code = await proc.wait()
            return code, stdout, stderr
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def run(
        self,
        script_ref: ArtifactRef,
        evidence_ids: tuple[str, ...],
        limits: AnalysisLimits,
        *,
        case_id: str,
    ) -> AnalysisResult:
        self.workspace.mkdir(parents=True, exist_ok=True)
        name = "amadeus-analysis-" + uuid4().hex
        result = AnalysisResult(
            status="failed",
            image=self.image,
            script=script_ref,
            inputs=evidence_ids,
            container_name=name,
            limits=limits,
        )
        started = monotonic()
        # Only staged inputs are bound from the host; output lives in bounded tmpfs.
        with tempfile.TemporaryDirectory(prefix="analysis-", dir=self.workspace) as temporary:
            root = Path(temporary)
            script = self.artifacts.read(script_ref)
            total = len(script)
            (root / "script.py").write_bytes(script)
            index: dict[str, str] = {}
            for position, evidence_id in enumerate(evidence_ids):
                content = self.store.read_evidence(case_id, evidence_id)
                total += len(content)
                if total > limits.input_bytes:
                    raise ValueError("analysis input limit exceeded")
                filename = f"evidence-{position}.json"
                (root / filename).write_bytes(content)
                index[evidence_id] = "/inputs/" + filename
            (root / "manifest.json").write_text(json.dumps(index), encoding="utf-8")
            for file in root.iterdir():
                file.chmod(0o444)
            root.chmod(0o755)
            cancelled = False
            try:
                code, _, _ = await self._command(
                    "create",
                    "--pull=never",
                    "--name",
                    name,
                    "--label=amadeus.analysis=true",
                    "--network=none",
                    "--read-only",
                    "--user=65534:65534",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--cpus",
                    str(limits.cpus),
                    "--memory",
                    f"{limits.memory_mb}m",
                    "--memory-swap",
                    f"{limits.memory_mb}m",
                    "--pids-limit",
                    str(limits.pids),
                    "--ulimit",
                    "nofile=64:64",
                    "--ulimit",
                    "core=0:0",
                    "--log-driver=none",
                    "--ipc=none",
                    "--stop-timeout=1",
                    "--tmpfs",
                    f"/outputs:rw,noexec,nosuid,nodev,size={limits.output_bytes},mode=1777",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777",
                    "--mount",
                    f"type=bind,src={root},dst=/inputs,readonly",
                    "--env",
                    f"AMADEUS_OUTPUT_BYTES={limits.output_bytes}",
                    "--env",
                    f"AMADEUS_OUTPUT_FILES={limits.output_files}",
                    "--entrypoint",
                    "python",
                    self.image,
                    "-I",
                    "/runner.py",
                )
                if code:
                    raise ValueError("container creation failed")
                code, stdout, stderr = await self._command(
                    "start",
                    "--attach",
                    name,
                    timeout=limits.timeout_seconds,
                    cap=limits.output_bytes * 2 + 65536,
                )
                if code:
                    raise ValueError("container execution failed")
                envelope = json.loads(stdout)
                outputs: dict[str, ArtifactRef] = {}
                import base64

                output_size = 0
                for key, value in envelope["outputs"].items():
                    if len(outputs) >= limits.output_files or not re.fullmatch(
                        r"[A-Za-z0-9_.-]+", key
                    ):
                        raise ValueError("invalid output manifest")
                    data = base64.b64decode(value, validate=True)
                    output_size += len(data)
                    if output_size > limits.output_bytes:
                        raise ValueError("output limit exceeded")
                    outputs[key] = self.artifacts.put(data, media_type="application/octet-stream")
                exit_code = int(envelope["exit_code"])
                result = result.model_copy(
                    update={
                        "status": "succeeded"
                        if exit_code == 0 and not envelope["truncated"]
                        else "failed",
                        "exit_code": exit_code,
                        "outputs": outputs,
                        "stdout": str(envelope["stdout"])[: limits.output_bytes],
                        "stderr": str(envelope["stderr"])[: limits.output_bytes],
                        "truncated": bool(envelope["truncated"]),
                        "reason": "output_limit"
                        if envelope["truncated"]
                        else "script_exit_nonzero"
                        if exit_code
                        else None,
                    }
                )
            except asyncio.CancelledError:
                cancelled = True
                result = result.model_copy(update={"status": "cancelled", "reason": "cancelled"})
            except (TimeoutError, ValueError, KeyError, TypeError, OSError) as exc:
                overflow = str(exc) == "Docker output limit reached"
                result = result.model_copy(
                    update={
                        "reason": "timeout"
                        if isinstance(exc, TimeoutError)
                        else "output_limit; partial stream unavailable"
                        if overflow
                        else type(exc).__name__,
                        "truncated": overflow,
                    }
                )
            finally:
                cleanup = asyncio.create_task(self._cleanup(name, result))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled = True
                result = cleanup.result()
                # Windows read-only files need write permission before TemporaryDirectory cleanup.
                for file in root.iterdir():
                    file.chmod(0o600)
            # Persist even when cancellation must propagate to the task.
            result = result.model_copy(update={"duration_ms": (monotonic() - started) * 1000})
            self.artifacts.put(result.model_dump_json().encode(), media_type="application/json")
            if cancelled:
                raise AnalysisCancelled(result)
            return result

    async def _cleanup(self, name: str, result: AnalysisResult) -> AnalysisResult:
        try:
            code, raw, _ = await self._command(
                "inspect", "--format", "{{json .State}}", name, timeout=1
            )
            if code == 0:
                state = json.loads(raw)
                if state.get("OOMKilled"):
                    result = result.model_copy(update={"status": "failed", "reason": "oom_killed"})
                if result.exit_code is None and not state.get("Running"):
                    result = result.model_copy(update={"exit_code": state.get("ExitCode")})
        except (OSError, TimeoutError, ValueError):
            self.gaps.append(name + ": state inspection failed")
        try:
            code, _, _ = await self._command("rm", "--force", name, timeout=2)
            clean = code == 0
            if not clean:
                self.gaps.append(name + ": cleanup unconfirmed")
            return result.model_copy(
                update={"cleanup_confirmed": clean, "status": result.status if clean else "unknown"}
            )
        except (OSError, TimeoutError, ValueError):
            self.gaps.append(name + ": cleanup failed")
            return result.model_copy(update={"status": "unknown", "reason": "cleanup failed"})


class AnalysisCancelled(asyncio.CancelledError):
    def __init__(self, result: AnalysisResult) -> None:
        self.result = result
        super().__init__("analysis cancelled")
