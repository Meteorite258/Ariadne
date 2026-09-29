"""Parse only the positional `incident` tail; leave Tau's prompt parser unchanged."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path
from typing import NoReturn, get_args
from uuid import uuid4

import httpx
import typer

from tau_coding.incident.config import IncidentConfig
from tau_coding.incident.host import IncidentHost
from tau_coding.paths import TauPaths
from tau_incident.budget import RunLimits
from tau_incident.coordinator import LocalRecordingError
from tau_incident.events import (
    AddConstraint,
    AddExplanation,
    AddObservation,
    Command,
    CreateCase,
    ExtendBudget,
    Lifecycle,
    Payload,
    RetryTask,
)
from tau_incident.models import (
    ObservationInput,
    OperationKind,
    Scope,
    Source,
    TimeWindow,
    VersionRef,
)
from tau_incident.store import CommitUnknown


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if message:
            typer.echo(message, err=status != 0)
        raise typer.Exit(status)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="tau incident",
        description="Incident records and bounded read-only investigation.",
        epilog=(
            "Service/API: serve, connect, dispatch, query, import-alert, inbox, "
            "associate, timeline, handoff; use --config HOST.json."
        ),
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for name in (
        "new",
        "observe",
        "show",
        "report",
        "run",
        "resume",
        "pause",
        "cancel",
        "budget",
        "retry",
        "memory",
        "reopen",
        "recheck",
        "withdraw-report",
        "evidence-applicability",
        "impact",
        "revise-task",
    ):
        sub = commands.add_parser(name)
        sub.add_argument(
            "--project",
            type=Path,
            help="Project directory; defaults to Tau --cwd or current directory",
        )
        sub.add_argument("--environment", required=True)
        if name != "new":
            sub.add_argument("case_id")
        if name in {"new", "observe"}:
            sub.add_argument("text", nargs="?", help="Symptoms or manual input")
            sub.add_argument(
                "--command-id", help="Stable retry ID; generated and printed if omitted"
            )
            sub.add_argument("--actor", default="user")
            sub.add_argument("--source-ref")
            sub.add_argument("--entity", action="append", default=[])
            sub.add_argument("--start", help="ISO 8601 timestamp including timezone")
            sub.add_argument("--end", help="ISO 8601 timestamp including timezone")
        if name == "new":
            sub.add_argument(
                "--impact", default="Unknown; reported symptoms have not been investigated."
            )
        elif name == "observe":
            sub.add_argument(
                "--kind",
                choices=("observation", "explanation", "constraint"),
                default="observation",
            )
            sub.add_argument("--expected-version", type=int)
            sub.add_argument(
                "--raw-file",
                type=Path,
                help="UTF-8 raw observation; summary remains positional text",
            )
            sub.add_argument(
                "--observation-json",
                type=Path,
                help="Full ObservationInput JSON; replaces text/source/scope flags",
            )
            sub.add_argument(
                "--result",
                choices=("complete", "partial", "no_match", "failed", "unknown"),
                default="unknown",
            )
        elif name in {"pause", "cancel", "budget", "retry"}:
            sub.add_argument("--command-id")
            sub.add_argument("--reason", default="user requested control change")
            if name == "cancel":
                sub.add_argument("--task-id")
            if name == "retry":
                sub.add_argument("--task-id", required=True)
            if name == "budget":
                sub.add_argument("--token-limit", type=int)
                sub.add_argument("--clear-token-limit", action="store_true")
                sub.add_argument("--add-tokens", type=int, default=0)
                sub.add_argument(
                    "--deadline", help="Extend the case deadline to this aware ISO timestamp"
                )
        elif name in {"run", "resume"}:
            data_source = sub.add_mutually_exclusive_group(required=True)
            data_source.add_argument("--fixture", type=Path)
            data_source.add_argument("--services-config", type=Path)
            sub.add_argument("--provider")
            sub.add_argument("--model")
            sub.add_argument(
                "--fixture-now", help="ISO timestamp with timezone for replay availability"
            )
            sub.add_argument("--format-repairs", type=int, default=2)
            sub.add_argument("--max-repair-rounds", type=int, default=2)
            sub.add_argument(
                "--checkpoint-steps",
                type=int,
                help="Optional model requests in this run before a resumable progress checkpoint",
            )
            sub.add_argument("--token-limit", type=int)
            sub.add_argument("--context-tokens", type=int, default=32768)
            sub.add_argument("--output-tokens", type=int, default=4096)
            sub.add_argument("--concurrency", type=int, default=2)
            sub.add_argument("--global-concurrency", type=int, default=8)
            sub.add_argument(
                "--deadline", help="Case budget deadline, timezone-aware ISO timestamp"
            )
            sub.add_argument("--format", choices=("markdown", "json"), default="markdown")
        elif name in {
            "reopen",
            "recheck",
            "withdraw-report",
            "evidence-applicability",
            "impact",
            "revise-task",
        }:
            sub.add_argument("--command-id")
            sub.add_argument("--reason", required=True)
            if name == "recheck":
                sub.add_argument("--review-id", required=True)
                sub.add_argument("--new-check", required=True)
            elif name == "withdraw-report":
                sub.add_argument("--version", type=int, required=True)
            elif name == "evidence-applicability":
                sub.add_argument("--evidence-id", required=True)
                sub.add_argument("--applicable", choices=("yes", "no"), required=True)
            elif name == "impact":
                sub.add_argument(
                    "--status", choices=("unknown", "ongoing", "recovered"), required=True
                )
                sub.add_argument("--evidence-id", action="append", default=[])
            elif name == "revise-task":
                sub.add_argument("--task-json", type=Path, required=True)
        elif name == "memory":
            sub.add_argument("--cross-environment", action="store_true")
            sub.add_argument("--limit", type=int, default=5)
            sub.add_argument("--rebuild", action="store_true")
        elif name == "report":
            sub.add_argument("--format", choices=("markdown", "json"), default="markdown")
            sub.add_argument("--version", type=int)
            sub.add_argument(
                "--commit", action="store_true", help="Commit a versioned report and rebuild memory"
            )
        elif name == "show":
            sub.add_argument(
                "--view",
                choices=(
                    "case",
                    "brief",
                    "events",
                    "executions",
                    "evidence",
                    "receipt",
                    "request",
                    "budget",
                    "manifest",
                    "owner",
                    "export",
                ),
                default="brief",
            )
            sub.add_argument("--after", type=int, default=0)
            sub.add_argument("--limit", type=int, default=100)
            sub.add_argument("--attempt-id")
            sub.add_argument("--operation", choices=get_args(OperationKind))
            sub.add_argument("--evidence-id")
            sub.add_argument("--command-id")
            sub.add_argument("--request-id")
        else:
            sub.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def _json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


def incident_command(
    arguments: list[str], *, cwd: Path, provider_name: str | None = None, model: str | None = None
) -> None:
    try:
        from tau_coding.incident.frontend_cli import OPERATIONS, command

        if arguments and arguments[0] in OPERATIONS:
            command(arguments, cwd=cwd)
            return
        args = _parser().parse_args(arguments)
        if args.action in {"run", "resume"}:
            args.provider = args.provider or provider_name
            args.model = args.model or model
        config = IncidentConfig.resolve(
            project=args.project or cwd, environment=args.environment, paths=TauPaths()
        )
        with IncidentHost(config) as host:
            if args.action in {"new", "observe"}:
                _write(host, args)
            elif args.action in {"run", "resume"}:
                _run(host, args)
            elif args.action in {"pause", "cancel", "budget", "retry"}:
                _control(host, args)
            elif args.action in {
                "reopen",
                "recheck",
                "withdraw-report",
                "evidence-applicability",
                "impact",
                "revise-task",
            }:
                _quality_control(host, args)
            elif args.action == "report":
                from tau_incident.memory import MemoryStore
                from tau_incident.reporting import ReportBuilder, render_report

                if host.history.contains(args.case_id):
                    if args.commit:
                        raise ValueError("historical case is read-only; cannot commit a report")
                    from tau_coding.incident.actions import IncidentQuery

                    reports = host.query(IncidentQuery(case_id=args.case_id, view="report"))[
                        "reports"
                    ]
                    saved_old = next(
                        (
                            r
                            for r in reversed(reports)
                            if args.version is None or r["version"] == args.version
                        ),
                        None,
                    )
                    if saved_old is None:
                        raise ValueError("unknown historical report version")
                    _json(saved_old)
                    return
                case = host.get_case(args.case_id)
                if args.commit:
                    host.runtime.acquire_owner(args.case_id)
                    try:
                        builder = ReportBuilder(host.runtime)
                        builder.commit(builder.build(args.case_id))
                        MemoryStore(host.runtime).rebuild(args.case_id)
                    finally:
                        host.runtime.release_owner()
                    case = host.get_case(args.case_id)
                saved = next(
                    (
                        r
                        for r in reversed(case.reports)
                        if args.version is None or r.version == args.version
                    ),
                    None,
                )
                if args.version is not None and saved is None:
                    raise ValueError("unknown report version")
                if saved is not None:
                    if args.format == "json":
                        _json(saved.model_dump(mode="json"))
                    else:
                        typer.echo(render_report(saved))
                    return
                report = host.report(args.case_id)
                if args.format == "json":
                    _json(report.model_dump(mode="json"))
                else:
                    typer.echo(report.markdown)
            elif args.action == "memory":
                from tau_incident.memory import MemoryStore

                host.get_case(args.case_id)
                memory = MemoryStore(host.runtime)
                if args.rebuild:
                    host.runtime.acquire_owner(args.case_id)
                    try:
                        _json(memory.rebuild(args.case_id).model_dump(mode="json"))
                    finally:
                        host.runtime.release_owner()
                else:
                    _json(
                        [
                            m.model_dump(mode="json")
                            for m in memory.retrieve(
                                args.case_id,
                                cross_environment=args.cross_environment,
                                limit=args.limit,
                            )
                        ]
                    )
            else:
                _show(host, args)
    except LocalRecordingError as exc:
        if exc.receipt is not None:
            _json({"receipt": exc.receipt.model_dump(mode="json"), "recording_error": str(exc)})
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    except (
        ValueError,
        KeyError,
        OSError,
        RuntimeError,
        httpx.HTTPError,
        sqlite3.Error,
        CommitUnknown,
    ) as exc:
        typer.echo(f"Incident command failed: {exc}", err=True)
        raise typer.Exit(1) from exc


def _quality_control(host: IncidentHost, args: argparse.Namespace) -> None:
    from tau_incident.events import (
        ReopenCase,
        ReviseEvidence,
        ReviseTask,
        ScheduleReview,
        SetImpact,
        WithdrawReport,
    )
    from tau_incident.models import EvidenceApplicability, InvestigationTask
    from tau_incident.quality import ref

    case = host.get_case(args.case_id)
    if args.command_id:
        existing = host.receipt(args.case_id, args.command_id)
        if existing is not None:
            base = next((r for r in existing.expected_versions if r.kind == "case"), None)
            if base is not None:
                case = host.store.case_at_version(case.case_id, base.version)
    payload: Payload
    if args.action == "reopen":
        payload = ReopenCase(scope=case.scope, reason=args.reason)
    elif args.action == "recheck":
        issue = next((i for i in case.review_issues if i.review_id == args.review_id), None)
        if issue is None or issue.status != "unresolved":
            raise ValueError("recheck requires an unresolved issue")
        revised = issue.model_copy(
            update={
                "version": issue.version + 1,
                "cycle": issue.cycle + 1,
                "repair_rounds": 0,
                "status": "open",
                "disposition": "pending",
                "new_check": args.new_check,
                "required_action": args.new_check,
                "stop_reason": None,
                "source": Source(kind="human", actor="user", reference=args.reason),
            }
        )
        payload = ScheduleReview(scope=case.scope, issue=revised)
    elif args.action == "withdraw-report":
        payload = WithdrawReport(
            scope=case.scope,
            target=ref(case, "report", f"report:{case.case_id}", args.version),
            reason=args.reason,
        )
    elif args.action == "evidence-applicability":
        evidence = host.store.evidence(case.case_id, args.evidence_id)
        reference = ref(case, "evidence", evidence.evidence_id, evidence.version)
        payload = ReviseEvidence(
            scope=case.scope,
            change=EvidenceApplicability(
                evidence=reference,
                version=1 + sum(c.evidence == reference for c in case.evidence_changes),
                applicable=args.applicable == "yes",
                reason=args.reason,
                source=Source(kind="human", actor="user"),
            ),
        )
    elif args.action == "revise-task":
        task = InvestigationTask.model_validate_json(args.task_json.read_text(encoding="utf-8"))
        payload = ReviseTask(scope=case.scope, task=task, reason=args.reason)
    else:
        payload = SetImpact(
            scope=case.scope,
            status=args.status,
            reason=args.reason,
            basis=tuple(
                ref(case, "evidence", eid, host.store.evidence(case.case_id, eid).version)
                for eid in args.evidence_id
            ),
        )
    receipt = host.execute(
        Command(command_id=args.command_id or uuid4().hex, case_id=case.case_id, payload=payload),
        (ref(case, "case", case.case_id, case.version),),
    )
    _json(receipt.model_dump(mode="json"))
    if receipt.status != "accepted":
        raise typer.Exit(2)


def _write(host: IncidentHost, args: argparse.Namespace) -> None:
    command_id = args.command_id or uuid4().hex
    case_id = host.case_id_for_command(command_id) if args.action == "new" else args.case_id
    typer.echo(f"command_id={command_id} case_id={case_id}", err=True)
    source = Source(kind="human", actor=args.actor, reference=args.source_ref)
    previous = host.get_case(case_id) if args.action == "observe" else None
    scope = Scope(
        environment=host.config.environment,
        entities=tuple(args.entity) or (previous.scope.entities if previous else ()),
        time_window=TimeWindow.model_validate({"start": args.start, "end": args.end})
        if args.start or args.end or previous is None
        else previous.scope.time_window,
    )
    payload: Payload
    if args.action == "new":
        if not args.text:
            raise ValueError("new requires a symptom description")
        payload = CreateCase(
            project_key=host.config.project_key,
            scope=scope,
            symptoms=args.text,
            impact=args.impact,
            source=source,
        )
    elif args.kind == "observation":
        if args.observation_json:
            if (
                args.text
                or args.raw_file
                or args.entity
                or args.start
                or args.end
                or args.source_ref
                or args.actor != "user"
                or args.result != "unknown"
            ):
                raise ValueError(
                    "--observation-json cannot be combined with observation text/metadata flags"
                )
            observation = ObservationInput.model_validate_json(
                args.observation_json.read_text(encoding="utf-8")
            )
            if observation.source.kind != "human":
                raise ValueError("manual observation source kind must be human")
        else:
            if not args.text:
                raise ValueError("observe requires a summary or --observation-json")
            raw = args.raw_file.read_bytes().decode("utf-8") if args.raw_file else args.text
            observation = ObservationInput(
                summary=args.text, raw_text=raw, scope=scope, source=source, result=args.result
            )
        payload = AddObservation(observation=observation)
    else:
        if not args.text or args.raw_file or args.observation_json or args.result != "unknown":
            raise ValueError(
                "explanation/constraint requires text and cannot use observation-only flags"
            )
        payload = (
            AddExplanation(text=args.text, scope=scope, source=source)
            if args.kind == "explanation"
            else AddConstraint(text=args.text, scope=scope, source=source)
        )
    versions: tuple[VersionRef, ...] = ()
    if args.action == "observe" and args.expected_version is not None:
        versions = (
            VersionRef(
                case_id=case_id, kind="case", object_id=case_id, version=args.expected_version
            ),
        )
    receipt = host.execute(
        Command(command_id=command_id, case_id=case_id, payload=payload), versions
    )
    _json(receipt.model_dump(mode="json"))
    if receipt.status != "accepted":
        raise typer.Exit(2)


def _show(host: IncidentHost, args: argparse.Namespace) -> None:
    if host.history.contains(args.case_id):
        from tau_coding.incident.actions import IncidentQuery

        reference = (
            getattr(args, "evidence_id", None)
            or getattr(args, "request_id", None)
            or getattr(args, "command_id", None)
        )
        _json(
            host.query(
                IncidentQuery.model_validate(
                    {
                        "case_id": args.case_id,
                        "view": "timeline" if args.view == "executions" else args.view,
                        "reference": reference,
                        "after_cursor": args.after,
                        "limit": args.limit,
                    }
                )
            )
        )
        return
    if args.view == "export":
        from tau_coding.incident.actions import IncidentQuery

        _json(host.query(IncidentQuery(case_id=args.case_id, view="export")))
    elif args.view == "owner":
        from tau_incident.store.control import owner

        host.get_case(args.case_id)
        lease = owner(host.store, args.case_id)
        _json({"owner": lease.model_dump(mode="json") if lease else None})
    elif args.view == "manifest":
        host.get_case(args.case_id)
        if not args.attempt_id:
            raise ValueError("--view manifest requires --attempt-id")
        manifest = host.store.read_manifest(args.case_id, args.attempt_id)
        _json(
            {
                "manifest": manifest.model_dump(mode="json"),
                "request_selections": {
                    rid: [
                        ref.model_dump(mode="json")
                        for ref in host.store.request(args.case_id, rid).selection
                    ]
                    for rid in manifest.requests
                },
            }
        )
    elif args.view == "request":
        host.get_case(args.case_id)
        if not args.request_id:
            raise ValueError("--view request requires --request-id")
        snapshot = host.store.request(args.case_id, args.request_id)
        _json(
            {
                "snapshot": snapshot.model_dump(mode="json"),
                "provider_input": json.loads(host.artifacts.read(snapshot.provider_input)),
                "usage": host.store.request_usage(args.case_id, args.request_id),
            }
        )
    elif args.view == "budget":
        host.get_case(args.case_id)
        _json(host.store.budget_summary(args.case_id))
    elif args.view in {"events", "executions"}:
        if args.view == "events":
            events = host.events(args.case_id, args.after, limit=args.limit)
            _json(
                {
                    "events": [item.model_dump(mode="json") for item in events],
                    "next_cursor": events[-1].cursor if events else args.after,
                }
            )
        else:
            records = host.executions(
                args.case_id,
                attempt_id=args.attempt_id,
                operation_kind=args.operation,
                after_cursor=args.after,
                limit=args.limit,
            )
            _json(
                {
                    "executions": [item.model_dump(mode="json") for item in records],
                    "next_cursor": records[-1].cursor if records else args.after,
                }
            )
    elif args.view == "evidence":
        if not args.evidence_id:
            raise ValueError("--view evidence requires --evidence-id")
        host.get_case(args.case_id)
        evidence = host.store.evidence(args.case_id, args.evidence_id)
        _json(
            {
                "evidence": evidence.model_dump(mode="json"),
                "raw_text": host.read_evidence(args.case_id, args.evidence_id).decode("utf-8"),
            }
        )
    elif args.view == "receipt":
        if not args.command_id:
            raise ValueError("--view receipt requires --command-id")
        receipt = host.receipt(args.case_id, args.command_id)
        _json({"receipt": receipt.model_dump(mode="json") if receipt else None})
    else:
        value = host.get_case(args.case_id) if args.view == "case" else host.brief(args.case_id)
        _json(value.model_dump(mode="json"))


def _run(host: IncidentHost, args: argparse.Namespace) -> None:
    from tau_coding.incident.investigation import investigate

    limits = RunLimits(
        format_repairs=args.format_repairs,
        max_repair_rounds=args.max_repair_rounds,
        checkpoint_steps=args.checkpoint_steps,
        token_limit=args.token_limit,
        context_tokens=args.context_tokens,
        output_tokens=args.output_tokens,
        concurrency=args.concurrency,
        global_concurrency=args.global_concurrency,
        deadline=TimeWindow.model_validate({"start": args.deadline}).start,
    )
    remote = host.owner_service(args.case_id)
    if remote is not None:
        from tau_coding.incident.actions import IncidentAction
        from tau_coding.incident.client import IncidentClient

        async def forward() -> dict[str, object]:
            client = IncidentClient(remote)
            try:
                return await client.dispatch(
                    IncidentAction.model_validate(
                        {
                            "request_id": uuid4().hex,
                            "operation": args.action,
                            "case_id": args.case_id,
                            "limits": limits.model_dump(mode="json"),
                        }
                    )
                )
            finally:
                await client.aclose()

        _json(asyncio.run(forward()))
        return
    instant = TimeWindow.model_validate({"start": args.fixture_now}).start
    result = asyncio.run(
        investigate(
            host,
            args.case_id,
            fixture=args.fixture,
            services_config=args.services_config,
            limits=limits,
            provider_name=args.provider,
            model=args.model,
            fixture_clock=(lambda: instant) if instant is not None else None,
            resume=args.action == "resume",
        )
    )
    if args.format == "json":
        _json(result.model_dump(mode="json"))
    else:
        from tau_incident.reporting import render_report

        markdown = render_report(result.diagnosis) if result.diagnosis else result.report.markdown
        typer.echo(
            f"Stopped: {result.stop_reason}\nSubmitted tasks: {result.tasks_submitted}\n"
            f"Cumulative calls/tokens/unknown: {result.usage}\n\n{markdown}"
        )


def _control(host: IncidentHost, args: argparse.Namespace) -> None:
    case = host.get_case(args.case_id)
    payload: Payload
    if args.action == "budget":
        payload = ExtendBudget(
            scope=case.scope,
            token_limit=args.token_limit,
            clear_token_limit=args.clear_token_limit,
            tokens=args.add_tokens,
            reason=args.reason,
            deadline=TimeWindow.model_validate({"start": args.deadline}).start,
        )
    elif args.action == "retry":
        payload = RetryTask(scope=case.scope, task_id=args.task_id, reason=args.reason)
    else:
        payload = Lifecycle(
            scope=case.scope,
            action=args.action,
            reason=args.reason,
            task_id=getattr(args, "task_id", None),
        )
    receipt = host.execute(
        Command(command_id=args.command_id or uuid4().hex, case_id=case.case_id, payload=payload)
    )
    _json(receipt.model_dump(mode="json"))
    if receipt.status != "accepted":
        raise typer.Exit(2)
