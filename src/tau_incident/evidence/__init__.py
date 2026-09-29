"""Artifacts are published before their references enter a domain transaction."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import AwareDatetime

from tau_incident.models import ArtifactRef, Observation, ObservationInput


class ArtifactStore:
    """Immutable content-addressed files; callers never supply a filesystem path to read."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", artifact_id) is None:
            raise ValueError("invalid artifact ID")
        path = self.root / artifact_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("artifact path escapes its store")
        return path

    def put(self, content: bytes, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        reference = ArtifactRef(
            artifact_id=digest, sha256=digest, size_bytes=len(content), media_type=media_type
        )
        destination = self._path(digest)
        if destination.exists():
            self.read(reference)
            return reference
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Readers see only complete files. Concurrent identical writers publish identical bytes.
            os.replace(temporary, destination)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.read(reference)
        return reference

    def read(self, reference: ArtifactRef) -> bytes:
        content = self._path(reference.artifact_id).read_bytes()
        if (
            len(content) != reference.size_bytes
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise ValueError("artifact content does not match its registered size/hash")
        return content


class EvidenceRecorder:
    """Prepare runtime provenance; CaseStore alone accepts it as committed evidence."""

    def __init__(self, artifacts: ArtifactStore) -> None:
        self.artifacts = artifacts

    def record(
        self,
        observation: ObservationInput,
        *,
        evidence_id: str,
        case_id: str,
        operation_id: str,
        collected_at: AwareDatetime,
        attempt_id: str | None = None,
    ) -> Observation:
        artifact = self.artifacts.put(observation.raw_text.encode("utf-8"))
        data = observation.model_dump(exclude={"raw_text"})
        data["collected_at"] = observation.collected_at or collected_at
        return Observation.model_validate(
            {
                **data,
                "evidence_id": evidence_id,
                "case_id": case_id,
                "source_operation_id": operation_id,
                "artifact": artifact,
                "attempt_id": attempt_id,
            }
        )
