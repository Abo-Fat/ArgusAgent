"""SQLite-owned contracts, preparation state and scoped operator directives."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .contract import ResearchContract, WorkflowError, integer, text
from .schema import SCHEMA_VERSION, migrate

DATABASE_NAME = "workflow.sqlite3"
_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE contracts (
    revision INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE rounds (
    id INTEGER PRIMARY KEY,
    contract_revision INTEGER NOT NULL REFERENCES contracts(revision),
    state TEXT NOT NULL CHECK(state IN ('draft', 'prepared', 'paused', 'cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE directives (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    round_id INTEGER REFERENCES rounds(id),
    role_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    round_id INTEGER REFERENCES rounds(id),
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
PRAGMA user_version = 1;
COMMIT;
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    mode = "ro" if readonly else "rw"
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class Ledger:
    def __init__(self, database: Path, *, readonly: bool = False) -> None:
        self.path = database.expanduser().resolve()
        self.root = self.path.parent
        self.connection = _connect(self.path, readonly=readonly)
        try:
            self._validate()
        except (sqlite3.Error, ValueError):
            self.connection.close()
            raise

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    @classmethod
    def create(cls, root: Path, contract: ResearchContract) -> Ledger:
        root = root.expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(root.iterdir()):
            raise WorkflowError("new workflow state directory must be empty; legacy state is not imported")
        path = root / DATABASE_NAME
        path.touch(mode=0o600, exist_ok=False)
        connection = _connect(path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            migrate(connection)
            with connection:
                connection.execute(
                    "INSERT INTO contracts VALUES (?, ?, ?)",
                    (1, _json(contract.to_dict()), _timestamp()),
                )
                connection.execute(
                    "INSERT INTO events(kind, payload, created_at) VALUES (?, ?, ?)",
                    ("project.created", _json({"contract_revision": 1}), _timestamp()),
                )
        finally:
            connection.close()
        return cls(path)

    @classmethod
    def migrate(cls, database: Path) -> None:
        connection = _connect(database.expanduser().resolve())
        try:
            migrate(connection)
        finally:
            connection.close()

    def _validate(self) -> None:
        if self.connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise WorkflowError("unsupported workflow database version; use the explicit migrate command")
        if self.connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise WorkflowError("workflow database integrity check failed")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkflowError("workflow database references are incomplete")
        if self.connection.execute("SELECT 1 FROM contracts LIMIT 1").fetchone() is None:
            raise WorkflowError("workflow database has no operator contract")
        for row in self.connection.execute("SELECT payload FROM contracts"):
            ResearchContract.from_value(json.loads(row["payload"]))

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _event(self, kind: str, payload: object, *, round_id: int | None = None) -> None:
        self.connection.execute(
            "INSERT INTO events(kind, round_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (kind, round_id, _json(payload), _timestamp()),
        )

    def contract(self, revision: int | None = None) -> tuple[int, ResearchContract]:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM contracts ORDER BY revision DESC LIMIT 1",
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM contracts WHERE revision = ?", (revision,),
            ).fetchone()
        if row is None:
            raise WorkflowError(f"contract revision not found: {revision}")
        return row["revision"], ResearchContract.from_value(json.loads(row["payload"]))

    def revise_contract(self, contract: ResearchContract, *, expected_revision: int) -> int:
        integer(expected_revision, "expected_revision")
        with self._transaction():
            current, prior = self.contract()
            if current != expected_revision:
                raise WorkflowError(f"stale contract revision: expected {expected_revision}, current {current}")
            if contract == prior:
                return current
            revision = current + 1
            self.connection.execute(
                "INSERT INTO contracts VALUES (?, ?, ?)",
                (revision, _json(contract.to_dict()), _timestamp()),
            )
            self.connection.execute(
                "UPDATE rounds SET state = 'draft', revision = revision + 1 WHERE state = 'prepared'",
            )
            from .jobs import JobQueue

            for row in self.connection.execute(
                "SELECT * FROM rounds WHERE state NOT IN ('draft','paused','cancelled','completed')",
            ).fetchall():
                JobQueue(self)._stop_round(row["id"], "operator contract revised")
                self.connection.execute(
                    "UPDATE rounds SET paused_from = state, state = 'paused', revision = revision + 1 WHERE id = ?",
                    (row["id"],),
                )
            self._event("contract.revised", {"prior_revision": current, "revision": revision})
            return revision

    def rebind_round(self, round_id: int, *, expected_revision: int) -> None:
        with self._transaction():
            row = self._round(round_id)
            if row["revision"] != expected_revision or row["state"] != "draft":
                raise WorkflowError("only an unchanged draft can explicitly bind a new contract")
            if self.connection.execute("SELECT 1 FROM ideas WHERE round_id = ?", (round_id,)).fetchone() is not None:
                raise WorkflowError("discussion has already started; create a new round")
            if self.connection.execute(
                """SELECT 1 FROM jobs j JOIN tasks t ON t.id = j.task_id WHERE t.round_id = ?
                   AND j.state IN ('reserved','starting','running','recovering','stop_requested')""", (round_id,),
            ).fetchone() is not None:
                raise WorkflowError("preparation jobs must be stopped before changing their round contract")
            revision, _contract = self.contract()
            self.connection.execute(
                "UPDATE rounds SET contract_revision = ?, revision = revision + 1 WHERE id = ?", (revision, round_id),
            )
            self._event("round.rebound", {"contract_revision": revision}, round_id=round_id)

    def create_round(self) -> int:
        with self._transaction():
            revision, _contract = self.contract()
            cursor = self.connection.execute(
                "INSERT INTO rounds(contract_revision, state, created_at) VALUES (?, ?, ?)",
                (revision, "draft", _timestamp()),
            )
            round_id = cursor.lastrowid
            assert round_id is not None
            self._event("round.created", {"contract_revision": revision}, round_id=round_id)
            return round_id

    def _round(self, round_id: int) -> sqlite3.Row:
        integer(round_id, "round_id")
        row = self.connection.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"round not found: {round_id}")
        return row

    def transition(self, round_id: int, action: str, *, expected_revision: int) -> dict[str, object]:
        integer(expected_revision, "expected_revision")
        with self._transaction():
            row = self._round(round_id)
            if row["revision"] != expected_revision:
                raise WorkflowError(f"stale round revision; current revision is {row['revision']}")
            state = row["state"]
            if state in {"cancelled", "completed"}:
                raise WorkflowError("cancelled rounds cannot be resumed")
            current, _latest = self.contract()
            _revision, contract = self.contract(row["contract_revision"])
            if action in {"prepare", "resume"}:
                allowed = {"draft"} if action == "prepare" else {"paused"}
                if state not in allowed:
                    raise WorkflowError(f"cannot {action} a round in state {state}")
                if current != row["contract_revision"]:
                    raise WorkflowError("round is bound to an older contract; create a new round")
                target = "prepared" if action == "prepare" else row["paused_from"] or "prepared"
                if target != "draft":
                    blockers = contract.research_blockers(datetime.now(UTC))
                    if blockers:
                        raise WorkflowError("round preparation blocked: " + "; ".join(blockers))
            elif action == "pause":
                if state == "paused":
                    raise WorkflowError(f"cannot pause a round in state {state}")
                target = "paused"
            elif action == "cancel":
                target = "cancelled"
            else:
                raise WorkflowError(f"unsupported preparation action: {action}")
            if action in {"pause", "cancel"}:
                from .jobs import JobQueue

                JobQueue(self)._stop_round(round_id, f"operator {action}")
            if action == "pause":
                self.connection.execute("UPDATE rounds SET paused_from = ? WHERE id = ?", (state, round_id))
            self.connection.execute(
                "UPDATE rounds SET state = ?, revision = revision + 1 WHERE id = ?",
                (target, round_id),
            )
            self._event(
                f"round.{action}", {"from": state, "to": target}, round_id=round_id,
            )
        return self.round_status(round_id)

    def round_status(self, round_id: int) -> dict[str, object]:
        row = self._round(round_id)
        current, _latest = self.contract()
        _revision, contract = self.contract(row["contract_revision"])
        blockers = contract.preparation_blockers(datetime.now(UTC))
        if row["contract_revision"] != current:
            blockers.append("contract changed; create a new round")
        return {
            "round_id": round_id,
            "revision": row["revision"],
            "contract_revision": row["contract_revision"],
            "state": row["state"],
            "review_cycle": row["review_cycle"],
            "preparation_blockers": blockers,
            "research_blockers": contract.research_blockers(datetime.now(UTC)),
            "discussion_blockers": contract.discussion_blockers(datetime.now(UTC)),
            "execution_enabled": False,
            "execution_blockers": [
                "agent runtime, enforced resource limits and independent evaluation are not implemented",
            ],
        }

    def status(self) -> dict[str, object]:
        from .jobs import JobQueue

        revision, contract = self.contract()
        return {
            "database": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "contract_revision": revision,
            "contract": contract.to_dict(),
            "rounds": [
                self.round_status(row["id"])
                for row in self.connection.execute("SELECT id FROM rounds ORDER BY id")
            ],
            "execution_enabled": False,
            "assignments": JobQueue(self).inspect(),
        }

    def add_directive(
        self, content: str, *, role_id: str = "all", round_id: int | None = None,
        lifetime: str = "standing",
    ) -> int:
        content = text(content, "directive")
        role_id = text(role_id, "role_id")
        # Validate credential exposure without silently changing the operator's rule.
        ResearchContract.from_value({"objective": content})
        if lifetime not in {"standing", "once", "bounded"}:
            raise WorkflowError("unknown directive lifetime")
        if lifetime == "bounded" and round_id is None:
            raise WorkflowError("bounded directives must name their round")
        with self._transaction():
            if round_id is not None:
                row = self._round(round_id)
                if row["state"] == "cancelled":
                    raise WorkflowError("cannot add a directive to a cancelled round")
                _revision, contract = self.contract(row["contract_revision"])
            else:
                _revision, contract = self.contract()
            if role_id != "all" and role_id not in {role.role_id for role in contract.roles or ()}:
                raise WorkflowError(f"role is not in the contract roster: {role_id}")
            prior = self.connection.execute(
                """SELECT id FROM directives WHERE text = ? AND role_id = ? AND round_id IS ?
                   AND active = 1 AND lifetime = 'standing'""",
                (content, role_id, round_id),
            ).fetchone()
            if prior is not None and lifetime == "standing":
                return prior["id"]
            cursor = self.connection.execute(
                """INSERT INTO directives(text, round_id, role_id, active, created_at, lifetime)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (content, round_id, role_id, _timestamp(), lifetime),
            )
            directive_id = cursor.lastrowid
            assert directive_id is not None
            self._event("directive.added", {"directive_id": directive_id, "role_id": role_id}, round_id=round_id)
            return directive_id

    def revoke_directive(self, directive_id: int) -> None:
        integer(directive_id, "directive_id")
        with self._transaction():
            row = self.connection.execute("SELECT * FROM directives WHERE id = ?", (directive_id,)).fetchone()
            if row is None:
                raise WorkflowError(f"directive not found: {directive_id}")
            if not row["active"]:
                return
            self.connection.execute("UPDATE directives SET active = 0 WHERE id = ?", (directive_id,))
            self._event("directive.revoked", {"directive_id": directive_id}, round_id=row["round_id"])

    def directives(self, round_id: int, role_id: str) -> list[dict[str, object]]:
        row = self._round(round_id)
        _revision, contract = self.contract(row["contract_revision"])
        if role_id not in {role.role_id for role in contract.roles or ()}:
            raise WorkflowError(f"role is not in the contract roster: {role_id}")
        rows = self.connection.execute(
            """SELECT id, text, round_id, role_id, lifetime FROM directives
               WHERE active = 1 AND (round_id IS NULL OR round_id = ?)
                 AND (role_id = 'all' OR role_id = ?)
                 AND consumed_job_id IS NULL
               ORDER BY round_id IS NULL, role_id = 'all', id DESC""",
            (round_id, role_id),
        )
        projected: dict[object, dict[str, object]] = {}
        for item in rows:
            key = item["text"] if item["lifetime"] == "standing" else item["id"]
            projected.setdefault(key, dict(item))
        return list(projected.values())

    def events(self, *, after: int = 0, limit: int = 50) -> list[dict[str, object]]:
        integer(after, "after", minimum=0)
        integer(limit, "limit")
        return [
            {
                "id": row["id"], "kind": row["kind"], "round_id": row["round_id"],
                "payload": json.loads(row["payload"]), "created_at": row["created_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit),
            )
        ]

    def backup(self, target: Path) -> Path:
        from .reports import Reports

        self.connection.execute("BEGIN")
        try:
            for row in self.connection.execute("SELECT id FROM reports"):
                Reports(self).verify_files(row["id"])
            return self._copy_database(target)
        finally:
            self.connection.rollback()

    def _copy_database(self, target: Path) -> Path:
        target = target.expanduser().resolve()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.touch(mode=0o600, exist_ok=False)
        destination = sqlite3.connect(target)
        try:
            self.connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise WorkflowError("backup integrity check failed")
        finally:
            destination.close()
        return target

    @classmethod
    def restore(cls, source: Path, root: Path) -> Ledger:
        source = source.expanduser().resolve()
        with cls(source, readonly=True) as backup:
            root = root.expanduser().resolve()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if any(root.iterdir()):
                raise WorkflowError("restore destination must be empty; existing state is never overwritten")
            backup._copy_database(root / DATABASE_NAME)
        restored = cls(root / DATABASE_NAME)
        try:
            reports = restored.connection.execute("SELECT * FROM reports").fetchall()
            for report in reports:
                directory = root / "reports" / f"round-{report['round_id']}" / f"version-{report['version']}"
                directory.mkdir(mode=0o700, parents=True, exist_ok=False)
                for kind in ("long", "short"):
                    with (directory / f"{kind}.md").open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(report[f"{kind}_body"])
            with restored._transaction():
                for report in reports:
                    directory = root / "reports" / f"round-{report['round_id']}" / f"version-{report['version']}"
                    restored.connection.execute(
                        "UPDATE reports SET long_path = ?, short_path = ?, channel = NULL WHERE id = ?",
                        (str(directory / "long.md"), str(directory / "short.md"), report["id"]),
                    )
            from .jobs import JobQueue

            JobQueue(restored).recover()
            return restored
        except BaseException:
            restored.connection.close()
            raise
