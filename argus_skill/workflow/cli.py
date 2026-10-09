"""Operator CLI for the rebuild's preparation ledger; it executes no agents."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from ..core.secret_guard import redact_secrets_record, redact_secrets_text
from .contract import ResearchContract, WorkflowError, integer, object_fields, text
from .store import DATABASE_NAME, Ledger


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Independent research-workflow preparation ledger. "
            "Agent execution and unattended automation are not enabled."
        ),
    )
    parser.add_argument("--state", required=True, type=Path, help="new workflow state directory")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create state without importing legacy projects")
    source = init.add_mutually_exclusive_group(required=True)
    source.add_argument("--objective", help="operator-owned research objective")
    source.add_argument("--contract", type=Path, help="complete or partial contract JSON")

    commands.add_parser("status", help="show declarations, blockers and preparation state")
    commands.add_parser("migrate", help="explicitly upgrade this workflow's version-one database")
    contracts = commands.add_parser("contract").add_subparsers(dest="action", required=True)
    contracts.add_parser("show", help="write the current contract JSON to stdout")
    revise = contracts.add_parser("revise", help="explicitly replace the operator contract")
    revise.add_argument("--file", required=True, type=Path)
    revise.add_argument("--expected-revision", required=True, type=int)

    rounds = commands.add_parser("round").add_subparsers(dest="action", required=True)
    rounds.add_parser("create", help="bind a new preparation round to the current contract")
    for action in ("status", "prepare", "pause", "resume", "cancel", "rebind"):
        command = rounds.add_parser(action)
        command.add_argument("round_id", type=int)
        if action != "status":
            command.add_argument("--expected-revision", required=True, type=int)

    directives = commands.add_parser(
        "directive", help="scoped standing rules, not one-off execution requests",
    ).add_subparsers(dest="action", required=True)
    add = directives.add_parser("add")
    add.add_argument("--text", required=True)
    add.add_argument("--role", default="all", help="declared role ID, or all")
    add.add_argument("--round", type=int, dest="round_id")
    add.add_argument("--lifetime", choices=("standing", "once", "bounded"), default="standing")
    revoke = directives.add_parser("revoke")
    revoke.add_argument("directive_id", type=int)
    show = directives.add_parser("show")
    show.add_argument("--round", required=True, type=int, dest="round_id")
    show.add_argument("--role", required=True)

    events = commands.add_parser("events", help="page through persisted audit events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)
    backup = commands.add_parser("backup", help="create a consistent SQLite backup")
    backup.add_argument("--output", required=True, type=Path)
    restore = commands.add_parser("restore", help="restore into an empty state directory")
    restore.add_argument("--backup", required=True, type=Path)

    discussion = commands.add_parser(
        "discussion", help="operator-imported protocol records; not live Agent execution",
    ).add_subparsers(dest="action", required=True)
    for action in ("begin", "advance"):
        discussion.add_parser(action).add_argument("round_id", type=int)
    submit = discussion.add_parser("submit")
    submit.add_argument("round_id", type=int)
    submit.add_argument("--role", required=True)
    submit.add_argument("--file", required=True, type=Path)
    for action in ("review", "rebut"):
        command = discussion.add_parser(action)
        command.add_argument("idea_id", type=int)
        command.add_argument("--role", required=True)
        command.add_argument("--file", required=True, type=Path)
        command.add_argument("--expected-version", required=True, type=int)
    for action in ("context", "prompt"):
        context = discussion.add_parser(action)
        context.add_argument("round_id", type=int)
        context.add_argument("--role", required=True)
        context.add_argument("--idea", type=int, dest="idea_id")

    reports = commands.add_parser("report").add_subparsers(dest="action", required=True)
    reports.add_parser("generate").add_argument("round_id", type=int)
    show_report = reports.add_parser("show")
    show_report.add_argument("report_id", type=int)
    reports.add_parser("repair-draft").add_argument("report_id", type=int)
    for action in ("deliver", "release", "annotate"):
        command = reports.add_parser(action)
        command.add_argument("report_id", type=int)
        if action == "deliver":
            command.add_argument("--channel", required=True, help="operator-agreed CLI delivery channel")
        else:
            command.add_argument("--idea", type=int, dest="idea_id")
        if action == "annotate":
            command.add_argument("--kind", required=True, choices=("comment", "pause", "reject", "approve"))
            command.add_argument("--text", required=True)
    resolve = reports.add_parser("resolve")
    resolve.add_argument("annotation_id", type=int)
    resolve.add_argument("--response", required=True)

    tasks = commands.add_parser("task").add_subparsers(dest="action", required=True)
    tasks.add_parser("list")
    tasks.add_parser("assign").add_argument("--file", type=Path, required=True)
    tasks.add_parser("research").add_argument("--file", type=Path, required=True)
    confirm = tasks.add_parser("confirm")
    confirm.add_argument("idea_id", type=int)
    confirm.add_argument("--expert", required=True)
    confirm.add_argument("--engineer", required=True)
    confirm.add_argument("--recap", required=True)
    reserve = tasks.add_parser("reserve")
    reserve.add_argument("task_id", type=int)
    reserve.add_argument("--usd", required=True, help="positive conservative request reservation")
    reserve.add_argument("--kind", choices=("production", "evaluation"), default="production")
    reserve.add_argument("--evaluator")
    decide = tasks.add_parser("decide")
    decide.add_argument("task_id", type=int)
    decide.add_argument("--manager", required=True)
    decide.add_argument("--decision", required=True, choices=("accept", "continue", "stop"))
    decide.add_argument("--reason", required=True)

    jobs = commands.add_parser("job").add_subparsers(dest="action", required=True)
    jobs.add_parser("list")
    jobs.add_parser("context").add_argument("job_id", type=int)
    stop = jobs.add_parser("stop")
    stop.add_argument("job_id", type=int)
    stop.add_argument("--reason", required=True)
    controllers = commands.add_parser("controller").add_subparsers(dest="action", required=True)
    for action in ("tick", "run", "recover"):
        controllers.add_parser(action)
    return parser


def _read_contract(path: Path) -> ResearchContract:
    return ResearchContract.from_value(_read_json(path))


def _read_json(path: Path) -> object:
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def _print(value: object) -> None:
    print(json.dumps(redact_secrets_record(value), ensure_ascii=False, indent=2, allow_nan=False))
    sys.stdout.flush()


def _operate(args: argparse.Namespace, ledger: Ledger) -> object:
    if args.command in {"status", "migrate"}:
        return ledger.status()
    if args.command == "contract":
        if args.action == "show":
            return ledger.contract()[1].to_dict()
        return {
            "contract_revision": ledger.revise_contract(
                _read_contract(args.file), expected_revision=args.expected_revision,
            ),
        }
    if args.command == "round":
        if args.action == "create":
            return ledger.round_status(ledger.create_round())
        if args.action == "status":
            return ledger.round_status(args.round_id)
        if args.action == "rebind":
            ledger.rebind_round(args.round_id, expected_revision=args.expected_revision)
            return ledger.round_status(args.round_id)
        return ledger.transition(
            args.round_id, args.action, expected_revision=args.expected_revision,
        )
    if args.command == "directive":
        if args.action == "add":
            return {"directive_id": ledger.add_directive(
                args.text, role_id=args.role, round_id=args.round_id,
                lifetime=args.lifetime,
            )}
        if args.action == "revoke":
            ledger.revoke_directive(args.directive_id)
            return {"directive_id": args.directive_id, "state": "revoked"}
        return ledger.directives(args.round_id, args.role)
    if args.command == "events":
        return ledger.events(after=args.after, limit=args.limit)
    if args.command == "backup":
        return {"backup": str(ledger.backup(args.output))}
    if args.command == "discussion":
        from .discussion import Discussion
        from .protocol import Proposal, Review

        discussion = Discussion(ledger)
        if args.action == "begin":
            discussion.begin(args.round_id)
            return ledger.round_status(args.round_id)
        if args.action == "advance":
            return {"phase": discussion.advance(args.round_id)}
        if args.action == "context":
            return discussion.view(args.round_id, args.role, idea_id=args.idea_id)
        if args.action == "prompt":
            return discussion.prompt(args.round_id, args.role, idea_id=args.idea_id)
        body = _read_json(args.file)
        if args.action == "submit":
            return {"idea_id": discussion.propose(args.round_id, args.role, Proposal.from_value(body))}
        if args.action == "review":
            discussion.review(args.idea_id, args.role, Review.from_value(body), expected_version=args.expected_version)
        else:
            payload = object_fields(body, {"response", "changes", "proposal"}, "rebuttal")
            proposal = payload.get("proposal")
            discussion.rebut(
                args.idea_id, args.role, text(payload.get("response"), "response"),
                text(payload.get("changes"), "changes"), expected_version=args.expected_version,
                proposal=None if proposal is None else Proposal.from_value(proposal),
            )
        return {"idea_id": args.idea_id, "submission_locked": True}
    if args.command == "report":
        from .reports import Reports

        reports = Reports(ledger)
        if args.action == "generate":
            return reports.generate(args.round_id)
        if args.action == "show":
            return dict(reports.report(args.report_id))
        if args.action == "repair-draft":
            reports.repair_draft(args.report_id)
            return {"report_id": args.report_id, "state": "draft files restored; no notification or window started"}
        if args.action == "deliver":
            return reports.deliver(args.report_id, _print, channel=args.channel)
        if args.action == "release":
            return {"released_ideas": reports.release(args.report_id, explicit=True, idea_id=args.idea_id)}
        if args.action == "annotate":
            return {"annotation_id": reports.annotate(
                args.report_id, args.kind, args.text, idea_id=args.idea_id,
            )}
        reports.resolve(args.annotation_id, args.response)
        return {"annotation_id": args.annotation_id, "state": "resolved"}
    if args.command in {"task", "job"}:
        from .jobs import JobQueue

        queue = JobQueue(ledger)
        if args.action == "list":
            return queue.inspect()
        if args.command == "job":
            if args.action == "context":
                return queue.request_context(args.job_id)
            queue.stop(args.job_id, args.reason)
            return dict(queue.job(args.job_id))
        if args.action == "assign":
            payload = object_fields(
                _read_json(args.file),
                {"idea_id", "manager", "engineer", "title", "objective", "workspace", "dependencies", "evaluation_rule"},
                "assignment",
            )
            raw_dependencies = payload.get("dependencies", [])
            if not isinstance(raw_dependencies, list):
                raise WorkflowError("dependencies must be an array of task IDs")
            return {"task_id": queue.assign(
                integer(payload.get("idea_id"), "idea_id"), manager=text(payload.get("manager"), "manager"),
                engineer=text(payload.get("engineer"), "engineer"), title=text(payload.get("title"), "title"),
                objective=text(payload.get("objective"), "objective"),
                workspace=Path(text(payload.get("workspace"), "workspace")),
                dependencies=tuple(integer(value, "dependency") for value in raw_dependencies),
                evaluation_rule=payload.get("evaluation_rule"),
            )}
        if args.action == "research":
            payload = object_fields(
                _read_json(args.file),
                {"round_id", "coordinator", "worker", "title", "objective", "workspace", "dependencies"},
                "research assignment",
            )
            raw_dependencies = payload.get("dependencies", [])
            if not isinstance(raw_dependencies, list):
                raise WorkflowError("dependencies must be an array")
            return {"task_id": queue.assign_preparation(
                integer(payload.get("round_id"), "round_id"), coordinator=text(payload.get("coordinator"), "coordinator"),
                worker=text(payload.get("worker"), "worker"), title=text(payload.get("title"), "title"),
                objective=text(payload.get("objective"), "objective"), workspace=Path(text(payload.get("workspace"), "workspace")),
                dependencies=tuple(integer(value, "dependency") for value in raw_dependencies),
            )}
        if args.action == "confirm":
            queue.confirm(args.idea_id, args.expert, args.engineer, args.recap)
            return {"idea_id": args.idea_id, "confirmation_recorded": True}
        if args.action == "reserve":
            return {"job_id": queue.reserve(args.task_id, args.usd, kind=args.kind, evaluator=args.evaluator),
                    "execution_enabled": False}
        queue.decide(args.task_id, args.manager, args.decision, args.reason)
        return dict(queue.task(args.task_id))
    if args.command == "controller":
        from threading import Event

        from .controller import Controller

        controller = Controller(ledger)
        if args.action == "tick":
            return controller.tick()
        if args.action == "recover":
            return {"recovery_required": controller.jobs.recover()}
        stop = Event()
        try:
            controller.run(stop, _print)
        except KeyboardInterrupt:
            stop.set()
        return {"controller": "stopped", "notice": "foreground loop stopped; job termination is a separate request"}
    raise WorkflowError(f"unsupported ledger operation: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            contract = (
                _read_contract(args.contract) if args.contract
                else ResearchContract.from_value({"objective": args.objective})
            )
            ledger = Ledger.create(args.state, contract)
        elif args.command == "restore":
            ledger = Ledger.restore(args.backup, args.state)
        else:
            if args.command == "migrate":
                Ledger.migrate(args.state / DATABASE_NAME)
            ledger = Ledger(
                args.state / DATABASE_NAME,
                readonly=(
                    args.command in {"status", "events", "backup"}
                    or args.command in {"contract", "round", "directive"}
                    and args.action in {"show", "status"}
                    or args.command == "discussion" and args.action in {"context", "prompt"}
                    or args.command == "report" and args.action == "show"
                    or args.command in {"task", "job"} and args.action in {"list", "context"}
                ),
            )
        with ledger:
            result = ledger.status() if args.command in {"init", "restore"} else _operate(args, ledger)
        _print(result)
    except (WorkflowError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"workflow error: {redact_secrets_text(str(exc))}", file=sys.stderr)
        return 2
    return 0
