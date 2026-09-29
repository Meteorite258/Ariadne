"""Resolve application-owned incident paths without leaking TauPaths into the domain."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tau_coding.paths import TauPaths


@dataclass(frozen=True, slots=True)
class IncidentConfig:
    project: Path
    environment: str
    project_key: str
    data_dir: Path

    @classmethod
    def resolve(cls, *, project: Path, environment: str, paths: TauPaths) -> IncidentConfig:
        project = project.resolve()
        environment = environment.strip()
        if not project.is_dir():
            raise ValueError(f"incident project directory does not exist: {project}")
        if not environment:
            raise ValueError("incident environment must not be empty")
        directory = paths.project_incident_dir(project)
        return cls(project, environment, directory.name, directory / "v2")
