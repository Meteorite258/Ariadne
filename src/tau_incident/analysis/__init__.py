"""Portable analysis contract; the application supplies the isolated container driver."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from tau_incident.models import ArtifactRef, Model


class AnalysisRequest(Model):
    script: str = Field(
        min_length=1,
        max_length=64000,
        description=(
            "Python 3 source. Call load_evidence(evidence_id) to decode "
            "authorized JSON, NDJSON, CSV/TSV or text. "
            "Call describe_evidence(evidence_id) for bounded fields, row count and samples. "
            "Both helpers are supplied as globals by the isolated runner. Raw "
            "paths remain in /inputs/manifest.json."
        ),
    )
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


class AnalysisLimits(Model):
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    cpus: float = Field(default=1, gt=0, le=2)
    memory_mb: int = Field(default=256, ge=64, le=1024)
    pids: int = Field(default=32, ge=8, le=64)
    input_bytes: int = Field(default=8388608, ge=1024, le=33554432)
    output_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    output_files: int = Field(default=16, ge=1, le=64)


class AnalysisResult(Model):
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    image: str
    script: ArtifactRef
    inputs: tuple[str, ...]
    outputs: dict[str, ArtifactRef] = Field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    reason: str | None = None
    cleanup_confirmed: bool = False
    truncated: bool = False
    container_name: str | None = None
    limits: AnalysisLimits | None = None
    duration_ms: float | None = None


class AnalysisExecutor(Protocol):
    async def run(
        self,
        script_ref: ArtifactRef,
        evidence_ids: tuple[str, ...],
        limits: AnalysisLimits,
        *,
        case_id: str,
    ) -> AnalysisResult: ...
