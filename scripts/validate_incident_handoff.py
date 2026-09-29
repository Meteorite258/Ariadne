"""Validate exported case replay and timeline without making model requests."""

import argparse
import json
from pathlib import Path

from tau_incident.events import DomainEvent, reduce_case
from tau_incident.models import IncidentCase


def main(handoff_path: Path, timeline_path: Path) -> None:
    handoff = json.loads(handoff_path.read_text())
    timeline = json.loads(timeline_path.read_text())
    expected = IncidentCase.model_validate(handoff["case"])
    replayed: IncidentCase | None = None
    for item in handoff["events"]:
        event = DomainEvent.model_validate(item)
        assert event.case_id == expected.case_id
        replayed = reduce_case(replayed, event)
    assert replayed == expected
    assert handoff["case_version"] == expected.version
    records = {r["operation_id"]: r for r in handoff["executions"]}
    assert len(records) == len(handoff["executions"])
    for observation in expected.observations:
        assert observation.source_operation_id in records
        assert records[observation.source_operation_id]["operation_kind"] == "evidence_registration"
    for receipt in handoff["receipts"]:
        assert receipt["case_id"] == expected.case_id
    for request in handoff["requests"]:
        assert request["case_id"] == expected.case_id
        assert any(r["request_id"] == request["request_id"] for r in records.values())
    rows = timeline["executions"]
    assert rows
    assert [r["operation_id"] for r in rows] == list(records)[: len(rows)]
    assert timeline["next_cursor"] == rows[-1]["cursor"]
    assert all(r["trace_id"] in r["jaeger_url"] for r in rows)
    assert timeline["budget"] == handoff["budget"]
    print(
        json.dumps(
            {
                "case_id": expected.case_id,
                "case_version": expected.version,
                "events": len(handoff["events"]),
                "executions": len(records),
                "requests": len(handoff["requests"]),
                "timeline_rows": len(rows),
                "replay": "passed",
                "diagnosis_count": sum(c.judgment == "diagnosis" for c in expected.claims),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("handoff", type=Path)
    parser.add_argument("timeline", type=Path)
    args = parser.parse_args()
    main(args.handoff, args.timeline)
