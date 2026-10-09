"""Atomic version-two migration for the portable research control plane."""

from __future__ import annotations

import sqlite3

from .contract import WorkflowError

SCHEMA_VERSION = 2

MIGRATION = """
CREATE TABLE rounds_next (
    id INTEGER PRIMARY KEY,
    contract_revision INTEGER NOT NULL REFERENCES contracts(revision),
    state TEXT NOT NULL CHECK(state IN (
        'draft', 'prepared', 'paused', 'cancelled', 'proposals', 'reviews',
        'rebuttal', 'reporting', 'comment_window', 'released', 'completed'
    )),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    review_cycle INTEGER NOT NULL DEFAULT 0,
    paused_from TEXT
);
INSERT INTO rounds_next(id, contract_revision, state, revision, created_at)
    SELECT id, contract_revision, state, revision, created_at FROM rounds;
DROP TABLE rounds;
ALTER TABLE rounds_next RENAME TO rounds;
ALTER TABLE directives ADD COLUMN lifetime TEXT NOT NULL DEFAULT 'standing'
    CHECK(lifetime IN ('standing', 'once', 'bounded'));
ALTER TABLE directives ADD COLUMN consumed_job_id INTEGER REFERENCES jobs(id);
CREATE TABLE ideas (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    owner TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'held', 'rejected', 'released', 'paused', 'terminated', 'accepted'))
);
CREATE TABLE proposals (
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(idea_id, version)
);
CREATE TABLE reviews (
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    proposal_version INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(idea_id, proposal_version, cycle, reviewer),
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE TABLE rebuttals (
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    cycle INTEGER NOT NULL,
    proposal_version INTEGER NOT NULL,
    response TEXT NOT NULL,
    changes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(idea_id, cycle),
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    version INTEGER NOT NULL,
    long_body TEXT NOT NULL,
    short_body TEXT NOT NULL,
    long_path TEXT NOT NULL,
    short_path TEXT NOT NULL,
    submitted_at TEXT,
    deadline TEXT,
    channel TEXT,
    UNIQUE(round_id, version)
);
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    idea_id INTEGER REFERENCES ideas(id),
    kind TEXT NOT NULL CHECK(kind IN ('comment', 'pause', 'reject', 'approve')),
    body TEXT NOT NULL,
    received_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);
CREATE TABLE report_targets (
    report_id INTEGER NOT NULL REFERENCES reports(id),
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    proposal_version INTEGER NOT NULL,
    PRIMARY KEY(report_id, idea_id),
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE TABLE report_reissues (
    idea_id INTEGER PRIMARY KEY REFERENCES ideas(id)
);
CREATE TABLE releases (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id),
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    proposal_version INTEGER NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('EXPLICIT_RELEASE', 'TIMEOUT_AUTO_RELEASE')),
    created_at TEXT NOT NULL,
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE UNIQUE INDEX one_timeout_release ON releases(report_id, idea_id, proposal_version)
    WHERE mode = 'TIMEOUT_AUTO_RELEASE';
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    phase TEXT NOT NULL CHECK(phase IN ('preparation','execution')),
    idea_id INTEGER REFERENCES ideas(id),
    proposal_version INTEGER,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    role_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    evaluation_rule TEXT,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN (
        'queued', 'reserved', 'running', 'evaluating', 'accepted',
        'blocked', 'stop_requested', 'cancelled'
    )),
    revision INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE TABLE dependencies (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    depends_on INTEGER NOT NULL REFERENCES tasks(id),
    PRIMARY KEY(task_id, depends_on),
    CHECK(task_id != depends_on)
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('production', 'evaluation')),
    role_id TEXT NOT NULL,
    producer_job_id INTEGER REFERENCES jobs(id),
    task_snapshot TEXT NOT NULL,
    directive_snapshot TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK(state IN (
        'reserved', 'starting', 'running', 'recovering', 'stop_requested',
        'stopped', 'completed', 'failed'
    )),
    instance_id TEXT,
    output_ref TEXT,
    reserved_usd_units INTEGER NOT NULL CHECK(reserved_usd_units >= 0),
    actual_usd_units INTEGER CHECK(actual_usd_units >= 0),
    usage_known INTEGER NOT NULL DEFAULT 0 CHECK(usage_known IN (0, 1)),
    created_at TEXT NOT NULL,
    next_progress_at TEXT NOT NULL,
    UNIQUE(task_id, attempt, kind)
);
CREATE TABLE confirmations (
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    proposal_version INTEGER NOT NULL,
    engineer TEXT NOT NULL,
    expert TEXT NOT NULL,
    recap TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(idea_id, proposal_version, engineer),
    FOREIGN KEY(idea_id, proposal_version) REFERENCES proposals(idea_id, version)
);
CREATE TABLE intents (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    action TEXT NOT NULL CHECK(action IN ('start', 'stop')),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'issued', 'acknowledged', 'cancelled')),
    created_at TEXT NOT NULL,
    UNIQUE(job_id, action)
);
CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    body TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('improved', 'regressed', 'unchanged', 'invalid')),
    created_at TEXT NOT NULL
);
CREATE TABLE preparation_reviews (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL CHECK(status IN ('supported', 'needs_evidence')),
    reason TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE yellow_cards (
    id INTEGER PRIMARY KEY,
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    evaluation_id INTEGER NOT NULL UNIQUE REFERENCES evaluations(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_reason TEXT
);
CREATE TABLE truth_cases (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    claim TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('suspected', 'confirmed', 'dismissed')),
    resolution TEXT,
    created_at TEXT NOT NULL
);
"""


def migrate(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 1:
        raise WorkflowError("unsupported workflow database version")
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + MIGRATION)
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise WorkflowError("migration found incomplete database references")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")
