"""Derived information comparisons; no second progress or planner state store."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from tau_incident.models import IncidentCase, Observation

if TYPE_CHECKING:
    from tau_incident.store import CaseStore


def evidence_signature(store: CaseStore, evidence: Observation) -> str:
    raw = store.artifacts.read(evidence.artifact)
    try:
        data: Any = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        data = {"sha256": evidence.artifact.sha256}
    if isinstance(data, dict):
        data = {k: v for k, v in data.items() if k not in {"collected_at"}}
    return hashlib.sha256(
        json.dumps(
            {
                "scope": evidence.scope.model_dump(mode="json"),
                "query": evidence.actual_query,
                "source": evidence.source.actor,
                "revision": evidence.source_revision,
                "coverage": evidence.actual_coverage.model_dump(mode="json")
                if evidence.actual_coverage
                else None,
                "result": evidence.result,
                "data": data,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def information_signature(store: CaseStore, case: IncidentCase) -> str:
    value = {
        "evidence": sorted({evidence_signature(store, e) for e in case.observations}),
        "claims": [c.model_dump(mode="json") for c in case.claims],
        "tasks": sorted(
            {
                json.dumps(
                    {
                        "goal": t.goal,
                        "scope": t.scope.model_dump(mode="json"),
                        "conditions": t.completion_conditions,
                        "progress": t.progress.model_dump(
                            mode="json",
                            exclude={
                                "version",
                                "attempt_id",
                                "execution_cursor",
                                "checked_operations",
                                "artifacts",
                                "observations",
                            },
                        ),
                    },
                    sort_keys=True,
                )
                for t in case.tasks
            }
        ),
        "constraints": [c.model_dump(mode="json") for c in case.constraints],
        "waits": [w.model_dump(mode="json") for w in case.waits],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
