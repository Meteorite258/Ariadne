"""Typed chat input and slash actions; no coding harness is invoked in case mode."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from tau_coding.incident.actions import IncidentAction, IncidentQuery
from tau_coding.incident.session import bind, binding, configured_dispatcher, session_dispatch
from tau_incident.events import (
    AddConstraint,
    AddExplanation,
    AddObservation,
    Command,
    CreateCase,
    ExtendBudget,
    Lifecycle,
)
from tau_incident.models import IncidentCase, ObservationInput, Scope, Source, VersionRef

if TYPE_CHECKING:
    from tau_coding.session import CodingSession


async def query(
    session: CodingSession, case_id: str, view: str = "case", reference: str | None = None
) -> dict[str, Any]:
    request = IncidentQuery.model_validate(
        {"case_id": case_id, "view": view, "reference": reference}
    )
    return await session_dispatch(
        session, {"type": "incident.query", "query": request.model_dump(mode="json")}
    )


async def slash(session: CodingSession, text: str) -> tuple[str, str | None]:
    args = text.strip().split(maxsplit=1)
    operation = args[0] if args else "workspace"
    tail = " ".join(args[1:])
    if operation == "coding":
        await bind(session, None)
        return "Coding mode.", None
    if session.incident_dispatcher is None:
        session.incident_dispatcher = configured_dispatcher(session.cwd)
    if session.incident_dispatcher is None:
        raise ValueError("set AMADEUS_INCIDENT_CONFIG before using /incident")
    settings = session.incident_dispatcher.backend.settings
    request_id = uuid4().hex
    if operation == "new":
        if not tail:
            raise ValueError("/incident new <symptoms>")
        case_id = uuid4().hex
        # Project identity is supplied by authenticated capability discovery.
        backend = session.incident_dispatcher.backend
        from tau_coding.incident.host import IncidentHost

        if isinstance(backend, IncidentHost):
            project_key = backend.config.project_key
        else:
            project_key = str((await backend.request("/capabilities", {}))["project_key"])
        command = Command(
            command_id=request_id,
            case_id=case_id,
            payload=CreateCase(
                project_key=project_key,
                scope=Scope(environment=settings.environment),
                symptoms=tail,
                impact="Unknown; manual description",
                source=Source(kind="human", actor="user"),
            ),
        )
        result = await session_dispatch(
            session,
            {
                "type": "incident.dispatch",
                "action": IncidentAction(
                    request_id=request_id, operation="command", case_id=case_id, command=command
                ).model_dump(mode="json"),
            },
        )
        if result["receipt"]["status"] != "accepted":
            raise ValueError(result["receipt"].get("reason"))
        await bind(session, case_id)
        return json.dumps(result, ensure_ascii=False), case_id
    selected_case = tail if operation == "bind" else await binding(session)
    if selected_case is None:
        raise ValueError("bind a case first")
    case_id = selected_case
    if not case_id:
        raise ValueError("/incident new <symptoms> or /incident bind <case-id> first")
    raw_case = await query(session, case_id)
    if operation == "bind":
        await bind(session, case_id)
        return f"Bound case {case_id} at version {raw_case['version']}", case_id
    if operation == "workspace":
        return "", case_id
    if raw_case.get("historical_readonly"):
        if operation in {
            "report",
            "handoff",
            "timeline",
            "evidence",
            "request",
            "receipt",
            "brief",
            "budget",
            "export",
        } and not (operation == "budget" and tail):
            return json.dumps(
                await query(session, case_id, operation, tail or None), ensure_ascii=False, indent=2
            ), None
        raise ValueError("historical case is read-only; create a new v2 case")
    case = IncidentCase.model_validate(raw_case)
    if operation in {"run", "resume"}:
        action = IncidentAction.model_validate(
            {
                "request_id": request_id,
                "case_id": case_id,
                "operation": operation,
                "limits": settings.auto_start.limits.model_dump(mode="json"),
            }
        )
    elif operation in {
        "report",
        "handoff",
        "timeline",
        "evidence",
        "request",
        "receipt",
        "brief",
        "provenance",
        "action",
        "export",
        "budget",
    } and not (operation == "budget" and tail):
        return json.dumps(
            await query(session, case_id, operation, tail or None), ensure_ascii=False, indent=2
        ), None
    else:
        source = Source(kind="human", actor="user", reference=f"session:{session.session_id}")
        payload: Any
        if operation in {"observe", "explain", "constraint"}:
            if not tail:
                raise ValueError("human input requires text")
            if operation == "observe":
                payload = AddObservation(
                    observation=ObservationInput(
                        summary=tail, raw_text=tail, scope=case.scope, source=source
                    )
                )
            elif operation == "explain":
                payload = AddExplanation(text=tail, scope=case.scope, source=source)
            else:
                payload = AddConstraint(text=tail, scope=case.scope, source=source)
        elif operation in {"pause", "cancel"}:
            payload = Lifecycle.model_validate(
                {"scope": case.scope, "action": operation, "reason": tail or "user requested"}
            )
        elif operation == "budget":
            values = tail.split()
            if len(values) != 1:
                raise ValueError("/incident budget <total-token-limit|unlimited>")
            payload = ExtendBudget(
                scope=case.scope,
                token_limit=None if values[0] == "unlimited" else int(values[0]),
                clear_token_limit=values[0] == "unlimited",
                reason="user requested",
            )
        else:
            raise ValueError(
                "Use new, bind, workspace, coding, run, pause, resume, cancel, observe, explain, "
                "constraint, budget, report, handoff, timeline, evidence, request or receipt"
            )
        action = IncidentAction(
            request_id=request_id,
            operation="command",
            case_id=case_id,
            command=Command(command_id=request_id, case_id=case_id, payload=payload),
            expected_versions=(
                VersionRef(case_id=case_id, kind="case", object_id=case_id, version=case.version),
            ),
        )
    result = await session_dispatch(
        session, {"type": "incident.dispatch", "action": action.model_dump(mode="json")}
    )
    return json.dumps(result, ensure_ascii=False, indent=2), None
