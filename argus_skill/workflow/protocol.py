"""Small actionable schemas; host identities and authority are not model output."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from .contract import WorkflowError, integer, object_fields, optional_text, text, text_list

SCORE_WEIGHTS = {"benefit": 30, "feasibility": 25, "evidence": 20, "system": 15, "cost": 10}
USD_UNITS = 1_000_000


def number(value: object, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkflowError(f"{label} must be a finite number >= {minimum}")
    try:
        result = float(value)
    except OverflowError as exc:
        raise WorkflowError(f"{label} is outside the finite numeric range") from exc
    if not math.isfinite(result) or result < minimum:
        raise WorkflowError(f"{label} must be a finite number >= {minimum}")
    return result


def money_units(value: object, *, allowance: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise WorkflowError("amount must be a finite non-negative USD value")
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise WorkflowError("amount must be a finite non-negative USD value")
        units = int((amount * USD_UNITS).to_integral_value(rounding=ROUND_FLOOR if allowance else ROUND_CEILING))
    except InvalidOperation as exc:
        raise WorkflowError("invalid USD amount") from exc
    if units > 2 ** 63 - 1:
        raise WorkflowError("amount exceeds the ledger's integer range")
    return units


@dataclass(frozen=True)
class Proposal:
    title: str
    problem: str
    hypothesis: str
    mechanism: str
    expected_benefit: str
    conditions: str
    minimum_experiment: str
    failure_condition: str
    implementation: str
    resources: str
    evidence_refs: tuple[str, ...]
    interfaces: tuple[str, ...]
    uncertainties: tuple[str, ...]
    innovation: str

    @classmethod
    def from_value(cls, value: object) -> Proposal:
        payload = object_fields(value, {item.name for item in fields(cls)}, "proposal")
        innovation = text(payload.get("innovation"), "innovation")
        if innovation not in {"single", "joint"}:
            raise WorkflowError("innovation must be single or joint")
        interfaces = text_list(payload.get("interfaces"), "interfaces")
        if innovation == "joint" and not interfaces:
            raise WorkflowError("joint innovation requires explicit common interfaces")
        return cls(
            title=text(payload.get("title"), "title"),
            problem=text(payload.get("problem"), "problem"),
            hypothesis=text(payload.get("hypothesis"), "hypothesis"),
            mechanism=text(payload.get("mechanism"), "mechanism"),
            expected_benefit=text(payload.get("expected_benefit"), "expected_benefit"),
            conditions=text(payload.get("conditions"), "conditions"),
            minimum_experiment=text(payload.get("minimum_experiment"), "minimum_experiment"),
            failure_condition=text(payload.get("failure_condition"), "failure_condition"),
            implementation=text(payload.get("implementation"), "implementation"),
            resources=text(payload.get("resources"), "resources"),
            evidence_refs=text_list(payload.get("evidence_refs"), "evidence_refs"),
            interfaces=interfaces,
            uncertainties=text_list(payload.get("uncertainties"), "uncertainties"),
            innovation=innovation,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Review:
    scores: dict[str, float]
    value: str
    risk: str
    action: str
    blocking_issue: str | None = None

    @classmethod
    def from_value(cls, value: object) -> Review:
        payload = object_fields(value, {item.name for item in fields(cls)}, "review")
        raw_scores = object_fields(payload.get("scores"), set(SCORE_WEIGHTS), "scores")
        scores = {name: number(raw_scores.get(name), name) for name in SCORE_WEIGHTS}
        if any(score > 5 or score * 2 != int(score * 2) for score in scores.values()):
            raise WorkflowError("scores must be 0..5 in half-point steps")
        return cls(
            scores=scores, value=text(payload.get("value"), "value"),
            risk=text(payload.get("risk"), "risk"), action=text(payload.get("action"), "action"),
            blocking_issue=optional_text(payload.get("blocking_issue"), "blocking_issue"),
        )

    @property
    def recommendation(self) -> float:
        return sum(self.scores[name] * weight for name, weight in SCORE_WEIGHTS.items()) / 100

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Measurement:
    baseline_ref: str
    candidate_ref: str
    protocol_ref: str
    raw_evidence_ref: str
    correctness: str
    direction: str
    baseline: float
    candidate: float
    observations: int
    required_observations: int
    noise_limit: float
    regression_limit: float
    interference: str | None = None

    @classmethod
    def from_value(cls, value: object) -> Measurement:
        payload = object_fields(value, {item.name for item in fields(cls)}, "measurement")
        correctness = text(payload.get("correctness"), "correctness")
        direction = text(payload.get("direction"), "direction")
        if correctness not in {"passed", "failed", "not_run", "unknown"}:
            raise WorkflowError("unknown correctness status")
        if direction not in {"minimize", "maximize"}:
            raise WorkflowError("direction must be minimize or maximize")
        return cls(
            baseline_ref=text(payload.get("baseline_ref"), "baseline_ref"),
            candidate_ref=text(payload.get("candidate_ref"), "candidate_ref"),
            protocol_ref=text(payload.get("protocol_ref"), "protocol_ref"),
            raw_evidence_ref=text(payload.get("raw_evidence_ref"), "raw_evidence_ref"),
            correctness=correctness, direction=direction,
            baseline=number(payload.get("baseline"), "baseline"),
            candidate=number(payload.get("candidate"), "candidate"),
            observations=integer(payload.get("observations"), "observations", minimum=0),
            required_observations=integer(payload.get("required_observations"), "required_observations"),
            noise_limit=number(payload.get("noise_limit"), "noise_limit"),
            regression_limit=number(payload.get("regression_limit"), "regression_limit"),
            interference=optional_text(payload.get("interference"), "interference"),
        )

    def verdict(self) -> str:
        if (
            self.correctness != "passed" or self.interference is not None
            or self.observations < self.required_observations
        ):
            return "invalid"
        delta = self.candidate - self.baseline
        if self.direction == "maximize":
            delta = -delta
        if delta > max(self.noise_limit, self.regression_limit):
            return "regressed"
        if -delta > self.noise_limit:
            return "improved"
        return "unchanged"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
