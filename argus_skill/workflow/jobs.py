"""Transactional assignments, conservative reservations and runner-only receipts."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .contract import (
    ResearchContract,
    WorkflowError,
    integer,
    object_fields,
    text,
    text_list,
    utc_deadline,
)
from .discussion import ROLE_DUTIES, Discussion
from .protocol import USD_UNITS, Measurement, money_units, number
from .store import Ledger, _json, _timestamp

ACTIVE_STATES = ("reserved", "starting", "running", "recovering", "stop_requested")


class JobQueue:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.db = ledger.connection
        self.discussion = Discussion(ledger)

    def task(self, task_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (integer(task_id, "task_id"),)).fetchone()
        if row is None:
            raise WorkflowError(f"task not found: {task_id}")
        return row

    def job(self, job_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (integer(job_id, "job_id"),)).fetchone()
        if row is None:
            raise WorkflowError(f"job not found: {job_id}")
        return row

    def _eligible(self, idea_id: int) -> tuple[sqlite3.Row, sqlite3.Row, ResearchContract]:
        idea = self.discussion.idea(idea_id)
        row, contract = self.discussion.context(idea["round_id"])
        if row["state"] not in {"released", "reporting", "comment_window"} or idea["state"] != "released":
            raise WorkflowError("idea is not released for manager assignment")
        if contract.preparation_blockers(datetime.now(UTC)):
            raise WorkflowError("authorization is incomplete or expired")
        if self.db.execute(
            "SELECT 1 FROM releases WHERE idea_id = ? AND proposal_version = ?",
            (idea_id, idea["version"]),
        ).fetchone() is None:
            raise WorkflowError("proposal version has no release")
        return idea, row, contract

    def _task_context(self, task: sqlite3.Row) -> tuple[sqlite3.Row | None, sqlite3.Row, ResearchContract]:
        if task["phase"] == "execution":
            return self._eligible(task["idea_id"])
        row, contract = self.discussion.context(task["round_id"], "prepared")
        blockers = contract.research_blockers(datetime.now(UTC))
        if blockers:
            raise WorkflowError("research authorization is incomplete: " + "; ".join(blockers))
        return None, row, contract

    def confirm(self, idea_id: int, expert: str, engineer: str, recap: str) -> None:
        recap = text(recap, "engineer recap")
        with self.ledger._transaction():
            idea, row, contract = self._eligible(idea_id)
            self.discussion.role(contract, expert, {"expert", "co-optimization-expert"})
            self.discussion.role(contract, engineer, {"engineer", "system-engineer"})
            if expert != idea["owner"]:
                raise WorkflowError("technical confirmation requires the owning expert")
            self.db.execute(
                "INSERT INTO confirmations VALUES (?, ?, ?, ?, ?, ?)",
                (idea_id, idea["version"], engineer, expert, recap, _timestamp()),
            )
            self.ledger._event("idea.confirmed", {"idea_id": idea_id, "engineer": engineer}, round_id=row["id"])

    def assign(
        self, idea_id: int, *, manager: str, engineer: str, title: str, objective: str,
        workspace: Path, dependencies: tuple[int, ...] = (), evaluation_rule: object = None,
    ) -> int:
        title, objective = text(title, "title"), text(objective, "objective")
        workspace = workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir() or workspace == self.ledger.root or self.ledger.root in workspace.parents:
            raise WorkflowError("task workspace must be an existing directory outside control state")
        rule = None
        if evaluation_rule is not None:
            rule = object_fields(
                evaluation_rule, {"direction", "required_observations", "noise_limit", "regression_limit"},
                "evaluation_rule",
            )
            if rule.get("direction") not in {"minimize", "maximize"}:
                raise WorkflowError("evaluation rule needs minimize/maximize direction")
            integer(rule.get("required_observations"), "required_observations")
            number(rule.get("noise_limit"), "noise_limit")
            number(rule.get("regression_limit"), "regression_limit")
        with self.ledger._transaction():
            idea, row, contract = self._eligible(idea_id)
            self.discussion.role(contract, manager, {"project-manager"})
            self.discussion.role(contract, engineer, {"engineer", "system-engineer"})
            for dependency in dependencies:
                prior = self.task(dependency)
                if prior["round_id"] != row["id"]:
                    raise WorkflowError("dependency belongs to another research round")
            cursor = self.db.execute(
                """INSERT INTO tasks(round_id, phase, idea_id, proposal_version, title, objective, role_id, workspace, evaluation_rule)
                   VALUES (?, 'execution', ?, ?, ?, ?, ?, ?, ?)""",
                (row["id"], idea_id, idea["version"], title, objective, engineer, str(workspace),
                 None if rule is None else _json(rule)),
            )
            task_id = cursor.lastrowid
            assert task_id is not None
            for dependency in dict.fromkeys(dependencies):
                self.db.execute("INSERT INTO dependencies VALUES (?, ?)", (task_id, dependency))
            self.ledger._event("task.assigned", {"task_id": task_id, "manager": manager}, round_id=row["id"])
            return task_id

    def assign_preparation(
        self, round_id: int, *, coordinator: str, worker: str, title: str, objective: str,
        workspace: Path, dependencies: tuple[int, ...] = (),
    ) -> int:
        title, objective = text(title, "title"), text(objective, "objective")
        workspace = workspace.expanduser().resolve(strict=True)
        if not workspace.is_dir() or workspace == self.ledger.root or self.ledger.root in workspace.parents:
            raise WorkflowError("research workspace must be outside control state")
        with self.ledger._transaction():
            row, contract = self.discussion.context(round_id, "prepared")
            if contract.research_blockers(datetime.now(UTC)):
                raise WorkflowError("research budget, data policy or deadline is incomplete")
            self.discussion.role(contract, coordinator, {"moderator", "project-manager"})
            self.discussion.role(contract, worker, {"researcher", "distiller"})
            for dependency in dependencies:
                if self.task(dependency)["round_id"] != round_id:
                    raise WorkflowError("dependency belongs to another research round")
            cursor = self.db.execute(
                """INSERT INTO tasks(round_id, phase, title, objective, role_id, workspace)
                   VALUES (?, 'preparation', ?, ?, ?, ?)""",
                (round_id, title, objective, worker, str(workspace)),
            )
            task_id = cursor.lastrowid
            assert task_id is not None
            for dependency in dict.fromkeys(dependencies):
                self.db.execute("INSERT INTO dependencies VALUES (?, ?)", (task_id, dependency))
            self.ledger._event("research.assigned", {"task_id": task_id, "coordinator": coordinator}, round_id=round_id)
            return task_id

    def budget(self) -> dict[str, object]:
        _revision, contract = self.ledger.contract()
        used = self.db.execute(
            """SELECT COALESCE(SUM(CASE WHEN usage_known = 1 THEN actual_usd_units
                 ELSE reserved_usd_units END), 0) FROM jobs""",
        ).fetchone()[0]
        allowance = None if contract.total_model_budget_usd is None else money_units(
            contract.total_model_budget_usd, allowance=True,
        )
        return {
            "allowance_usd": None if allowance is None else allowance / USD_UNITS,
            "spent_or_reserved_usd": used / USD_UNITS,
            "available_usd": None if allowance is None else (allowance - used) / USD_UNITS,
            "unknown_usage_jobs": [row["id"] for row in self.db.execute(
                "SELECT id FROM jobs WHERE usage_known = 0 AND state IN ('stopped', 'completed', 'failed')",
            )],
        }

    def reserve(self, task_id: int, amount: object, *, kind: str = "production", evaluator: str | None = None) -> int:
        units = money_units(amount)
        if units == 0:
            raise WorkflowError("a request requires a positive conservative reservation")
        if kind not in {"production", "evaluation"}:
            raise WorkflowError("unknown job kind")
        with self.ledger._transaction():
            if self.db.execute("SELECT 1 FROM jobs WHERE state = 'recovering' LIMIT 1").fetchone() is not None:
                raise WorkflowError("startup reconciliation is incomplete; new reservations are blocked")
            task = self.task(task_id)
            idea, row, contract = self._task_context(task)
            if idea is not None and task["proposal_version"] != idea["version"]:
                raise WorkflowError("task proposal version is stale")
            producer = None
            role_id = task["role_id"]
            if kind == "production":
                if task["state"] != "queued":
                    raise WorkflowError("task is not queued")
                if idea is not None and self.db.execute(
                    "SELECT 1 FROM confirmations WHERE idea_id = ? AND proposal_version = ? AND engineer = ?",
                    (idea["id"], idea["version"], role_id),
                ).fetchone() is None:
                    raise WorkflowError("expert/engineer confirmation is missing")
                if self.db.execute(
                    """SELECT 1 FROM dependencies d JOIN tasks t ON t.id = d.depends_on
                       WHERE d.task_id = ? AND t.state != 'accepted'""", (task_id,),
                ).fetchone() is not None:
                    raise WorkflowError("task dependencies have not been accepted")
            else:
                if task["state"] != "evaluating" or task["phase"] == "execution" and task["evaluation_rule"] is None:
                    raise WorkflowError("task lacks a completed producer or frozen evaluation rule")
                role_id = text(evaluator, "evaluator")
                self.discussion.role(
                    contract, role_id, {"supervisor"} if idea is not None else {"reviewer", "supervisor"},
                )
                if role_id == task["role_id"]:
                    raise WorkflowError("engineer cannot independently evaluate its task")
                producer = self.db.execute(
                    """SELECT * FROM jobs WHERE task_id = ? AND kind = 'production'
                       AND state = 'completed' ORDER BY attempt DESC LIMIT 1""", (task_id,),
                ).fetchone()
                if producer is None:
                    raise WorkflowError("no frozen completed production job")
            active = self.db.execute(
                "SELECT * FROM jobs WHERE state IN ('reserved','starting','running','recovering','stop_requested')",
            ).fetchall()
            assert contract.max_active_attempts is not None
            if len(active) >= contract.max_active_attempts:
                raise WorkflowError("concurrent attempt allowance is exhausted")
            for job in active:
                prior = self.task(job["task_id"])
                if Path(prior["workspace"]) == Path(task["workspace"]):
                    raise WorkflowError("workspace is already reserved by another live attempt")
            assert contract.total_model_budget_usd is not None
            used = self.db.execute(
                """SELECT COALESCE(SUM(CASE WHEN usage_known = 1 THEN actual_usd_units
                   ELSE reserved_usd_units END), 0) FROM jobs""",
            ).fetchone()[0]
            if used + units > money_units(contract.total_model_budget_usd, allowance=True):
                raise WorkflowError("model budget reservation exceeds the remaining allowance")
            attempt = self.db.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM jobs WHERE task_id = ? AND kind = ?", (task_id, kind),
            ).fetchone()[0]
            created = datetime.now(UTC)
            cursor = self.db.execute(
                """INSERT INTO jobs(task_id, attempt, kind, role_id, producer_job_id, task_snapshot, state,
                   reserved_usd_units, created_at, next_progress_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)""",
                (task_id, attempt, kind, role_id, None if producer is None else producer["id"],
                 _json({**dict(task), "contract_revision": row["contract_revision"]}),
                 units, created.isoformat(), (created + timedelta(hours=1)).isoformat()),
            )
            job_id = cursor.lastrowid
            assert job_id is not None
            self.db.execute("INSERT INTO intents(job_id, action, created_at) VALUES (?, 'start', ?)", (job_id, _timestamp()))
            if kind == "production":
                self.db.execute("UPDATE tasks SET state = 'reserved', revision = revision + 1 WHERE id = ?", (task_id,))
            self.ledger._event("job.reserved", {"job_id": job_id, "kind": kind}, round_id=row["id"])
            return job_id

    def claim_start(self, job_id: int) -> None:
        """Trusted runner boundary; deliberately not exposed by the operator CLI."""
        with self.ledger._transaction():
            job = self.job(job_id)
            task = self.task(job["task_id"])
            _idea, row, _contract = self._task_context(task)
            if self.db.execute("SELECT 1 FROM jobs WHERE state = 'recovering' LIMIT 1").fetchone() is not None:
                raise WorkflowError("startup reconciliation must finish before dispatch")
            if job["state"] != "reserved":
                raise WorkflowError("job cannot be dispatched again")
            directives = self.ledger.directives(row["id"], job["role_id"])
            self.db.execute(
                "UPDATE jobs SET state = 'starting', directive_snapshot = ? WHERE id = ?",
                (_json(directives), job_id),
            )
            self.db.execute("UPDATE intents SET state = 'issued' WHERE job_id = ? AND action = 'start'", (job_id,))
            self.ledger._event("job.start_issued", {"job_id": job_id})

    def started(self, job_id: int, instance_id: str) -> None:
        instance_id = text(instance_id, "runner instance_id")
        with self.ledger._transaction():
            job = self.job(job_id)
            if job["instance_id"] == instance_id and job["state"] in {
                "running", "stop_requested", "stopped", "completed", "failed",
            }:
                return
            if job["state"] not in {"starting", "recovering", "stop_requested"}:
                raise WorkflowError("start receipt is not for a starting job")
            if job["instance_id"] is not None and job["instance_id"] != instance_id:
                raise WorkflowError("recovery receipt has a different instance identity")
            stopping = self.db.execute(
                "SELECT 1 FROM intents WHERE job_id = ? AND action = 'stop'", (job_id,),
            ).fetchone() is not None
            self.db.execute(
                "UPDATE jobs SET state = ?, instance_id = ? WHERE id = ?",
                ("stop_requested" if stopping else "running", instance_id, job_id),
            )
            self.db.execute("UPDATE intents SET state = 'acknowledged' WHERE job_id = ? AND action = 'start'", (job_id,))
            if job["kind"] == "production":
                self.db.execute(
                    "UPDATE tasks SET state = ?, revision = revision + 1 WHERE id = ?",
                    ("stop_requested" if stopping else "running", job["task_id"]),
                )
            for directive in json.loads(job["directive_snapshot"]):
                if directive["lifetime"] == "once":
                    self.db.execute(
                        """UPDATE directives SET consumed_job_id = ? WHERE id = ? AND active = 1
                           AND consumed_job_id IS NULL""", (job_id, directive["id"]),
                    )
            self.ledger._event("job.started", {"job_id": job_id, "instance_id": instance_id})

    def finish(
        self, job_id: int, instance_id: str, *, state: str, cost: object = None, output_ref: str | None = None,
    ) -> None:
        if state not in {"completed", "failed", "stopped"}:
            raise WorkflowError("unknown terminal receipt state")
        if state == "completed":
            output_ref = text(output_ref, "frozen output_ref")
        units = None if cost is None else money_units(cost)
        with self.ledger._transaction():
            job = self.job(job_id)
            if job["state"] in {"completed", "failed", "stopped"}:
                if (
                    job["state"] == state and job["instance_id"] == instance_id
                    and job["actual_usd_units"] == units and job["output_ref"] == output_ref
                ):
                    return
                raise WorkflowError("conflicting terminal receipt; late usage must be settled separately")
            if job["state"] not in {"running", "recovering", "stop_requested"} or job["instance_id"] != instance_id:
                raise WorkflowError("terminal receipt does not match the active runner instance")
            self.db.execute(
                """UPDATE jobs SET state = ?, output_ref = ?, actual_usd_units = ?, usage_known = ? WHERE id = ?""",
                (state, output_ref, units, int(units is not None), job_id),
            )
            self.db.execute("UPDATE intents SET state = 'acknowledged' WHERE job_id = ? AND action = 'stop'", (job_id,))
            if job["kind"] == "production":
                target = "evaluating" if state == "completed" else "blocked"
                self.db.execute("UPDATE tasks SET state = ?, revision = revision + 1 WHERE id = ?", (target, job["task_id"]))
            self.ledger._event("job.finished", {"job_id": job_id, "state": state, "usage_known": units is not None})

    def settle(self, job_id: int, cost: object) -> None:
        units = money_units(cost)
        with self.ledger._transaction():
            job = self.job(job_id)
            if job["state"] not in {"completed", "failed", "stopped"}:
                raise WorkflowError("cannot settle a still-active request")
            if job["usage_known"]:
                if job["actual_usd_units"] != units:
                    raise WorkflowError("settled usage cannot be overwritten")
                return
            self.db.execute("UPDATE jobs SET actual_usd_units = ?, usage_known = 1 WHERE id = ?", (units, job_id))
            self.ledger._event("job.usage_settled", {"job_id": job_id})

    def _stop(self, job_id: int, reason: str) -> None:
        job = self.job(job_id)
        if job["state"] not in ACTIVE_STATES:
            return
        if job["state"] == "reserved":
            self.db.execute("UPDATE intents SET state = 'cancelled' WHERE job_id = ? AND action = 'start'", (job_id,))
            self.db.execute(
                "UPDATE jobs SET state = 'stopped', usage_known = 1, actual_usd_units = 0 WHERE id = ?", (job_id,),
            )
        else:
            self.db.execute("UPDATE jobs SET state = 'stop_requested' WHERE id = ?", (job_id,))
            self.db.execute(
                "INSERT OR IGNORE INTO intents(job_id, action, created_at) VALUES (?, 'stop', ?)",
                (job_id, _timestamp()),
            )
        self.db.execute(
            "UPDATE tasks SET state = 'stop_requested', revision = revision + 1 WHERE id = ?", (job["task_id"],),
        )
        if job["state"] == "reserved":
            self.db.execute("UPDATE tasks SET state = 'cancelled' WHERE id = ?", (job["task_id"],))
        self.ledger._event("job.stop_requested", {"job_id": job_id, "reason": reason})

    def _stop_idea(self, idea_id: int, reason: str) -> None:
        for job in self.db.execute(
            "SELECT j.id FROM jobs j JOIN tasks t ON t.id = j.task_id WHERE t.idea_id = ?", (idea_id,),
        ).fetchall():
            self._stop(job["id"], reason)
        self.db.execute("UPDATE tasks SET state = 'cancelled' WHERE idea_id = ? AND state = 'queued'", (idea_id,))

    def _stop_round(self, round_id: int, reason: str) -> None:
        for job in self.db.execute(
            "SELECT j.id FROM jobs j JOIN tasks t ON t.id = j.task_id WHERE t.round_id = ?", (round_id,),
        ).fetchall():
            self._stop(job["id"], reason)
        self.db.execute("UPDATE tasks SET state = 'cancelled' WHERE round_id = ? AND state = 'queued'", (round_id,))

    def stop(self, job_id: int, reason: str) -> None:
        with self.ledger._transaction():
            self._stop(job_id, text(reason, "reason"))

    def recover(self) -> list[int]:
        with self.ledger._transaction():
            ids = [row["id"] for row in self.db.execute(
                "SELECT id FROM jobs WHERE state IN ('starting', 'running', 'stop_requested')",
            )]
            for job_id in ids:
                self.db.execute("UPDATE jobs SET state = 'recovering' WHERE id = ?", (job_id,))
                self.ledger._event("job.recovery_required", {"job_id": job_id})
            return ids

    def record_measurement(self, job_id: int, measurement: Measurement) -> int:
        """Called only for a completed independent evaluator's captured output."""
        with self.ledger._transaction():
            job = self.job(job_id)
            task = self.task(job["task_id"])
            if task["phase"] != "execution":
                raise WorkflowError("source-only research is not a performance measurement")
            idea = self.discussion.idea(task["idea_id"])
            _round, contract = self.discussion.context(idea["round_id"])
            if job["kind"] != "evaluation" or job["state"] != "completed":
                raise WorkflowError("measurement requires a completed independent evaluation job")
            producer = self.job(job["producer_job_id"])
            latest = self.db.execute(
                "SELECT id FROM jobs WHERE task_id = ? AND kind = 'production' ORDER BY attempt DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
            if latest is None or producer["id"] != latest["id"]:
                raise WorkflowError("evaluation belongs to a superseded production attempt")
            if measurement.raw_evidence_ref != job["output_ref"]:
                raise WorkflowError("measurement evidence is not the evaluator's captured output")
            if measurement.candidate_ref != producer["output_ref"]:
                raise WorkflowError("measurement candidate is not the frozen production output")
            if measurement.baseline_ref != contract.baseline_ref or measurement.protocol_ref != contract.evaluation_protocol:
                raise WorkflowError("measurement changed the frozen baseline or protocol")
            rule = json.loads(json.loads(producer["task_snapshot"])["evaluation_rule"])
            if any(getattr(measurement, field) != rule[field] for field in rule):
                raise WorkflowError("measurement changed the predeclared comparison rule")
            evidence = self._evidence_file(measurement.raw_evidence_ref)
            assert contract.storage_limits is not None
            if evidence.stat().st_size > contract.storage_limits.log_bytes:
                raise WorkflowError("structured evidence exceeds its declared log allowance")
            packet = json.loads(evidence.read_text(encoding="utf-8"))
            if (
                not isinstance(packet, dict) or packet.get("measurement") != measurement.to_dict()
                or not packet.get("raw_results") or not packet.get("command") or not packet.get("environment")
            ):
                raise WorkflowError("raw evidence does not match the captured measurement")
            verdict = measurement.verdict()
            cursor = self.db.execute(
                "INSERT INTO evaluations(job_id, body, verdict, created_at) VALUES (?, ?, ?, ?)",
                (job_id, _json(measurement.to_dict()), verdict, _timestamp()),
            )
            evaluation_id = cursor.lastrowid
            assert evaluation_id is not None
            self.db.execute(
                "UPDATE tasks SET state = ?, revision = revision + 1 WHERE id = ?",
                ("blocked" if verdict == "invalid" else "evaluating", task["id"]),
            )
            self.ledger._event("evaluation.recorded", {"evaluation_id": evaluation_id, "verdict": verdict}, round_id=idea["round_id"])
            return evaluation_id

    def record_preparation_review(self, job_id: int) -> None:
        """Adopt only a captured independent peer result, never the worker's completion claim."""
        with self.ledger._transaction():
            job = self.job(job_id)
            task = self.task(job["task_id"])
            if task["phase"] != "preparation" or job["kind"] != "evaluation" or job["state"] != "completed":
                raise WorkflowError("source review requires a completed independent preparation reviewer")
            payload = object_fields(
                json.loads(self._evidence_file(job["output_ref"]).read_text(encoding="utf-8")),
                {"status", "reason", "evidence_refs"}, "source review",
            )
            status = text(payload.get("status"), "source review status")
            refs = text_list(payload.get("evidence_refs"), "evidence_refs")
            if status not in {"supported", "needs_evidence"} or status == "supported" and not refs:
                raise WorkflowError("supported source review requires evidence references")
            self.db.execute(
                "INSERT INTO preparation_reviews VALUES (?, ?, ?, ?, ?)",
                (job_id, status, text(payload.get("reason"), "reason"), _json(refs), _timestamp()),
            )
            self.ledger._event("research.reviewed", {"job_id": job_id, "status": status}, round_id=task["round_id"])

    def _evidence_file(self, reference: str) -> Path:
        original = Path(reference)
        evidence = original.resolve(strict=True)
        if self.ledger.root not in evidence.parents or original.is_symlink() or not evidence.is_file():
            raise WorkflowError("raw evidence is outside the control-owned archive")
        return evidence

    def decide(self, task_id: int, manager: str, action: str, reason: str) -> None:
        reason = text(reason, "manager reason")
        with self.ledger._transaction():
            task = self.task(task_id)
            idea = None if task["idea_id"] is None else self.discussion.idea(task["idea_id"])
            _row, contract = self.discussion.context(task["round_id"])
            self.discussion.role(contract, manager, {"project-manager"} if idea is not None else {"project-manager", "moderator"})
            if self.db.execute(
                """SELECT 1 FROM truth_cases c JOIN jobs j ON j.id = c.job_id
                   WHERE j.task_id = ? AND c.state IN ('suspected','confirmed')""", (task_id,),
            ).fetchone() is not None:
                raise WorkflowError("task evidence is under truthfulness investigation")
            if action == "accept":
                if idea is None:
                    peer = self.db.execute(
                        """SELECT r.status, j.producer_job_id, j.output_ref FROM preparation_reviews r
                           JOIN jobs j ON j.id = r.job_id WHERE j.task_id = ? ORDER BY j.attempt DESC LIMIT 1""",
                        (task_id,),
                    ).fetchone()
                    producer = self.db.execute(
                        "SELECT id FROM jobs WHERE task_id = ? AND kind = 'production' ORDER BY attempt DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if peer is None or peer["status"] != "supported" or producer is None or peer["producer_job_id"] != producer["id"]:
                        raise WorkflowError("source adoption requires a matching independent supported review")
                    self._evidence_file(peer["output_ref"])
                    self.db.execute("UPDATE tasks SET state = 'accepted', revision = revision + 1 WHERE id = ?", (task_id,))
                    self.ledger._event("research.accepted", {"task_id": task_id, "reason": reason}, round_id=task["round_id"])
                    return
                result = self.db.execute(
                    """SELECT e.verdict, e.body, j.producer_job_id FROM evaluations e JOIN jobs j ON j.id = e.job_id
                       WHERE j.task_id = ? ORDER BY e.id DESC LIMIT 1""", (task_id,),
                ).fetchone()
                if result is None or result["verdict"] not in {"improved", "unchanged"}:
                    raise WorkflowError("acceptance requires valid independent evidence")
                producer = self.db.execute(
                    "SELECT id FROM jobs WHERE task_id = ? AND kind = 'production' ORDER BY attempt DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if producer is None or producer["id"] != result["producer_job_id"]:
                    raise WorkflowError("current production attempt has no matching independent evidence")
                self._evidence_file(json.loads(result["body"])["raw_evidence_ref"])
                if idea["state"] != "released":
                    raise WorkflowError("idea is not active")
                target = "accepted"
            elif action == "continue":
                self._task_context(task)
                if task["state"] not in {"blocked", "evaluating"}:
                    raise WorkflowError("task is not awaiting a next-attempt decision")
                if self.db.execute(
                    """SELECT 1 FROM jobs WHERE task_id = ? AND
                       state IN ('reserved','starting','running','recovering','stop_requested')""", (task_id,),
                ).fetchone() is not None:
                    raise WorkflowError("current attempt has not fully stopped")
                target = "queued"
            elif action == "stop":
                for job in self.db.execute("SELECT id FROM jobs WHERE task_id = ?", (task_id,)).fetchall():
                    self._stop(job["id"], reason)
                target = "cancelled"
            else:
                raise WorkflowError("unknown manager task decision")
            self.db.execute("UPDATE tasks SET state = ?, revision = revision + 1 WHERE id = ?", (target, task_id))
            self.ledger._event("task.decision", {"task_id": task_id, "action": action, "reason": reason})

    def request_context(self, job_id: int) -> dict[str, object]:
        job = self.job(job_id)
        snapshot = json.loads(job["task_snapshot"])
        revision, contract = self.ledger.contract(snapshot["contract_revision"])
        role = self.discussion.role(contract, job["role_id"], {item.kind for item in contract.roles or ()})
        public = contract.to_dict()
        public["roles"] = [{"role_id": item.role_id, "kind": item.kind} for item in contract.roles or ()]
        producer = None if job["producer_job_id"] is None else self.job(job["producer_job_id"])["output_ref"]
        duty = ROLE_DUTIES[role.kind]
        runner_type = "copilot-role"
        if job["kind"] == "evaluation":
            if snapshot["phase"] == "preparation":
                duty = (
                    "Independently verify the assigned source-only output. Return supported/needs_evidence, "
                    "reason and evidence_refs. Do not turn a source review into a performance claim."
                )
            else:
                duty = "Run the frozen deterministic evaluation program; do not generate measurements with a model."
                runner_type = "evaluation-program"
        return {
            "job_id": job_id, "state": job["state"], "kind": job["kind"], "contract_revision": revision,
            "role": role.role_id, "duty": duty, "instruction": role.instruction, "runner_type": runner_type,
            "knowledge_refs": role.knowledge_refs, "contract": public, "task": snapshot,
            "producer_output_ref": producer, "directives": json.loads(job["directive_snapshot"]),
            "execution_enabled": False,
            "notice": "frozen request context, not permission to execute or a provider-session claim",
        }

    def card(self, evaluation_id: int, reason: str, *, manager: str) -> int:
        reason = text(reason, "card reason")
        with self.ledger._transaction():
            evaluation = self.db.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
            if evaluation is None or evaluation["verdict"] != "regressed":
                raise WorkflowError("yellow cards require valid, confirmed negative optimization")
            self._evidence_file(json.loads(evaluation["body"])["raw_evidence_ref"])
            job = self.job(evaluation["job_id"])
            task = self.task(job["task_id"])
            idea = self.discussion.idea(task["idea_id"])
            _round, contract = self.discussion.context(idea["round_id"])
            self.discussion.role(contract, manager, {"project-manager"})
            prior = self.db.execute("SELECT id FROM yellow_cards WHERE evaluation_id = ?", (evaluation_id,)).fetchone()
            if prior is not None:
                return prior["id"]
            cursor = self.db.execute(
                "INSERT INTO yellow_cards(idea_id, evaluation_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (task["idea_id"], evaluation_id, reason, _timestamp()),
            )
            card_id = cursor.lastrowid
            assert card_id is not None
            count = self.db.execute(
                "SELECT COUNT(*) FROM yellow_cards WHERE idea_id = ? AND revoked_reason IS NULL", (task["idea_id"],),
            ).fetchone()[0]
            self.db.execute(
                "UPDATE ideas SET state = ? WHERE id = ?", ("terminated" if count >= 2 else "paused", task["idea_id"]),
            )
            self.db.execute("UPDATE tasks SET state = 'blocked' WHERE id = ?", (task["id"],))
            self._stop_idea(task["idea_id"], "negative optimization")
            self.ledger._event("yellow_card.issued", {"card_id": card_id, "count": count})
            return card_id

    def revoke_card(self, card_id: int, *, manager: str, reason: str, correction_ref: str) -> None:
        reason = text(reason, "correction reason")
        self._evidence_file(correction_ref)
        with self.ledger._transaction():
            card = self.db.execute("SELECT * FROM yellow_cards WHERE id = ?", (card_id,)).fetchone()
            if card is None or card["revoked_reason"] is not None:
                raise WorkflowError("card is missing or already revoked")
            idea = self.discussion.idea(card["idea_id"])
            _round, contract = self.discussion.context(idea["round_id"])
            self.discussion.role(contract, manager, {"project-manager"})
            self.db.execute("UPDATE yellow_cards SET revoked_reason = ? WHERE id = ?", (reason, card_id))
            self.db.execute("UPDATE ideas SET state = 'held' WHERE id = ?", (idea["id"],))
            self.ledger._event(
                "yellow_card.revoked", {"card_id": card_id, "reason": reason, "evidence": correction_ref},
                round_id=idea["round_id"],
            )

    def consultation(self, idea_id: int, *, manager: str, expert: str, engineer: str, recap: str) -> None:
        recap = text(recap, "consultation plan")
        with self.ledger._transaction():
            idea = self.discussion.idea(idea_id)
            _round, contract = self.discussion.context(idea["round_id"])
            self.discussion.role(contract, manager, {"project-manager"})
            self.discussion.role(contract, expert, {"expert", "co-optimization-expert"})
            self.discussion.role(contract, engineer, {"engineer", "system-engineer"})
            if idea["state"] != "paused" or expert != idea["owner"]:
                raise WorkflowError("consultation must address the first-card paused idea")
            if self.db.execute(
                """SELECT 1 FROM jobs j JOIN tasks t ON t.id = j.task_id WHERE t.idea_id = ?
                   AND j.state IN ('reserved','starting','running','recovering','stop_requested')""", (idea_id,),
            ).fetchone() is not None:
                raise WorkflowError("associated jobs have not stopped")
            self.db.execute("UPDATE ideas SET state = 'released' WHERE id = ?", (idea_id,))
            self.db.execute("UPDATE tasks SET state = 'queued', revision = revision + 1 WHERE idea_id = ? AND state = 'blocked'", (idea_id,))
            self.db.execute(
                "UPDATE tasks SET objective = objective || ? WHERE idea_id = ? AND state = 'queued'",
                ("\nConfirmed consultation adjustment:\n" + recap, idea_id),
            )
            self.ledger._event("idea.consultation", {"idea_id": idea_id, "plan": recap}, round_id=idea["round_id"])

    def truth_case(self, job_id: int, claim: str, evidence_ref: str) -> int:
        with self.ledger._transaction():
            self.job(job_id)
            cursor = self.db.execute(
                "INSERT INTO truth_cases(job_id, claim, evidence_ref, state, created_at) VALUES (?, ?, ?, 'suspected', ?)",
                (job_id, text(claim, "claim"), text(evidence_ref, "evidence_ref"), _timestamp()),
            )
            case_id = cursor.lastrowid
            assert case_id is not None
            self._stop(job_id, "truthfulness concern")
            job = self.job(job_id)
            self.db.execute("UPDATE tasks SET state = 'blocked' WHERE id = ?", (job["task_id"],))
            self.ledger._event("truth.suspected", {"case_id": case_id})
            return case_id

    def confirm_truth(self, case_id: int, resolution: str) -> None:
        """Trusted evidence adjudication, not an Agent's assertion or public CLI action."""
        with self.ledger._transaction():
            case = self.db.execute("SELECT * FROM truth_cases WHERE id = ?", (case_id,)).fetchone()
            if case is None or case["state"] != "suspected":
                raise WorkflowError("truth case is not awaiting factual adjudication")
            self._evidence_file(case["evidence_ref"])
            self.db.execute(
                "UPDATE truth_cases SET state = 'confirmed', resolution = ? WHERE id = ?",
                (text(resolution, "factual resolution"), case_id),
            )
            self._stop(case["job_id"], "TRUTH_VIOLATION")
            self.ledger._event("truth.confirmed", {"case_id": case_id, "job_id": case["job_id"]})

    def dismiss_truth(self, case_id: int, resolution: str) -> None:
        with self.ledger._transaction():
            case = self.db.execute("SELECT * FROM truth_cases WHERE id = ?", (case_id,)).fetchone()
            if case is None or case["state"] != "suspected":
                raise WorkflowError("truth case is not awaiting factual adjudication")
            self.db.execute(
                "UPDATE truth_cases SET state = 'dismissed', resolution = ? WHERE id = ?",
                (text(resolution, "resolution"), case_id),
            )
            self.ledger._event("truth.dismissed", {"case_id": case_id})

    def inspect(self) -> dict[str, object]:
        return {
            "tasks": [dict(row) for row in self.db.execute("SELECT * FROM tasks ORDER BY id")],
            "jobs": [dict(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY id")],
            "intents": [dict(row) for row in self.db.execute("SELECT * FROM intents ORDER BY id")],
            "evaluations": [dict(row) for row in self.db.execute("SELECT * FROM evaluations ORDER BY id")],
            "yellow_cards": [dict(row) for row in self.db.execute("SELECT * FROM yellow_cards ORDER BY id")],
            "truth_cases": [dict(row) for row in self.db.execute("SELECT * FROM truth_cases ORDER BY id")],
            "budget": self.budget(),
            "execution_enabled": False,
        }

    def progress_due(self, now: datetime) -> list[int]:
        due: list[int] = []
        with self.ledger._transaction():
            for job in self.db.execute("SELECT * FROM jobs WHERE state = 'running'").fetchall():
                deadline = utc_deadline(job["next_progress_at"])
                if deadline <= now:
                    steps = int((now - deadline).total_seconds() // 3600) + 1
                    self.db.execute(
                        "UPDATE jobs SET next_progress_at = ? WHERE id = ?",
                        ((deadline + timedelta(hours=steps)).isoformat(), job["id"]),
                    )
                    self.ledger._event("job.progress_check_due", {"job_id": job["id"]})
                    due.append(job["id"])
        return due
