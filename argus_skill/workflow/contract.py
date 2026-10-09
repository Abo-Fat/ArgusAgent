"""Operator-owned research contracts and declared preparation requirements."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Literal

from ..core.secret_guard import redact_secrets_record, redact_secrets_text


class WorkflowError(ValueError):
    """An explicit workflow input, authority, or state error."""


ROLE_KINDS = frozenset({
    "moderator", "expert", "co-optimization-expert", "reviewer",
    "project-manager", "engineer", "system-engineer", "supervisor",
    "researcher", "distiller",
})


def object_fields(value: object, names: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise WorkflowError(f"{label} must be a JSON object")
    unknown = set(value) - names
    if unknown:
        raise WorkflowError(f"unknown {label} fields: {', '.join(sorted(unknown))}")
    result: dict[str, object] = dict(value)
    if redact_secrets_record(result, redact_ambiguous_record_keys=False) != result:
        raise WorkflowError(f"{label} contains a credential; use an external credential store")
    return result


def text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be a non-empty string")
    result = value.strip()
    if redact_secrets_text(result, redact_ambiguous_record_keys=False) != result:
        raise WorkflowError(f"{label} contains a credential; use an external credential store")
    return result


def optional_text(value: object, label: str) -> str | None:
    return None if value is None else text(value, label)


def integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise WorkflowError(f"{label} must be an integer >= {minimum}")
    return value


def optional_integer(value: object, label: str, *, minimum: int = 1) -> int | None:
    return None if value is None else integer(value, label, minimum=minimum)


def text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise WorkflowError(f"{label} must be an array of strings")
    return tuple(text(item, label) for item in value)


def optional_text_list(value: object, label: str) -> tuple[str, ...] | None:
    return None if value is None else text_list(value, label)


def utc_deadline(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorkflowError("deadline_utc must be an ISO timestamp with a timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise WorkflowError("deadline_utc must include a timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True)
class Role:
    role_id: str
    kind: str
    revision: int = 1
    instruction: str | None = None
    knowledge_refs: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: object) -> Role:
        payload = object_fields(value, {item.name for item in fields(cls)}, "role")
        role_id = text(payload.get("role_id"), "role_id")
        if role_id == "all":
            raise WorkflowError("role_id 'all' is reserved for directive scope")
        kind = text(payload.get("kind"), "role kind")
        if kind not in ROLE_KINDS:
            raise WorkflowError(f"unknown role kind: {kind}")
        return cls(
            role_id=role_id, kind=kind,
            revision=integer(payload.get("revision", 1), "role revision"),
            instruction=optional_text(payload.get("instruction"), "role instruction"),
            knowledge_refs=text_list(payload.get("knowledge_refs", []), "knowledge_refs"),
        )


@dataclass(frozen=True)
class StorageLimits:
    workspace_bytes: int
    workspace_files: int
    log_bytes: int
    checkpoint_bytes: int
    archive_bytes: int

    @classmethod
    def from_value(cls, value: object) -> StorageLimits:
        payload = object_fields(value, {item.name for item in fields(cls)}, "storage_limits")
        return cls(
            workspace_bytes=integer(payload.get("workspace_bytes"), "workspace_bytes"),
            workspace_files=integer(payload.get("workspace_files"), "workspace_files"),
            log_bytes=integer(payload.get("log_bytes"), "log_bytes"),
            checkpoint_bytes=integer(payload.get("checkpoint_bytes"), "checkpoint_bytes", minimum=0),
            archive_bytes=integer(payload.get("archive_bytes"), "archive_bytes"),
        )


@dataclass(frozen=True)
class ResearchContract:
    objective: str
    backend: Literal["copilot"] = "copilot"
    non_goals: tuple[str, ...] = ()
    model: str | None = None
    rebuttal_rounds: int | None = None
    max_ideas_per_expert: int | None = None
    total_model_budget_usd: float | None = None
    max_active_attempts: int | None = None
    deadline_utc: str | None = None
    compute_scope: tuple[str, ...] | None = None
    allowed_services: tuple[str, ...] | None = None
    data_scope: tuple[str, ...] | None = None
    baseline_ref: str | None = None
    evaluation_protocol: str | None = None
    roles: tuple[Role, ...] | None = None
    storage_limits: StorageLimits | None = None

    @classmethod
    def from_value(cls, value: object) -> ResearchContract:
        payload = object_fields(value, {item.name for item in fields(cls)}, "contract")
        if payload.get("backend", "copilot") != "copilot":
            raise WorkflowError("this workflow's selected backend is copilot")
        raw_budget = payload.get("total_model_budget_usd")
        budget: float | None = None
        if raw_budget is not None:
            if isinstance(raw_budget, bool) or not isinstance(raw_budget, (int, float)):
                raise WorkflowError("total_model_budget_usd must be a finite non-negative number")
            try:
                budget = float(raw_budget)
            except OverflowError as exc:
                raise WorkflowError("total_model_budget_usd is outside the finite numeric range") from exc
            if not math.isfinite(budget) or budget < 0:
                raise WorkflowError("total_model_budget_usd must be a finite non-negative number")
        deadline = optional_text(payload.get("deadline_utc"), "deadline_utc")
        if deadline is not None:
            deadline = utc_deadline(deadline).isoformat()
        raw_roles = payload.get("roles")
        roles: tuple[Role, ...] | None = None
        if raw_roles is not None:
            if not isinstance(raw_roles, (list, tuple)):
                raise WorkflowError("roles must be an array")
            roles = tuple(Role.from_value(item) for item in raw_roles)
            if len({role.role_id for role in roles}) != len(roles):
                raise WorkflowError("role_id values must be unique")
        raw_storage = payload.get("storage_limits")
        return cls(
            objective=text(payload.get("objective"), "objective"),
            non_goals=text_list(payload.get("non_goals", []), "non_goals"),
            model=optional_text(payload.get("model"), "model"),
            rebuttal_rounds=optional_integer(
                payload.get("rebuttal_rounds"), "rebuttal_rounds", minimum=0,
            ),
            max_ideas_per_expert=optional_integer(
                payload.get("max_ideas_per_expert"), "max_ideas_per_expert",
            ),
            total_model_budget_usd=budget,
            max_active_attempts=optional_integer(
                payload.get("max_active_attempts"), "max_active_attempts",
            ),
            deadline_utc=deadline,
            compute_scope=optional_text_list(payload.get("compute_scope"), "compute_scope"),
            allowed_services=optional_text_list(
                payload.get("allowed_services"), "allowed_services",
            ),
            data_scope=optional_text_list(payload.get("data_scope"), "data_scope"),
            baseline_ref=optional_text(payload.get("baseline_ref"), "baseline_ref"),
            evaluation_protocol=optional_text(
                payload.get("evaluation_protocol"), "evaluation_protocol",
            ),
            roles=roles,
            storage_limits=None if raw_storage is None else StorageLimits.from_value(raw_storage),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def preparation_blockers(self, now: datetime) -> list[str]:
        """Check declarations only; they do not prove runnable or safe infrastructure."""
        blockers: list[str] = []
        for name in (
            "rebuttal_rounds", "total_model_budget_usd", "max_active_attempts",
            "deadline_utc", "allowed_services", "data_scope", "storage_limits",
        ):
            if getattr(self, name) is None:
                blockers.append(f"missing {name}")
        if not self.compute_scope:
            blockers.append("missing compute_scope")
        if not self.baseline_ref:
            blockers.append("missing baseline_ref")
        if not self.evaluation_protocol:
            blockers.append("missing evaluation_protocol")
        if self.deadline_utc and utc_deadline(self.deadline_utc) <= now:
            blockers.append("deadline has expired")
        if self.total_model_budget_usd == 0:
            blockers.append("model budget has no available allowance")
        kinds = {role.kind for role in self.roles or ()}
        for kind in sorted({
            "moderator", "expert", "co-optimization-expert",
            "reviewer", "project-manager", "engineer", "supervisor",
        } - kinds):
            blockers.append(f"missing role kind: {kind}")
        return blockers

    def research_blockers(self, now: datetime) -> list[str]:
        blockers = [
            f"missing {name}" for name in (
                "total_model_budget_usd", "max_active_attempts", "deadline_utc",
                "allowed_services", "data_scope", "storage_limits",
            ) if getattr(self, name) is None
        ]
        if self.total_model_budget_usd == 0:
            blockers.append("model budget has no available allowance")
        if self.deadline_utc and utc_deadline(self.deadline_utc) <= now:
            blockers.append("deadline has expired")
        return blockers

    def discussion_blockers(self, now: datetime) -> list[str]:
        blockers = self.research_blockers(now)
        if self.rebuttal_rounds is None:
            blockers.append("missing rebuttal_rounds")
        if self.max_ideas_per_expert is None:
            blockers.append("missing max_ideas_per_expert")
        kinds = {role.kind for role in self.roles or ()}
        for kind in sorted({"moderator", "expert", "co-optimization-expert", "reviewer"} - kinds):
            blockers.append(f"missing role kind: {kind}")
        return blockers
