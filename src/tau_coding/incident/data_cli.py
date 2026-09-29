"""Explicit dataset export entrypoint; no model session or Case is created."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from tau_coding.incident.services import IncidentServices, ServiceSettings
from tau_incident.models import Scope
from tau_incident.telemetry.dataset import DatasetExporter


def main() -> None:
    parser = argparse.ArgumentParser(description="Export scoped telemetry for queryable replay")
    parser.add_argument("--services-config", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True, help="Scope JSON with bounded window")
    parser.add_argument("--output", type=Path, required=True, help="New dataset directory")
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args()

    def clock() -> datetime:
        return datetime.now(UTC)

    services = IncidentServices(ServiceSettings.from_file(args.services_config), clock=clock)
    scope = Scope.model_validate_json(args.scope.read_bytes())
    manifest = asyncio.run(
        DatasetExporter(
            services.telemetry, services.catalog, clock=clock, max_pages=args.max_pages
        ).export(scope, args.output)
    )
    print(manifest.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
