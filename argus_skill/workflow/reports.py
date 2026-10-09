"""Versioned dual reports, durable annotations and bounded policy release."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from ..core.file_lock import exclusive_file_lock
from .contract import WorkflowError, text, utc_deadline
from .discussion import Discussion
from .store import Ledger, _timestamp


class Reports:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.db = ledger.connection
        self.discussion = Discussion(ledger)

    def report(self, report_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"report not found: {report_id}")
        return row

    def latest(self, round_id: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM reports WHERE round_id = ? ORDER BY version DESC LIMIT 1", (round_id,),
        ).fetchone()
        if row is None:
            raise WorkflowError("round has no reports")
        return row

    def verify_files(self, report_id: int) -> None:
        report = self.report(report_id)
        for kind in ("long", "short"):
            path = Path(report[f"{kind}_path"])
            if path.is_symlink() or not path.is_file():
                raise WorkflowError(f"{kind} report is not a regular accessible file")
            if path.read_text(encoding="utf-8") != report[f"{kind}_body"]:
                raise WorkflowError(f"{kind} report differs from its published version")

    def generate(self, round_id: int) -> dict[str, object]:
        self.discussion.context(round_id, "reporting")
        pending = self.db.execute(
            "SELECT * FROM reports WHERE round_id = ? ORDER BY version DESC LIMIT 1", (round_id,),
        ).fetchone()
        if pending is not None and pending["submitted_at"] is None:
            self.repair_draft(pending["id"])
            return {
                "report_id": pending["id"], "version": pending["version"],
                "long_path": pending["long_path"], "short_path": pending["short_path"],
            }
        with self.ledger._transaction():
            row, contract = self.discussion.context(round_id, "reporting")
            version = self.db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM reports WHERE round_id = ?", (round_id,),
            ).fetchone()[0]
            long_parts = [
                f"# Research round {round_id}, report {version}",
                "These are operator-imported protocol records, not verified live-agent or experiment results.",
                "Scores recommend experiments; they do not establish performance improvement.",
                "## Frozen contract", "```json", json.dumps(contract.to_dict(), ensure_ascii=False, indent=2), "```",
            ]
            short_parts = [
                f"# Round {round_id}: decision summary, report {version}", contract.objective,
                "Expected benefits are unmeasured. Raw inputs and sources still require verification.",
            ]
            ideas = self.db.execute("SELECT * FROM ideas WHERE round_id = ?", (round_id,)).fetchall()
            targets = {item["idea_id"] for item in self.db.execute(
                """SELECT r.idea_id FROM report_reissues r JOIN ideas i ON i.id = r.idea_id
                   WHERE i.round_id = ?""", (round_id,),
            )}
            if version == 1:
                targets = {idea["id"] for idea in ideas}
            elif not targets:
                raise WorkflowError("no affected scopes require a revised report")
            kinds = {role.role_id: role.kind for role in contract.roles or ()}
            ideas.sort(key=lambda idea: (kinds[idea["owner"]] == "co-optimization-expert", idea["owner"], idea["id"]))
            for idea in ideas:
                if not self.discussion.reviews_complete(idea["id"], row["review_cycle"]):
                    raise WorkflowError("cannot report incomplete final reviews")
                summary = self.discussion.summary(idea["id"], row["review_cycle"])
                if summary["blocking_issues"]:
                    self.db.execute(
                        "UPDATE ideas SET state = 'held' WHERE id = ? AND state = 'pending'", (idea["id"],),
                    )
                proposal = self.db.execute(
                    "SELECT body FROM proposals WHERE idea_id = ? AND version = ?", (idea["id"], idea["version"]),
                ).fetchone()
                body = json.loads(proposal["body"])
                long_parts.extend([
                    f"## Idea {idea['id']}, proposal {idea['version']}: {body['title']}",
                    "```json", json.dumps(body, ensure_ascii=False, indent=2), "```",
                    "### Final recommendation", "```json",
                    json.dumps(summary, ensure_ascii=False, indent=2), "```",
                ])
                for table in ("proposals", "reviews", "rebuttals"):
                    records = [dict(item) for item in self.db.execute(
                        f"SELECT * FROM {table} WHERE idea_id = ?", (idea["id"],),
                    )]
                    long_parts.extend([
                        f"### {table} history", "```json", json.dumps(records, ensure_ascii=False, indent=2), "```",
                    ])
                mean = summary["mean"]
                assert isinstance(mean, float)
                short_parts.append(
                    f"- Idea {idea['id']}: {body['title']}; recommendation {mean:.1f}/5; "
                    f"disagreement={summary['disagreement']}; blockers={summary['blocking_issues']}; "
                    f"expected={body['expected_benefit']}; risk={body['uncertainties']}; resources={body['resources']}"
                )
            annotations = [dict(item) for item in self.db.execute(
                """SELECT a.* FROM annotations a JOIN reports r ON r.id = a.report_id
                   WHERE r.round_id = ? ORDER BY a.id""", (round_id,),
            )]
            long_parts.extend([
                "## User annotations and resolutions", "```json",
                json.dumps(annotations, ensure_ascii=False, indent=2), "```",
            ])
            directory = self.ledger.root / "reports" / f"round-{round_id}" / f"version-{version}"
            long_path, short_path = directory / "long.md", directory / "short.md"
            short_parts.extend([
                f"Long report: {long_path}",
                "Comment window has not started: both reports and successful notification are required.",
                "After successful delivery, 20 minutes without applicable comments releases only authorized directions.",
            ])
            long_body, short_body = "\n\n".join(long_parts) + "\n", "\n\n".join(short_parts) + "\n"
            assert contract.storage_limits is not None
            if len(long_body.encode("utf-8")) + len(short_body.encode("utf-8")) > contract.storage_limits.archive_bytes:
                raise WorkflowError("report exceeds the declared archive allowance")
            cursor = self.db.execute(
                """INSERT INTO reports(round_id, version, long_body, short_body, long_path, short_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (round_id, version, long_body, short_body, str(long_path), str(short_path)),
            )
            report_id = cursor.lastrowid
            assert report_id is not None
            for idea in ideas:
                if idea["id"] in targets:
                    self.db.execute(
                        "INSERT INTO report_targets VALUES (?, ?, ?)",
                        (report_id, idea["id"], idea["version"]),
                    )
                    self.db.execute("DELETE FROM report_reissues WHERE idea_id = ?", (idea["id"],))
            self.ledger._event("report.generated", {"report_id": report_id, "version": version}, round_id=round_id)
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for path, body in ((long_path, long_body), (short_path, short_body)):
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(body)
        self.verify_files(report_id)
        return {"report_id": report_id, "version": version, "long_path": str(long_path), "short_path": str(short_path)}

    def repair_draft(self, report_id: int) -> None:
        report = self.report(report_id)
        if report["submitted_at"] is not None:
            raise WorkflowError("published reports cannot be repaired or rewritten in place")
        directory = self.ledger.root / "reports" / f"round-{report['round_id']}" / f"version-{report['version']}"
        resolved = directory.resolve()
        if self.ledger.root not in resolved.parents:
            raise WorkflowError("report directory escapes the control-owned archive")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for kind in ("long", "short"):
            path = directory / f"{kind}.md"
            if path.is_symlink():
                raise WorkflowError("draft report path is a symbolic link")
            path.write_text(report[f"{kind}_body"], encoding="utf-8", newline="\n")
        self.verify_files(report_id)

    def deliver(
        self, report_id: int, notify: Callable[[str], None], *, channel: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        channel = text(channel, "delivery channel")
        lock = self.ledger.root / f"report-{report_id}.delivery.lock"
        with lock.open("a+b") as handle, exclusive_file_lock(handle, timeout_seconds=0, lock_name="report delivery"):
            report = self.report(report_id)
            self.verify_files(report_id)
            if report["submitted_at"] is not None:
                if report["channel"] is None:
                    notify(f"Restored reports: {report['long_path']}\n{report['short_path']}\nOriginal deadline: {report['deadline']}")
                    with self.ledger._transaction():
                        self.db.execute("UPDATE reports SET channel = ? WHERE id = ?", (channel, report_id))
                        self.ledger._event("report.redelivered", {"report_id": report_id}, round_id=report["round_id"])
                return {"report_id": report_id, "submitted_at": report["submitted_at"], "deadline": report["deadline"]}
            row, _contract = self.discussion.context(report["round_id"], "reporting")
            if self.latest(report["round_id"])["id"] != report_id:
                raise WorkflowError("cannot deliver a superseded report")
            notify(f"Reports ready: {report['long_path']}\n{report['short_path']}\n20-minute policy window follows delivery.")
            submitted = now or datetime.now(UTC)
            deadline = submitted + timedelta(minutes=20)
            short_body = report["short_body"] + (
                f"\nWindow starts: {submitted.isoformat()}\nDeadline: {deadline.isoformat()}\n"
                "No applicable comments within this window triggers TIMEOUT_AUTO_RELEASE, not user endorsement.\n"
            )
            Path(report["short_path"]).write_text(short_body, encoding="utf-8", newline="\n")
            with self.ledger._transaction():
                fresh, _contract = self.discussion.context(report["round_id"], "reporting")
                if fresh["revision"] != row["revision"]:
                    raise WorkflowError("round changed during report delivery")
                self.db.execute(
                    """UPDATE reports SET short_body = ?, submitted_at = ?, deadline = ?, channel = ? WHERE id = ?""",
                    (short_body, submitted.isoformat(), deadline.isoformat(), channel, report_id),
                )
                self.discussion._state(report["round_id"], "comment_window")
                self.ledger._event("report.delivered", {"report_id": report_id, "channel": channel}, round_id=report["round_id"])
        return {"report_id": report_id, "submitted_at": submitted.isoformat(), "deadline": deadline.isoformat()}

    def annotate(self, report_id: int, kind: str, body: str, *, idea_id: int | None = None) -> int:
        body = text(body, "annotation")
        if kind not in {"comment", "pause", "reject", "approve"}:
            raise WorkflowError("unknown annotation kind")
        with self.ledger._transaction():
            report = self.report(report_id)
            row, _contract = self.discussion.context(report["round_id"])
            if report["submitted_at"] is None:
                raise WorkflowError("annotation must reference the current delivered report")
            delivered = self.db.execute(
                """SELECT MAX(id) FROM reports WHERE round_id = ? AND submitted_at IS NOT NULL""",
                (report["round_id"],),
            ).fetchone()[0]
            if idea_id is None and delivered != report_id:
                raise WorkflowError("global annotation must reference the latest report")
            if idea_id is not None and self.db.execute(
                """SELECT MAX(t.report_id) FROM report_targets t JOIN reports r ON r.id = t.report_id
                   WHERE t.idea_id = ? AND r.submitted_at IS NOT NULL""", (idea_id,),
            ).fetchone()[0] != report_id:
                raise WorkflowError("annotation references an outdated report for this idea")
            if idea_id is not None and self.discussion.idea(idea_id)["round_id"] != report["round_id"]:
                raise WorkflowError("annotation idea is outside this round")
            cursor = self.db.execute(
                "INSERT INTO annotations(report_id, idea_id, kind, body, received_at) VALUES (?, ?, ?, ?, ?)",
                (report_id, idea_id, kind, body, _timestamp()),
            )
            annotation_id = cursor.lastrowid
            assert annotation_id is not None
            if kind != "approve":
                ids = [idea_id] if idea_id is not None else [
                    item["id"] for item in self.db.execute("SELECT id FROM ideas WHERE round_id = ?", (row["id"],))
                ]
                from .jobs import JobQueue

                for target in ids:
                    assert target is not None
                    self.db.execute(
                        "UPDATE ideas SET state = ? WHERE id = ? AND state NOT IN ('terminated', 'accepted')",
                        ("rejected" if kind == "reject" else "held", target),
                    )
                    JobQueue(self.ledger)._stop_idea(target, "user annotation")
            self.ledger._event("annotation.received", {"annotation_id": annotation_id, "kind": kind}, round_id=row["id"])
            return annotation_id

    def resolve(self, annotation_id: int, response: str) -> None:
        response = text(response, "resolution")
        with self.ledger._transaction():
            annotation = self.db.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
            if annotation is None or annotation["resolved_at"] is not None:
                raise WorkflowError("annotation is missing or already resolved")
            if annotation["kind"] in {"pause", "reject"}:
                raise WorkflowError("pause/rejection requires an explicit operator approval, not timed resolution")
            report = self.report(annotation["report_id"])
            self.db.execute(
                "UPDATE annotations SET resolved_at = ?, resolution = ? WHERE id = ?",
                (_timestamp(), response, annotation_id),
            )
            if annotation["kind"] == "comment":
                self.discussion._state(report["round_id"], "reporting")
                self.db.execute(
                    """UPDATE ideas SET state = 'pending' WHERE round_id = ?
                       AND state = 'held' AND (? IS NULL OR id = ?)""",
                    (report["round_id"], annotation["idea_id"], annotation["idea_id"]),
                )
                self.db.execute(
                    """INSERT OR IGNORE INTO report_reissues(idea_id)
                       SELECT id FROM ideas WHERE round_id = ? AND (? IS NULL OR id = ?)""",
                    (report["round_id"], annotation["idea_id"], annotation["idea_id"]),
                )
            self.ledger._event("annotation.resolved", {"annotation_id": annotation_id}, round_id=report["round_id"])

    def release(self, report_id: int, *, explicit: bool, idea_id: int | None = None, now: datetime | None = None) -> list[int]:
        with self.ledger._transaction():
            report = self.report(report_id)
            row, contract = self.discussion.context(report["round_id"])
            if row["state"] not in {"reporting", "comment_window", "released"} or report["submitted_at"] is None:
                raise WorkflowError("report is not in a releasable window")
            if report["channel"] is None:
                raise WorkflowError("report notification must be re-established after restore")
            self.verify_files(report_id)
            current = now or datetime.now(UTC)
            if contract.preparation_blockers(current):
                raise WorkflowError("authorization is incomplete or expired")
            if not explicit and utc_deadline(report["deadline"]) > current:
                return []
            ideas = self.db.execute(
                """SELECT i.* FROM ideas i JOIN report_targets t ON t.idea_id = i.id
                   WHERE t.report_id = ? AND t.proposal_version = i.version""", (report_id,),
            ).fetchall()
            if idea_id is not None:
                if not any(idea["id"] == idea_id for idea in ideas):
                    raise WorkflowError("release idea is outside this round")
                ideas = [idea for idea in ideas if idea["id"] == idea_id]
            released: list[int] = []
            for idea in ideas:
                if idea["state"] in {"terminated", "accepted", "paused"}:
                    continue
                if self.db.execute(
                    "SELECT 1 FROM report_reissues WHERE idea_id = ?", (idea["id"],),
                ).fetchone() is not None:
                    continue
                current_report = self.db.execute(
                    "SELECT MAX(report_id) FROM report_targets WHERE idea_id = ?", (idea["id"],),
                ).fetchone()[0]
                if current_report != report_id:
                    continue
                annotations = self.db.execute(
                    """SELECT * FROM annotations WHERE resolved_at IS NULL
                       AND (idea_id IS NULL OR idea_id = ?) AND report_id IN
                         (SELECT id FROM reports WHERE round_id = ?)""", (idea["id"], row["id"]),
                ).fetchall()
                blockers = [item for item in annotations if item["kind"] != "approve"]
                if blockers and not explicit:
                    continue
                summary = self.discussion.summary(idea["id"], row["review_cycle"])
                if not summary["complete"] or summary["blocking_issues"]:
                    continue
                if explicit and idea["state"] == "released":
                    continue
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO releases(report_id, idea_id, proposal_version, mode, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (report_id, idea["id"], idea["version"],
                     "EXPLICIT_RELEASE" if explicit else "TIMEOUT_AUTO_RELEASE", current.isoformat()),
                )
                if cursor.rowcount:
                    released.append(idea["id"])
                    self.db.execute("UPDATE ideas SET state = 'released' WHERE id = ?", (idea["id"],))
                    self.ledger._event(
                        "idea.released", {"idea_id": idea["id"], "explicit": explicit}, round_id=row["id"],
                    )
            if released and row["state"] != "reporting":
                self.discussion._state(row["id"], "released")
            return released
