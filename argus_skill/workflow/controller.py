"""Single-owner portable policy loop. No process or model dispatcher is enabled."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Event
from typing import Callable

from ..core.file_lock import exclusive_file_lock
from .contract import WorkflowError, utc_deadline
from .jobs import JobQueue
from .reports import Reports
from .store import Ledger


class Controller:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.db = ledger.connection
        self.jobs = JobQueue(ledger)
        self.reports = Reports(ledger)

    def _tick(self, now: datetime) -> dict[str, object]:
        released: list[int] = []
        errors: list[dict[str, object]] = []
        current_revision, _contract = self.ledger.contract()
        with self.ledger._transaction():
            for row in self.db.execute(
                "SELECT * FROM rounds WHERE state NOT IN ('draft','paused','cancelled','completed')",
            ).fetchall():
                _revision, contract = self.ledger.contract(row["contract_revision"])
                stale = row["contract_revision"] != current_revision
                expired = contract.deadline_utc is not None and utc_deadline(contract.deadline_utc) <= now
                if stale or expired:
                    self.jobs._stop_round(row["id"], "contract changed" if stale else "deadline reached")
                    self.db.execute(
                        "UPDATE rounds SET paused_from = state, state = 'paused', revision = revision + 1 WHERE id = ?",
                        (row["id"],),
                    )
                    self.ledger._event(
                        "round.control_paused", {"expired": expired, "stale": stale}, round_id=row["id"],
                    )
        for annotation in self.db.execute(
            "SELECT * FROM annotations WHERE kind = 'approve' AND resolved_at IS NULL ORDER BY id LIMIT 50",
        ).fetchall():
            try:
                released.extend(self.reports.release(
                    annotation["report_id"], explicit=True, idea_id=annotation["idea_id"], now=now,
                ))
                self.reports.resolve(annotation["id"], "explicit operator approval processed")
            except WorkflowError as exc:
                errors.append({"annotation_id": annotation["id"], "error": str(exc)})
        for report in self.db.execute(
            """SELECT r.* FROM reports r JOIN rounds q ON q.id = r.round_id
               WHERE r.submitted_at IS NOT NULL AND r.channel IS NOT NULL
                 AND r.deadline <= ?
                 AND q.state IN ('reporting','comment_window','released')
                 AND EXISTS (
                    SELECT 1 FROM report_targets t JOIN ideas i ON i.id = t.idea_id
                    WHERE t.report_id = r.id AND i.state = 'pending'
                      AND r.id = (SELECT MAX(report_id) FROM report_targets WHERE idea_id = i.id)
                      AND NOT EXISTS (SELECT 1 FROM report_reissues WHERE idea_id = i.id)
                 )
               ORDER BY r.id LIMIT 50""", (now.astimezone(UTC).isoformat(),),
        ).fetchall():
            if utc_deadline(report["deadline"]) <= now:
                try:
                    released.extend(self.reports.release(report["id"], explicit=False, now=now))
                except WorkflowError as exc:
                    errors.append({"report_id": report["id"], "error": str(exc)})
        return {
            "released_ideas": released,
            "progress_checks_due": self.jobs.progress_due(now),
            "errors": errors,
            "recovery_required": [row["id"] for row in self.db.execute("SELECT id FROM jobs WHERE state = 'recovering'")],
            "held_ideas": [row["id"] for row in self.db.execute(
                "SELECT id FROM ideas WHERE state IN ('held','paused','terminated')",
            )],
            "execution_enabled": False,
            "notice": "dispatch intents are not executed until the Linux runtime and permission boundaries are verified",
        }

    def tick(self, *, now: datetime | None = None) -> dict[str, object]:
        with (self.ledger.root / "controller.lock").open("a+b") as handle:
            with exclusive_file_lock(handle, timeout_seconds=0, lock_name="workflow controller"):
                return self._tick(now or datetime.now(UTC))

    def run(self, stop: Event, emit: Callable[[dict[str, object]], None]) -> None:
        """Explicit user-started foreground loop; tests use tick, not a service."""
        with (self.ledger.root / "controller.lock").open("a+b") as handle:
            with exclusive_file_lock(handle, timeout_seconds=0, lock_name="workflow controller"):
                self.jobs.recover()
                previous: dict[str, object] | None = None
                while not stop.is_set():
                    result = self._tick(datetime.now(UTC))
                    if result != previous:
                        emit(result)
                        previous = result
                    stop.wait(1)
