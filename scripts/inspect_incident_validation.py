"""Print case validation metadata, without model text, tool payloads or credentials."""

import argparse
import json
from collections import Counter
from pathlib import Path

from tau_coding.incident.actions import IncidentQuery
from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.paths import TauPaths
from tau_incident.models import ArtifactRef


def main(case_id, environment):
    with IncidentHost(
        IncidentConfig.resolve(project=Path.cwd(), environment=environment, paths=TauPaths())
    ) as host:
        if host.history.contains(case_id):
            archived = host.query(IncidentQuery(case_id=case_id, view="case"))
            print(
                json.dumps(
                    {
                        "case_id": case_id,
                        "historical_readonly": True,
                        "version": archived["version"],
                        "tasks": Counter(t["status"] for t in archived.get("tasks", [])),
                        "observations": len(archived.get("observations", [])),
                        "findings": len(archived.get("findings", [])),
                        "claims": len(archived.get("claims", [])),
                        "budget": host.query(IncidentQuery(case_id=case_id, view="budget")),
                    }
                )
            )
            cursor = 0
            while page := host.history.query(case_id, "timeline", after=cursor, limit=500)[
                "executions"
            ]:
                for record in page:
                    if record["operation_kind"] == "model_request":
                        print(
                            json.dumps(
                                {
                                    key: record.get(key)
                                    for key in (
                                        "request_id",
                                        "attempt_id",
                                        "status",
                                        "duration_ms",
                                    )
                                }
                            )
                        )
                cursor = page[-1]["cursor"]
            return
        case = host.get_case(case_id)
        print(
            json.dumps(
                {
                    "case_id": case_id,
                    "version": case.version,
                    "tasks": Counter(t.status for t in case.tasks),
                    "observations": len(case.observations),
                    "findings": len(case.findings),
                    "claims": len(case.claims),
                    "usage": host.store.usage_totals(case_id),
                    "field_bytes": {
                        name: len(json.dumps(value, ensure_ascii=False).encode())
                        for name, value in case.model_dump(mode="json").items()
                        if isinstance(value, list)
                    },
                }
            )
        )
        for record in host.executions(case_id, operation_kind="model_request", limit=1000):
            response = host.store.request_result(record.request_id)
            row = {
                "request": record.request_id,
                "attempt": record.attempt_id,
                "status": record.status,
                "duration_ms": record.duration_ms,
            }
            if response:
                message = json.loads(
                    host.artifacts.read(ArtifactRef.model_validate(response["artifact"]))
                )
                row.update(
                    stop_reason=message["stopReason"],
                    usage=message["usage"],
                    text_length=sum(len(b.get("text", "")) for b in message["content"]),
                    tools=[b["name"] for b in message["content"] if b["type"] == "toolCall"],
                )
            print(json.dumps(row))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("case_id")
    parser.add_argument("--environment", required=True)
    args = parser.parse_args()
    main(args.case_id, args.environment)
