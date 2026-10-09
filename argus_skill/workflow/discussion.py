"""Independent submission protocol and role-scoped context, without model calls."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from .contract import ResearchContract, Role, WorkflowError, integer, text
from .protocol import Proposal, Review
from .store import Ledger, _json, _timestamp

ROLE_DUTIES = {
    "researcher": "Investigate approved sources and counterexamples, record access limits and method cards. Do not run candidate experiments.",
    "distiller": "Distill documented methods and failure conditions into role instructions. Do not grant tools or change authority.",
    "moderator": "Distribute allowed materials, enforce agreed rounds, retain disagreements and deliver both reports.",
    "expert": "Propose testable mechanisms from your documented specialty; guide implementation and explain failures.",
    "co-optimization-expert": "Develop joint mechanisms, interfaces and end-to-end experiments without reading private first proposals.",
    "reviewer": "Independently score the five common dimensions; provide value/risk/action and explicit blocking issues.",
    "project-manager": "Decide order, bounded allocations and stop/continue actions within operator authority.",
    "engineer": "Implement the confirmed contract and submit real artifacts; do not certify your own results.",
    "system-engineer": "Implement confirmed cross-layer interfaces with disjoint writable workspaces.",
    "supervisor": "Interpret independently captured evidence and costs; do not confuse missing data with regression.",
}


class Discussion:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.db = ledger.connection

    def context(self, round_id: int, state: str | None = None) -> tuple[sqlite3.Row, ResearchContract]:
        row = self.ledger._round(round_id)
        revision, contract = self.ledger.contract()
        if revision != row["contract_revision"]:
            raise WorkflowError("round contract is stale")
        if state is not None and row["state"] != state:
            raise WorkflowError(f"expected round state {state}; current state is {row['state']}")
        return row, contract

    def role(self, contract: ResearchContract, role_id: str, kinds: set[str]) -> Role:
        role = next((item for item in contract.roles or () if item.role_id == role_id), None)
        if role is None or role.kind not in kinds:
            raise WorkflowError(f"role {role_id!r} cannot perform this operation")
        return role

    def idea(self, idea_id: int) -> sqlite3.Row:
        integer(idea_id, "idea_id")
        row = self.db.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        if row is None:
            raise WorkflowError(f"idea not found: {idea_id}")
        return row

    def _state(self, round_id: int, target: str) -> None:
        self.db.execute(
            "UPDATE rounds SET state = ?, revision = revision + 1 WHERE id = ?",
            (target, round_id),
        )
        self.ledger._event("discussion.phase", {"state": target}, round_id=round_id)

    def begin(self, round_id: int) -> None:
        with self.ledger._transaction():
            _row, contract = self.context(round_id, "prepared")
            blockers = contract.discussion_blockers(datetime.now(UTC))
            if blockers:
                raise WorkflowError("discussion is blocked: " + "; ".join(blockers))
            if self.db.execute(
                "SELECT 1 FROM tasks WHERE round_id = ? AND phase = 'preparation' AND state != 'accepted'",
                (round_id,),
            ).fetchone() is not None:
                raise WorkflowError("assigned preparation work is incomplete")
            self._state(round_id, "proposals")

    def propose(self, round_id: int, role_id: str, proposal: Proposal) -> int:
        with self.ledger._transaction():
            _row, contract = self.context(round_id, "proposals")
            role = self.role(contract, role_id, {"expert", "co-optimization-expert"})
            if role.kind == "co-optimization-expert" and proposal.innovation != "joint":
                raise WorkflowError("co-optimization proposals must describe joint innovation")
            count = self.db.execute(
                "SELECT COUNT(*) FROM ideas WHERE round_id = ? AND owner = ?", (round_id, role_id),
            ).fetchone()[0]
            if contract.max_ideas_per_expert is None or count >= contract.max_ideas_per_expert:
                raise WorkflowError("expert proposal allowance is exhausted")
            cursor = self.db.execute("INSERT INTO ideas(round_id, owner) VALUES (?, ?)", (round_id, role_id))
            idea_id = cursor.lastrowid
            assert idea_id is not None
            self.db.execute(
                "INSERT INTO proposals VALUES (?, 1, ?, 'operator-import', ?)",
                (idea_id, _json(proposal.to_dict()), _timestamp()),
            )
            self.ledger._event("proposal.submitted", {"idea_id": idea_id, "owner": role_id}, round_id=round_id)
            return idea_id

    def review(self, idea_id: int, role_id: str, review: Review, *, expected_version: int) -> None:
        with self.ledger._transaction():
            idea = self.idea(idea_id)
            row, contract = self.context(idea["round_id"], "reviews")
            self.role(contract, role_id, {"reviewer"})
            if idea["version"] != expected_version:
                raise WorkflowError("review references a stale proposal")
            self.db.execute(
                "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?)",
                (idea_id, expected_version, row["review_cycle"], role_id, _json(review.to_dict()), _timestamp()),
            )
            self.ledger._event(
                "review.submitted", {"idea_id": idea_id, "reviewer": role_id, "cycle": row["review_cycle"]},
                round_id=idea["round_id"],
            )

    def rebut(
        self, idea_id: int, role_id: str, response: str, changes: str, *,
        expected_version: int, proposal: Proposal | None = None,
    ) -> None:
        response, changes = text(response, "response"), text(changes, "changes")
        with self.ledger._transaction():
            idea = self.idea(idea_id)
            row, contract = self.context(idea["round_id"], "rebuttal")
            self.role(contract, role_id, {"expert", "co-optimization-expert"})
            if idea["owner"] != role_id or idea["version"] != expected_version:
                raise WorkflowError("rebuttal owner or proposal version does not match")
            cycle = row["review_cycle"] + 1
            if self.db.execute(
                "SELECT 1 FROM rebuttals WHERE idea_id = ? AND cycle = ?", (idea_id, cycle),
            ).fetchone() is not None:
                raise WorkflowError("rebuttal is already locked for this cycle")
            version = expected_version
            if proposal is not None:
                version += 1
                self.db.execute(
                    "INSERT INTO proposals VALUES (?, ?, ?, 'operator-import', ?)",
                    (idea_id, version, _json(proposal.to_dict()), _timestamp()),
                )
                self.db.execute("UPDATE ideas SET version = ? WHERE id = ?", (version, idea_id))
            self.db.execute(
                "INSERT INTO rebuttals VALUES (?, ?, ?, ?, ?, ?)",
                (idea_id, cycle, version, response, changes, _timestamp()),
            )
            self.ledger._event("rebuttal.submitted", {"idea_id": idea_id, "cycle": cycle}, round_id=idea["round_id"])

    def advance(self, round_id: int) -> str:
        with self.ledger._transaction():
            row, contract = self.context(round_id)
            ideas = self.db.execute("SELECT * FROM ideas WHERE round_id = ? ORDER BY id", (round_id,)).fetchall()
            if row["state"] == "proposals":
                owners = {idea["owner"] for idea in ideas}
                required = {
                    role.role_id for role in contract.roles or ()
                    if role.kind in {"expert", "co-optimization-expert"}
                }
                if not required.issubset(owners):
                    raise WorkflowError("first-round proposals are incomplete")
                target = "reviews"
            elif row["state"] == "reviews":
                if any(not self.reviews_complete(idea["id"], row["review_cycle"]) for idea in ideas):
                    raise WorkflowError("independent reviews are incomplete")
                assert contract.rebuttal_rounds is not None
                target = "rebuttal" if row["review_cycle"] < contract.rebuttal_rounds else "reporting"
            elif row["state"] == "rebuttal":
                cycle = row["review_cycle"] + 1
                if any(self.db.execute(
                    "SELECT 1 FROM rebuttals WHERE idea_id = ? AND cycle = ?", (idea["id"], cycle),
                ).fetchone() is None for idea in ideas):
                    raise WorkflowError("expert responses are incomplete")
                self.db.execute("UPDATE rounds SET review_cycle = ? WHERE id = ?", (cycle, round_id))
                target = "reviews"
            else:
                raise WorkflowError("this phase cannot advance through discussion")
            self._state(round_id, target)
            return target

    def reviews_complete(self, idea_id: int, cycle: int, *, version: int | None = None) -> bool:
        idea = self.idea(idea_id)
        _row, contract = self.context(idea["round_id"])
        required = {role.role_id for role in contract.roles or () if role.kind == "reviewer"}
        submitted = {
            item["reviewer"] for item in self.db.execute(
                "SELECT reviewer FROM reviews WHERE idea_id = ? AND proposal_version = ? AND cycle = ?",
                (idea_id, idea["version"] if version is None else version, cycle),
            )
        }
        return bool(required) and required == submitted

    def summary(self, idea_id: int, cycle: int) -> dict[str, object]:
        idea = self.idea(idea_id)
        reviews = [
            Review.from_value(json.loads(row["body"])) for row in self.db.execute(
                "SELECT body FROM reviews WHERE idea_id = ? AND proposal_version = ? AND cycle = ? ORDER BY reviewer",
                (idea_id, idea["version"], cycle),
            )
        ]
        complete = self.reviews_complete(idea_id, cycle)
        scores = [review.recommendation for review in reviews]
        return {
            "complete": complete,
            "mean": sum(scores) / len(scores) if complete else None,
            "range": [min(scores), max(scores)] if scores else None,
            "disagreement": bool(scores and max(scores) - min(scores) >= 2),
            "blocking_issues": [review.blocking_issue for review in reviews if review.blocking_issue],
        }

    def view(self, round_id: int, role_id: str, *, idea_id: int | None = None) -> dict[str, object]:
        row, contract = self.context(round_id)
        role = self.role(contract, role_id, {item.kind for item in contract.roles or ()})
        if role.kind in {"researcher", "distiller"} and row["state"] != "prepared":
            raise WorkflowError("preparation roles do not receive private discussion submissions")
        if role.kind in {"engineer", "system-engineer", "supervisor", "project-manager"}:
            if row["state"] not in {"reporting", "comment_window", "released", "completed"}:
                raise WorkflowError("execution roles do not receive private discussion submissions")
        ideas = self.db.execute("SELECT * FROM ideas WHERE round_id = ? ORDER BY id", (round_id,)).fetchall()
        cycle_locked = row["state"] in {"rebuttal", "reporting", "comment_window", "released", "completed"} or (
            row["state"] == "reviews" and all(
                self.reviews_complete(idea["id"], row["review_cycle"]) for idea in ideas
            )
        )
        if role.kind in {"expert", "co-optimization-expert"}:
            ideas = [idea for idea in ideas if idea["owner"] == role_id]
        if idea_id is not None:
            if not any(idea["id"] == idea_id for idea in ideas):
                raise WorkflowError("idea is outside this role's visible scope")
            ideas = [idea for idea in ideas if idea["id"] == idea_id]
        result: list[dict[str, object]] = []
        for idea in ideas:
            proposal = self.db.execute(
                "SELECT body, origin FROM proposals WHERE idea_id = ? AND version = ?",
                (idea["id"], idea["version"]),
            ).fetchone()
            visible_reviews = []
            for review in self.db.execute(
                "SELECT * FROM reviews WHERE idea_id = ? AND cycle = ? ORDER BY reviewer",
                (idea["id"], row["review_cycle"]),
            ):
                locked = self.reviews_complete(
                    idea["id"], row["review_cycle"], version=review["proposal_version"],
                )
                if role.kind == "reviewer":
                    locked = cycle_locked
                if role.kind == "moderator" or locked or review["reviewer"] == role_id:
                    visible_reviews.append({
                        "reviewer": review["reviewer"], "proposal_version": review["proposal_version"],
                        "body": json.loads(review["body"]),
                    })
            result.append({
                "idea_id": idea["id"], "version": idea["version"], "owner": idea["owner"],
                "proposal": json.loads(proposal["body"]), "origin": proposal["origin"],
                "reviews": visible_reviews,
            })
        public_contract = contract.to_dict()
        public_contract["roles"] = [
            {"role_id": item.role_id, "kind": item.kind} for item in contract.roles or ()
        ]
        return {
            "contract": public_contract, "role": role.kind, "phase": row["state"],
            "cycle": row["review_cycle"], "ideas": result,
            "directives": self.ledger.directives(round_id, role_id),
            "notice": "operator-imported protocol records; no live agents or OS isolation have been verified",
        }

    def prompt(self, round_id: int, role_id: str, *, idea_id: int | None = None) -> dict[str, object]:
        row, contract = self.context(round_id)
        role = self.role(contract, role_id, {item.kind for item in contract.roles or ()})
        if role.kind in {"expert", "co-optimization-expert"} and role.instruction is None:
            raise WorkflowError("specialist instruction must be distilled from research before invoking this expert")
        if role.kind == "reviewer" and idea_id is None:
            raise WorkflowError("reviewer invocation requires an explicit idea scope")
        view = self.view(round_id, role_id, idea_id=idea_id)
        static = "\n\n".join([
            "Report verified facts, inference, expectations, not-run, failure and unknown distinctly. "
            "Never fabricate sources, operations, metrics or user decisions. Preserve necessary failure evidence. "
            "Do not modify authority or evaluation. Actual tool and data permissions are owned by the trusted runner.",
            ROLE_DUTIES[role.kind],
            role.instruction or "",
            "Knowledge references, read on demand:\n" + "\n".join(role.knowledge_refs),
            "Canonical shared contract:\n" + json.dumps(view.pop("contract"), ensure_ascii=False, sort_keys=True),
        ])
        return {
            "context_revision": (
                f"contract-{row['contract_revision']}:role-{role_id}-{role.revision}:idea-{idea_id or 'new'}"
            ),
            "static": static, "delta": json.dumps(view, ensure_ascii=False, sort_keys=True),
            "provider_session": None,
            "notice": "prompt assembly only; no provider session or cache hit is asserted",
        }
