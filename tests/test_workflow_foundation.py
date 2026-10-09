from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from argus_skill.workflow.contract import ResearchContract, WorkflowError
from argus_skill.workflow.controller import Controller
from argus_skill.workflow.discussion import Discussion
from argus_skill.workflow.jobs import JobQueue
from argus_skill.workflow.protocol import SCORE_WEIGHTS, Measurement, Proposal, Review, money_units
from argus_skill.workflow.reports import Reports
from argus_skill.workflow.store import _SCHEMA, DATABASE_NAME, Ledger, _json

REPO_ROOT = Path(__file__).resolve().parents[1]


def complete_contract() -> ResearchContract:
    return ResearchContract.from_value({
        "objective": "Improve edge LLM inference efficiency",
        "rebuttal_rounds": 0,
        "total_model_budget_usd": 1,
        "max_active_attempts": 1,
        "deadline_utc": "2099-01-01T00:00:00Z",
        "compute_scope": ["explicitly-declared-test-device"],
        "allowed_services": [],
        "data_scope": [],
        "baseline_ref": "test-baseline",
        "evaluation_protocol": "test-protocol",
        "roles": [
            {"role_id": kind, "kind": kind}
            for kind in (
                "moderator", "expert", "co-optimization-expert", "reviewer",
                "project-manager", "engineer", "supervisor",
            )
        ],
        "storage_limits": {
            "workspace_bytes": 1024, "workspace_files": 10, "log_bytes": 1024,
            "checkpoint_bytes": 0, "archive_bytes": 1024,
        },
    })


def cli(state: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "argus_skill.workflow", "--state", str(state), *arguments],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=False,
    )


@pytest.mark.parametrize("payload", [
    {"objective": ""},
    {"objective": "research", "workflow_mode": "direct"},
    {"objective": "research", "backend": "codex"},
    {"objective": "research", "rebuttal_rounds": True},
    {"objective": "research", "total_model_budget_usd": float("nan")},
    {"objective": "research", "total_model_budget_usd": -1},
    {"objective": "research", "total_model_budget_usd": 10 ** 1000},
    {"objective": "research", "deadline_utc": "2099-01-01"},
    {"objective": "research", "roles": [{"role_id": "x", "kind": "planner"}]},
    {"objective": "research", "roles": [{"role_id": "all", "kind": "expert"}]},
    {"objective": "api_key=credential-example-123"},
])
def test_invalid_contracts_fail_explicitly(payload: dict[str, object]) -> None:
    with pytest.raises(WorkflowError):
        ResearchContract.from_value(payload)


def test_missing_requirements_never_authorize_execution(tmp_path: Path) -> None:
    contract = ResearchContract(objective="Improve edge LLM inference efficiency")
    with Ledger.create(tmp_path / "state", contract) as ledger:
        round_id = ledger.create_round()
        status = ledger.round_status(round_id)
        assert "missing rebuttal_rounds" in status["preparation_blockers"]
        assert "missing total_model_budget_usd" in status["preparation_blockers"]
        assert "missing baseline_ref" in status["preparation_blockers"]
        assert status["execution_enabled"] is False
        with pytest.raises(WorkflowError, match="preparation blocked"):
            ledger.transition(round_id, "prepare", expected_revision=1)
        assert ledger.round_status(round_id)["state"] == "draft"
    with Ledger(tmp_path / "state" / DATABASE_NAME) as reopened:
        assert reopened.round_status(round_id) == status


def test_declared_setup_is_not_runtime_verification(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", complete_contract()) as ledger:
        round_id = ledger.create_round()
        prepared = ledger.transition(round_id, "prepare", expected_revision=1)
        assert prepared["state"] == "prepared"
        assert prepared["preparation_blockers"] == []
        assert prepared["execution_enabled"] is False
        assert prepared["execution_blockers"]
        paused = ledger.transition(round_id, "pause", expected_revision=2)
        assert paused["state"] == "paused"
        assert ledger.transition(round_id, "resume", expected_revision=3)["state"] == "prepared"
        ledger.transition(round_id, "cancel", expected_revision=4)
        with pytest.raises(WorkflowError, match="cannot be resumed"):
            ledger.transition(round_id, "resume", expected_revision=5)


def test_contract_revision_invalidates_old_preparation(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with Ledger.create(root, complete_contract()) as ledger, Ledger(root / DATABASE_NAME) as other:
        round_id = ledger.create_round()
        ledger.transition(round_id, "prepare", expected_revision=1)
        revised = complete_contract().to_dict()
        revised["objective"] = "A changed operator objective"
        assert other.revise_contract(ResearchContract.from_value(revised), expected_revision=1) == 2
        assert ledger.round_status(round_id)["state"] == "draft"
        assert ledger.round_status(round_id)["contract_revision"] == 1
        with pytest.raises(WorkflowError, match="older contract"):
            ledger.transition(round_id, "prepare", expected_revision=3)
        with pytest.raises(WorkflowError, match="stale contract"):
            ledger.revise_contract(complete_contract(), expected_revision=1)
        new_round = ledger.create_round()
        assert ledger.round_status(new_round)["contract_revision"] == 2
        assert len(ledger.events()) == 5


def test_stale_round_actions_do_not_change_state(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", complete_contract()) as ledger:
        round_id = ledger.create_round()
        ledger.transition(round_id, "pause", expected_revision=1)
        with pytest.raises(WorkflowError, match="stale round"):
            ledger.transition(round_id, "cancel", expected_revision=1)
        assert ledger.round_status(round_id)["state"] == "paused"


def test_standing_directives_are_scoped_deduplicated_and_revocable(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", complete_contract()) as ledger:
        first = ledger.create_round()
        second = ledger.create_round()
        global_rule = ledger.add_directive("Keep private inputs local")
        assert ledger.add_directive(" Keep private inputs local ") == global_rule
        local_rule = ledger.add_directive("Keep private inputs local", round_id=first)
        expert_rule = ledger.add_directive("Inspect the baseline", role_id="expert", round_id=first)
        assert len(ledger.directives(first, "expert")) == 2
        assert [item["id"] for item in ledger.directives(first, "reviewer")] == [local_rule]
        assert [item["id"] for item in ledger.directives(second, "expert")] == [global_rule]
        ledger.revoke_directive(local_rule)
        assert {item["id"] for item in ledger.directives(first, "expert")} == {global_rule, expert_rule}
        ledger.revoke_directive(global_rule)
        assert ledger.directives(second, "expert") == []
        with pytest.raises(WorkflowError, match="not in the contract"):
            ledger.directives(first, "other-expert")
        with pytest.raises(WorkflowError, match="credential"):
            ledger.add_directive("Use api_key=credential-example-123")


def test_backup_restores_contract_history_and_references(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", complete_contract()) as ledger:
        round_id = ledger.create_round()
        ledger.add_directive("Check the raw evidence", round_id=round_id)
        ledger.transition(round_id, "pause", expected_revision=1)
        snapshot = ledger.events()
        backup = ledger.backup(tmp_path / "snapshot.sqlite3")
        ledger.transition(round_id, "cancel", expected_revision=2)
        with pytest.raises(FileExistsError):
            ledger.backup(backup)
    with Ledger.restore(backup, tmp_path / "restored") as restored:
        assert restored.events() == snapshot
        assert restored.round_status(round_id)["state"] == "paused"
        assert restored.directives(round_id, "expert")[0]["text"] == "Check the raw evidence"
        assert restored.create_round() == round_id + 1
        assert [item["id"] for item in restored.events(after=2, limit=2)] == [3, 4]
    with pytest.raises(WorkflowError, match="never overwritten"):
        Ledger.restore(backup, tmp_path / "restored")


def test_legacy_and_invalid_backup_are_not_adopted(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    old_data = legacy / "backlog.jsonl"
    old_data.write_text("old-state", encoding="utf-8")
    with pytest.raises(WorkflowError, match="legacy state is not imported"):
        Ledger.create(legacy, complete_contract())
    assert old_data.read_text(encoding="utf-8") == "old-state"
    assert not (legacy / DATABASE_NAME).exists()
    invalid = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(WorkflowError, match="unsupported"):
        Ledger.restore(invalid, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_rejects_broken_references(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", complete_contract()) as ledger:
        backup = ledger.backup(tmp_path / "snapshot.sqlite3")
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "INSERT INTO rounds(contract_revision, state, created_at) VALUES (999, 'draft', 'test')",
        )
    with pytest.raises(WorkflowError, match="references are incomplete"):
        Ledger.restore(backup, tmp_path / "restored")


def test_cli_real_entrypoint_persists_and_reports_blockers(tmp_path: Path) -> None:
    state = tmp_path / "state"
    created = cli(state, "init", "--objective", "Improve edge LLM inference efficiency")
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["contract"]["backend"] == "copilot"
    round_result = cli(state, "round", "create")
    assert round_result.returncode == 0, round_result.stderr
    assert json.loads(round_result.stdout)["state"] == "draft"
    blocked = cli(state, "round", "prepare", "1", "--expected-revision", "1")
    assert blocked.returncode == 2
    assert "preparation blocked" in blocked.stderr
    status = cli(state, "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["execution_enabled"] is False
    backup = tmp_path / "snapshot.sqlite3"
    assert cli(state, "backup", "--output", str(backup)).returncode == 0
    restored = cli(tmp_path / "restored", "restore", "--backup", str(backup))
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout)["rounds"][0]["state"] == "draft"


def test_readonly_cli_does_not_initialize_missing_state(tmp_path: Path) -> None:
    state = tmp_path / "missing"
    result = cli(state, "status")
    assert result.returncode == 2
    assert not state.exists()
    result = cli(state, "init", "--objective", "api_key=credential-example-123")
    assert result.returncode == 2
    assert "credential-example-123" not in result.stderr
    assert not state.exists()


def test_package_does_not_eagerly_import_old_orchestration() -> None:
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, argus_skill; "
         "assert 'argus_skill.loop' not in sys.modules; "
         "from argus_skill import RunnerOptions; "
         "assert RunnerOptions().dangerous_yolo is False; "
         "assert 'argus_skill.loop' not in sys.modules"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def portable_contract(rounds: int = 0) -> ResearchContract:
    payload = complete_contract().to_dict()
    payload["max_ideas_per_expert"] = 1
    payload["rebuttal_rounds"] = rounds
    payload["total_model_budget_usd"] = 2
    payload["roles"] = [
        {"role_id": kind, "kind": kind, "instruction": f"Documented test-only method for {kind}"}
        for kind in (
            "moderator", "expert", "co-optimization-expert", "reviewer",
            "project-manager", "engineer", "supervisor",
        )
    ] + [{"role_id": "second-reviewer", "kind": "reviewer"}]
    payload["storage_limits"] = {
        "workspace_bytes": 1024, "workspace_files": 10, "log_bytes": 1024,
        "checkpoint_bytes": 0, "archive_bytes": 1_000_000,
    }
    return ResearchContract.from_value(payload)


def proposal(joint: bool = False) -> Proposal:
    return Proposal.from_value({
        "title": "Synthetic protocol fixture",
        "problem": "No deployment baseline exists",
        "hypothesis": "A mechanism is worth a bounded test",
        "mechanism": "Test-only mechanism",
        "expected_benefit": "Unmeasured",
        "conditions": "Require an actual deployment before claiming performance",
        "minimum_experiment": "Compare identical inputs and devices",
        "failure_condition": "Correctness failure or trusted regression",
        "implementation": "Implement only after confirmation",
        "resources": "Within the declared budget",
        "evidence_refs": [], "interfaces": ["joint fixture interface"] if joint else [],
        "uncertainties": ["No actual inference experiment was performed"],
        "innovation": "joint" if joint else "single",
    })


def review(score: float = 3, blocker: str | None = None) -> Review:
    return Review.from_value({
        "scores": dict.fromkeys(SCORE_WEIGHTS, score),
        "value": "Testable mechanism", "risk": "Synthetic inputs",
        "action": "Run independently after authorization", "blocking_issue": blocker,
    })


def discussion_round(ledger: Ledger) -> tuple[int, list[int]]:
    round_id = ledger.create_round()
    ledger.transition(round_id, "prepare", expected_revision=1)
    discussion = Discussion(ledger)
    discussion.begin(round_id)
    ideas = [
        discussion.propose(round_id, "expert", proposal()),
        discussion.propose(round_id, "co-optimization-expert", proposal(True)),
    ]
    return round_id, ideas


def finish_reviews(ledger: Ledger, round_id: int, ideas: list[int], blocker: str | None = None) -> None:
    discussion = Discussion(ledger)
    discussion.advance(round_id)
    for idea_id in ideas:
        version = discussion.idea(idea_id)["version"]
        for reviewer in ("reviewer", "second-reviewer"):
            discussion.review(idea_id, reviewer, review(blocker=blocker), expected_version=version)
    discussion.advance(round_id)


def delivered_round(ledger: Ledger) -> tuple[int, list[int], int, datetime]:
    round_id, ideas = discussion_round(ledger)
    finish_reviews(ledger, round_id, ideas)
    reports = Reports(ledger)
    report_id = reports.generate(round_id)["report_id"]
    assert isinstance(report_id, int)
    now = datetime.now(UTC)
    reports.deliver(report_id, lambda message: None, channel="test-only-notification", now=now)
    return round_id, ideas, report_id, now


def released_round(ledger: Ledger) -> tuple[int, list[int]]:
    round_id, ideas, report_id, now = delivered_round(ledger)
    assert Reports(ledger).release(report_id, explicit=False, now=now + timedelta(minutes=20)) == ideas
    return round_id, ideas


def assigned_task(ledger: Ledger, idea_id: int, workspace: Path) -> int:
    workspace.mkdir(exist_ok=True)
    queue = JobQueue(ledger)
    queue.confirm(idea_id, Discussion(ledger).idea(idea_id)["owner"], "engineer", "Test-only technical recap")
    return queue.assign(
        idea_id, manager="project-manager", engineer="engineer", title="Fixture task",
        objective="Test the portable protocol, not LLM inference",
        workspace=workspace,
        evaluation_rule={
            "direction": "minimize", "required_observations": 3,
            "noise_limit": 0.1, "regression_limit": 0.5,
        },
    )


def test_explicit_migration_preserves_version_one_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / DATABASE_NAME
    with sqlite3.connect(database) as connection:
        connection.executescript(_SCHEMA)
        connection.execute("INSERT INTO contracts VALUES (1, ?, 'test')", (_json(complete_contract().to_dict()),))
        connection.execute("INSERT INTO rounds VALUES (1, 1, 'draft', 1, 'test')")
        connection.execute("INSERT INTO directives VALUES (1, 'Keep evidence', 1, 'all', 1, 'test')")
    with pytest.raises(WorkflowError, match="explicit migrate"):
        Ledger(database)
    Ledger.migrate(database)
    with Ledger(database) as ledger:
        assert ledger.round_status(1)["state"] == "draft"
        assert ledger.directives(1, "expert")[0]["text"] == "Keep evidence"
        assert ledger.status()["schema_version"] == 2
    Ledger.migrate(database)


def test_independent_submissions_and_cycle_locking(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas = discussion_round(ledger)
        discussion = Discussion(ledger)
        assert [item["idea_id"] for item in discussion.view(round_id, "expert")["ideas"]] == [ideas[0]]
        assert [item["idea_id"] for item in discussion.view(round_id, "co-optimization-expert")["ideas"]] == [ideas[1]]
        with pytest.raises(WorkflowError, match="private discussion"):
            discussion.view(round_id, "project-manager")
        with pytest.raises(WorkflowError, match="allowance"):
            discussion.propose(round_id, "expert", proposal())
        discussion.advance(round_id)
        discussion.review(ideas[0], "reviewer", review(2), expected_version=1)
        discussion.review(ideas[0], "second-reviewer", review(4), expected_version=1)
        own_view = discussion.view(round_id, "reviewer")
        assert [item["reviewer"] for item in own_view["ideas"][0]["reviews"]] == ["reviewer"]
        with pytest.raises(WorkflowError, match="incomplete"):
            discussion.advance(round_id)
        for reviewer in ("reviewer", "second-reviewer"):
            discussion.review(ideas[1], reviewer, review(), expected_version=1)
        assert len(discussion.view(round_id, "reviewer")["ideas"][0]["reviews"]) == 2
        assert discussion.summary(ideas[0], 0)["disagreement"] is True
        assert discussion.advance(round_id) == "reporting"


@pytest.mark.parametrize("rounds", [0, 1, 2])
def test_exact_rebuttal_rounds_including_unchanged_proposals(tmp_path: Path, rounds: int) -> None:
    with Ledger.create(tmp_path / "state", portable_contract(rounds)) as ledger:
        round_id, ideas = discussion_round(ledger)
        discussion = Discussion(ledger)
        finish_reviews(ledger, round_id, ideas)
        for _cycle in range(rounds):
            assert ledger.round_status(round_id)["state"] == "rebuttal"
            for idea_id in ideas:
                discussion.rebut(
                    idea_id, discussion.idea(idea_id)["owner"], "Responded to every review",
                    "No mechanism change; all uncertainty retained", expected_version=1,
                )
            discussion.advance(round_id)
            for idea_id in ideas:
                for reviewer in ("reviewer", "second-reviewer"):
                    discussion.review(idea_id, reviewer, review(), expected_version=1)
            discussion.advance(round_id)
        assert ledger.round_status(round_id)["state"] == "reporting"
        assert ledger.round_status(round_id)["review_cycle"] == rounds
        count = ledger.connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        assert count == 4 * (rounds + 1)
        with pytest.raises(WorkflowError, match="cannot advance"):
            discussion.advance(round_id)


def test_prompt_static_delta_and_private_role_instructions(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas = discussion_round(ledger)
        discussion = Discussion(ledger)
        first = discussion.prompt(round_id, "expert", idea_id=ideas[0])
        assert "Documented test-only method for co-optimization-expert" not in first["static"]
        assert "Test-only mechanism" not in first["static"]
        discussion.advance(round_id)
        second = discussion.prompt(round_id, "expert", idea_id=ideas[0])
        assert first["static"] == second["static"]
        assert first["delta"] != second["delta"]
        with pytest.raises(WorkflowError, match="explicit idea scope"):
            discussion.prompt(round_id, "reviewer")
        with pytest.raises(WorkflowError, match="visible scope"):
            discussion.prompt(round_id, "expert", idea_id=ideas[1])


@pytest.mark.parametrize("score", [-0.5, 5.5, 0.2, True, float("nan")])
def test_common_scoring_rejects_invalid_values(score: float) -> None:
    with pytest.raises(WorkflowError):
        review(score)


def test_report_notification_failure_and_exact_timeout(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas = discussion_round(ledger)
        finish_reviews(ledger, round_id, ideas)
        reports = Reports(ledger)
        report_id = reports.generate(round_id)["report_id"]

        def failed_notification(message: str) -> None:
            raise OSError("channel failed")

        with pytest.raises(OSError, match="channel failed"):
            reports.deliver(report_id, failed_notification, channel="test")
        assert reports.report(report_id)["submitted_at"] is None
        notices: list[str] = []
        now = datetime.now(UTC)
        first = reports.deliver(report_id, notices.append, channel="test", now=now)
        assert reports.deliver(report_id, notices.append, channel="test", now=now + timedelta(minutes=5)) == first
        assert len(notices) == 1
        assert reports.release(report_id, explicit=False, now=now + timedelta(minutes=19, seconds=59)) == []
        assert Controller(ledger).tick(now=now + timedelta(minutes=20))["released_ideas"] == ideas
        assert Controller(ledger).tick(now=now + timedelta(minutes=21))["released_ideas"] == []


def test_pending_annotations_block_scope_without_timeout_unpausing(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas, report_id, now = delivered_round(ledger)
        reports = Reports(ledger)
        reports.annotate(report_id, "pause", "Do not execute this idea", idea_id=ideas[0])
        assert Controller(ledger).tick(now=now + timedelta(minutes=20))["released_ideas"] == [ideas[1]]
        assert Controller(ledger).tick(now=now + timedelta(hours=2))["released_ideas"] == []
        assert Discussion(ledger).idea(ideas[0])["state"] == "held"
        reports.release(report_id, explicit=True, idea_id=ideas[0], now=now + timedelta(hours=2))
        assert Discussion(ledger).idea(ideas[0])["state"] == "released"


def test_scoped_approval_does_not_clear_a_global_pause(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas, report_id, now = delivered_round(ledger)
        reports = Reports(ledger)
        reports.annotate(report_id, "pause", "Pause the entire round")
        assert reports.release(report_id, explicit=True, idea_id=ideas[0]) == [ideas[0]]
        assert Controller(ledger).tick(now=now + timedelta(hours=1))["released_ideas"] == []
        assert Discussion(ledger).idea(ideas[1])["state"] == "held"


def test_reissued_report_only_restarts_affected_scope(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas, first_report, now = delivered_round(ledger)
        reports = Reports(ledger)
        annotation = reports.annotate(first_report, "comment", "Clarify the evidence", idea_id=ideas[0])
        reports.resolve(annotation, "Clarification retained without claiming new results")
        second_report = reports.generate(round_id)["report_id"]
        assert Controller(ledger).tick(now=now + timedelta(minutes=20))["released_ideas"] == [ideas[1]]
        reports.deliver(second_report, lambda message: None, channel="test", now=now + timedelta(minutes=21))
        assert Controller(ledger).tick(now=now + timedelta(minutes=40))["released_ideas"] == []
        assert Controller(ledger).tick(now=now + timedelta(minutes=41))["released_ideas"] == [ideas[0]]


def test_review_blocker_cannot_be_overridden_by_high_scores(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, _ideas = discussion_round(ledger)
        finish_reviews(ledger, round_id, _ideas, blocker="Hard authorization conflict")
        reports = Reports(ledger)
        report_id = reports.generate(round_id)["report_id"]
        now = datetime.now(UTC)
        reports.deliver(report_id, lambda message: None, channel="test", now=now)
        assert reports.release(report_id, explicit=True) == []
        assert Controller(ledger).tick(now=now + timedelta(minutes=30))["released_ideas"] == []


def test_reports_restore_without_original_files_and_require_redelivery(tmp_path: Path) -> None:
    root = tmp_path / "state"
    with Ledger.create(root, portable_contract()) as ledger:
        _round, _ideas, report_id, now = delivered_round(ledger)
        backup = ledger.backup(tmp_path / "snapshot.sqlite3")
        original = Reports(ledger).report(report_id)
        Path(original["long_path"]).unlink()
        Path(original["short_path"]).unlink()
    with Ledger.restore(backup, tmp_path / "restored") as ledger:
        reports = Reports(ledger)
        reports.verify_files(report_id)
        assert Controller(ledger).tick(now=now + timedelta(hours=1))["released_ideas"] == []
        restored = reports.deliver(report_id, lambda message: None, channel="test")
        assert restored["deadline"] == (now + timedelta(minutes=20)).isoformat()
        assert len(Controller(ledger).tick(now=now + timedelta(hours=1))["released_ideas"]) == 2


def test_reservation_confirmation_budget_and_instance_receipts(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        queue = JobQueue(ledger)
        task_id = queue.assign(
            ideas[0], manager="project-manager", engineer="engineer",
            title="Test assignment", objective="Synthetic work", workspace=workspace,
        )
        with pytest.raises(WorkflowError, match="confirmation"):
            queue.reserve(task_id, "0.25")
        queue.confirm(ideas[0], "expert", "engineer", "Test implementation recap")
        job_id = queue.reserve(task_id, "0.25")
        assert queue.budget()["available_usd"] == 1.75
        with pytest.raises(WorkflowError, match="not queued"):
            queue.reserve(task_id, "0.25")
        queue.claim_start(job_id)
        queue.started(job_id, "test-instance")
        with pytest.raises(WorkflowError, match="does not match"):
            queue.finish(job_id, "wrong-instance", state="failed")
        queue.finish(job_id, "test-instance", state="failed")
        assert queue.budget()["unknown_usage_jobs"] == [job_id]
        assert queue.budget()["available_usd"] == 1.75
        queue.settle(job_id, "0.10")
        assert queue.budget()["available_usd"] == 1.90
        assert Controller(ledger).tick()["execution_enabled"] is False


def test_once_directives_are_not_merged_or_consumed_before_start(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas = released_round(ledger)
        first = ledger.add_directive("Perform one additional check", lifetime="once", round_id=round_id)
        second = ledger.add_directive("Perform one additional check", lifetime="once", round_id=round_id)
        assert first != second
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        queue = JobQueue(ledger)
        job_id = queue.reserve(task_id, "0.1")
        assert len(ledger.directives(round_id, "engineer")) == 2
        queue.claim_start(job_id)
        assert len(ledger.directives(round_id, "engineer")) == 2
        late = ledger.add_directive("A later independent request", lifetime="once", round_id=round_id)
        queue.started(job_id, "test-instance")
        assert [item["id"] for item in ledger.directives(round_id, "engineer")] == [late]


def test_recovery_does_not_dispatch_another_instance(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        job_id = queue.reserve(task_id, "0.1")
        queue.claim_start(job_id)
        assert queue.recover() == [job_id]
        with pytest.raises(WorkflowError, match="reconciliation"):
            queue.claim_start(job_id)
        queue.started(job_id, "recovered-test-instance")
        assert queue.job(job_id)["state"] == "running"
        queue.stop(job_id, "Operator stop")
        assert queue.job(job_id)["state"] == "stop_requested"
        assert queue.budget()["available_usd"] == 1.9
        queue.finish(job_id, "recovered-test-instance", state="stopped", cost="0.02")
        assert queue.budget()["available_usd"] == 1.98


def test_shared_workspace_and_dependency_prevent_admission(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        first = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        queue.confirm(ideas[1], "co-optimization-expert", "engineer", "Test joint recap")
        second = queue.assign(
            ideas[1], manager="project-manager", engineer="engineer",
            title="Dependent task", objective="Wait for accepted output",
            workspace=tmp_path / "workspace", dependencies=(first,),
        )
        with pytest.raises(WorkflowError, match="dependencies"):
            queue.reserve(second, "0.1")
        queued = queue.reserve(first, "0.1")
        queue.stop(queued, "Never dispatched")
        assert queue.job(queued)["state"] == "stopped"
        assert queue.budget()["spent_or_reserved_usd"] == 0


def test_single_controller_owner_and_bounded_progress_checks(tmp_path: Path) -> None:
    from argus_skill.core.file_lock import exclusive_file_lock

    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        job_id = queue.reserve(task_id, "0.1")
        queue.claim_start(job_id)
        queue.started(job_id, "test-instance")
        due = datetime.fromisoformat(queue.job(job_id)["next_progress_at"])
        assert queue.progress_due(due - timedelta(seconds=1)) == []
        assert queue.progress_due(due + timedelta(hours=2)) == [job_id]
        assert queue.progress_due(due + timedelta(hours=2)) == []
        with (ledger.root / "controller.lock").open("a+b") as handle:
            with exclusive_file_lock(handle, timeout_seconds=0):
                with pytest.raises(TimeoutError):
                    Controller(ledger).tick()


def test_conservative_fractional_money_and_invalid_amounts() -> None:
    assert money_units("0.0000001") == 1
    assert money_units("0.0000001", allowance=True) == 0
    for value in (True, "-1", "nan", "infinity"):
        with pytest.raises(WorkflowError):
            money_units(value)


def measured_attempt(ledger: Ledger, task_id: int, candidate: float) -> int:
    queue = JobQueue(ledger)
    production = queue.reserve(task_id, "0.05")
    queue.claim_start(production)
    queue.started(production, f"test-production-{production}")
    queue.finish(
        production, f"test-production-{production}", state="completed",
        cost="0.01", output_ref=f"synthetic-candidate-{production}",
    )
    evaluation = queue.reserve(task_id, "0.05", kind="evaluation", evaluator="supervisor")
    queue.claim_start(evaluation)
    queue.started(evaluation, f"test-evaluation-{evaluation}")
    evidence = ledger.root / f"synthetic-evidence-{evaluation}.json"
    measurement = Measurement.from_value({
        "baseline_ref": "test-baseline", "candidate_ref": f"synthetic-candidate-{production}",
        "protocol_ref": "test-protocol", "raw_evidence_ref": str(evidence),
        "correctness": "passed", "direction": "minimize", "baseline": 10,
        "candidate": candidate, "observations": 3, "required_observations": 3,
        "noise_limit": 0.1, "regression_limit": 0.5, "interference": None,
    })
    evidence.write_text(json.dumps({
        "measurement": measurement.to_dict(),
        "raw_results": {"baseline": [10, 10, 10], "candidate": [candidate, candidate, candidate]},
        "command": "synthetic unit-test receipt, not an inference experiment",
        "environment": "unit-test fixture",
    }), encoding="utf-8")
    queue.finish(evaluation, f"test-evaluation-{evaluation}", state="completed", cost="0.01", output_ref=str(evidence))
    return queue.record_measurement(evaluation, measurement)


def test_independent_measurement_and_two_cards_keep_lineage_history(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        first = measured_attempt(ledger, task_id, 12)
        first_card = queue.card(first, "Synthetic regression beyond frozen threshold", manager="project-manager")
        assert queue.card(first, "Same evidence", manager="project-manager") == first_card
        assert Discussion(ledger).idea(ideas[0])["state"] == "paused"
        with pytest.raises(WorkflowError, match="not released"):
            queue.reserve(task_id, "0.1")
        queue.consultation(
            ideas[0], manager="project-manager", expert="expert", engineer="engineer",
            recap="Test-only adjustment with the original budget and comparison rule",
        )
        second = measured_attempt(ledger, task_id, 13)
        queue.card(second, "Second independent synthetic regression", manager="project-manager")
        assert Discussion(ledger).idea(ideas[0])["state"] == "terminated"
        with pytest.raises(WorkflowError, match="first-card"):
            queue.consultation(ideas[0], manager="project-manager", expert="expert", engineer="engineer", recap="Retry")
        assert ledger.connection.execute("SELECT COUNT(*) FROM yellow_cards").fetchone()[0] == 2


def test_valid_result_requires_manager_acceptance(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        evaluation = measured_attempt(ledger, task_id, 8)
        assert queue.task(task_id)["state"] == "evaluating"
        with pytest.raises(WorkflowError, match="valid, confirmed"):
            queue.card(evaluation, "A good result is not a regression", manager="project-manager")
        queue.decide(task_id, "project-manager", "accept", "Synthetic independent result accepted")
        assert queue.task(task_id)["state"] == "accepted"


@pytest.mark.parametrize("correctness,observations,candidate,verdict", [
    ("failed", 3, 20, "invalid"), ("not_run", 3, 20, "invalid"),
    ("passed", 1, 20, "invalid"), ("passed", 3, 10.05, "unchanged"),
    ("passed", 3, 10.5, "unchanged"), ("passed", 3, 10.6, "regressed"),
])
def test_measurement_validity_and_exact_noise_threshold(
    correctness: str, observations: int, candidate: float, verdict: str,
) -> None:
    measurement = Measurement.from_value({
        "baseline_ref": "baseline", "candidate_ref": "candidate", "protocol_ref": "protocol",
        "raw_evidence_ref": "raw", "correctness": correctness, "direction": "minimize",
        "baseline": 10, "candidate": candidate, "observations": observations,
        "required_observations": 3, "noise_limit": 0.1, "regression_limit": 0.5,
    })
    assert measurement.verdict() == verdict


def test_cli_operator_protocol_and_pending_intent_end_to_end(tmp_path: Path) -> None:
    state = tmp_path / "state"
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(json.dumps(portable_contract().to_dict()), encoding="utf-8")
    assert cli(state, "init", "--contract", str(contract_file)).returncode == 0
    assert cli(state, "round", "create").returncode == 0
    assert cli(state, "round", "prepare", "1", "--expected-revision", "1").returncode == 0
    assert cli(state, "discussion", "begin", "1").returncode == 0
    for role, joint in (("expert", False), ("co-optimization-expert", True)):
        file = tmp_path / f"{role}.json"
        file.write_text(json.dumps(proposal(joint).to_dict()), encoding="utf-8")
        result = cli(state, "discussion", "submit", "1", "--role", role, "--file", str(file))
        assert result.returncode == 0, result.stderr
    assert cli(state, "discussion", "advance", "1").returncode == 0
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(review().to_dict()), encoding="utf-8")
    for idea in ("1", "2"):
        for role in ("reviewer", "second-reviewer"):
            result = cli(
                state, "discussion", "review", idea, "--role", role, "--file", str(review_file),
                "--expected-version", "1",
            )
            assert result.returncode == 0, result.stderr
    assert cli(state, "discussion", "advance", "1").returncode == 0
    generated = cli(state, "report", "generate", "1")
    assert generated.returncode == 0, generated.stderr
    report_id = str(json.loads(generated.stdout)["report_id"])
    delivered = cli(state, "report", "deliver", report_id, "--channel", "test-only-cli")
    assert delivered.returncode == 0, delivered.stderr
    assert cli(state, "report", "release", report_id).returncode == 0
    assert cli(state, "task", "confirm", "1", "--expert", "expert", "--engineer", "engineer", "--recap", "Fixture recap").returncode == 0
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assignment = tmp_path / "assignment.json"
    assignment.write_text(json.dumps({
        "idea_id": 1, "manager": "project-manager", "engineer": "engineer",
        "title": "CLI fixture", "objective": "Check portable dispatch intent",
        "workspace": str(workspace),
    }), encoding="utf-8")
    assert cli(state, "task", "assign", "--file", str(assignment)).returncode == 0
    reserved = cli(state, "task", "reserve", "1", "--usd", "0.1")
    assert reserved.returncode == 0, reserved.stderr
    tick = cli(state, "controller", "tick")
    assert tick.returncode == 0, tick.stderr
    assert json.loads(tick.stdout)["execution_enabled"] is False
    with Ledger(state / DATABASE_NAME, readonly=True) as ledger:
        assert JobQueue(ledger).job(1)["state"] == "reserved"
        assert JobQueue(ledger).job(1)["instance_id"] is None


def test_workspace_conflict_and_global_recovery_block_other_tasks(tmp_path: Path) -> None:
    payload = portable_contract().to_dict()
    payload["max_active_attempts"] = 2
    with Ledger.create(tmp_path / "state", ResearchContract.from_value(payload)) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        first = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        second = assigned_task(ledger, ideas[1], tmp_path / "workspace")
        job_id = queue.reserve(first, "0.1")
        with pytest.raises(WorkflowError, match="workspace"):
            queue.reserve(second, "0.1")
        queue.claim_start(job_id)
        queue.recover()
        with pytest.raises(WorkflowError, match="reconciliation"):
            queue.reserve(second, "0.1")
        queue.started(job_id, "test-live-instance")
        queue.finish(job_id, "test-live-instance", state="failed", cost="0.01")
        assert queue.reserve(second, "0.1") > job_id


def test_truth_suspicion_confirmation_and_card_revocation_preserve_history(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        evaluation = measured_attempt(ledger, task_id, 12)
        card = queue.card(evaluation, "Synthetic negative evidence", manager="project-manager")
        correction = ledger.root / "correction.json"
        correction.write_text('{"fixture_correction":"original observation was erroneous"}', encoding="utf-8")
        queue.revoke_card(card, manager="project-manager", reason="Verified fixture correction", correction_ref=str(correction))
        assert queue.inspect()["yellow_cards"][0]["revoked_reason"] == "Verified fixture correction"
        assert Discussion(ledger).idea(ideas[0])["state"] == "held"
        production = ledger.connection.execute("SELECT id FROM jobs WHERE kind = 'production'").fetchone()[0]
        suspicion = queue.truth_case(production, "Investigate a fixture claim", str(correction))
        assert queue.inspect()["truth_cases"][0]["state"] == "suspected"
        with pytest.raises(WorkflowError, match="investigation"):
            queue.decide(task_id, "project-manager", "accept", "Cannot use frozen evidence")
        queue.dismiss_truth(suspicion, "Fixture bug, not a truth violation")
        confirmed = queue.truth_case(production, "A claim contradicts the captured fixture output", str(correction))
        queue.confirm_truth(confirmed, "Trusted fixture adjudication identified the contradiction")
        assert queue.inspect()["truth_cases"][1]["state"] == "confirmed"
        assert len(queue.inspect()["yellow_cards"]) == 1


def test_draft_repair_is_recoverable_and_never_rewrites_published_reports(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas = discussion_round(ledger)
        finish_reviews(ledger, round_id, ideas)
        reports = Reports(ledger)
        generated = reports.generate(round_id)
        report_id = generated["report_id"]
        Path(generated["short_path"]).unlink()
        retried = reports.generate(round_id)
        assert retried == generated
        assert reports.report(report_id)["submitted_at"] is None
        reports.deliver(report_id, lambda message: None, channel="test")
        with pytest.raises(WorkflowError, match="cannot be repaired"):
            reports.repair_draft(report_id)


def test_round_pause_resume_keeps_deadline_and_contract_revision_stops_intents(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        round_id, ideas, report_id, _now = delivered_round(ledger)
        reports = Reports(ledger)
        deadline = reports.report(report_id)["deadline"]
        reports.release(report_id, explicit=True)
        version = ledger.round_status(round_id)["revision"]
        ledger.transition(round_id, "pause", expected_revision=version)
        assert Controller(ledger).tick()["released_ideas"] == []
        ledger.transition(round_id, "resume", expected_revision=version + 1)
        assert reports.report(report_id)["deadline"] == deadline
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        job_id = queue.reserve(task_id, "0.1")
        changed = portable_contract().to_dict()
        changed["objective"] = "An explicitly changed operator goal"
        ledger.revise_contract(ResearchContract.from_value(changed), expected_revision=1)
        assert queue.job(job_id)["state"] == "stopped"
        assert ledger.round_status(round_id)["state"] == "paused"
        assert queue.budget()["spent_or_reserved_usd"] == 0


def test_source_only_preparation_does_not_require_existing_inference_code(tmp_path: Path) -> None:
    payload = portable_contract().to_dict()
    payload["baseline_ref"] = None
    payload["evaluation_protocol"] = None
    payload["compute_scope"] = None
    payload["rebuttal_rounds"] = None
    payload["roles"] = list(payload["roles"]) + [{"role_id": "baseline-researcher", "kind": "researcher"}]
    with Ledger.create(tmp_path / "state", ResearchContract.from_value(payload)) as ledger:
        round_id = ledger.create_round()
        ledger.transition(round_id, "prepare", expected_revision=1)
        queue = JobQueue(ledger)
        workspace = tmp_path / "research-workspace"
        workspace.mkdir()
        task_id = queue.assign_preparation(
            round_id, coordinator="moderator", worker="baseline-researcher",
            title="Find a baseline", objective="Source-only, no candidate experiments", workspace=workspace,
        )
        job_id = queue.reserve(task_id, "0.1")
        queue.claim_start(job_id)
        context = queue.request_context(job_id)
        assert context["task"]["phase"] == "preparation"
        assert context["task"]["contract_revision"] == 1
        assert "Do not run candidate experiments" in context["duty"]
        queue.started(job_id, "test-researcher")
        notes = ledger.root / "test-method-card.json"
        notes.write_text('{"fixture":"source-only method card"}', encoding="utf-8")
        queue.finish(job_id, "test-researcher", state="completed", cost="0.01", output_ref=str(notes))
        with pytest.raises(WorkflowError, match="independent supported review"):
            queue.decide(task_id, "moderator", "accept", "Worker completion is not enough")
        peer_id = queue.reserve(task_id, "0.1", kind="evaluation", evaluator="reviewer")
        queue.claim_start(peer_id)
        queue.started(peer_id, "test-source-reviewer")
        peer = ledger.root / "test-source-review.json"
        peer.write_text(json.dumps({
            "status": "supported", "reason": "Synthetic peer fixture, not a literature verification",
            "evidence_refs": [str(notes)],
        }), encoding="utf-8")
        queue.finish(peer_id, "test-source-reviewer", state="completed", cost="0.01", output_ref=str(peer))
        queue.record_preparation_review(peer_id)
        queue.decide(task_id, "moderator", "accept", "Synthetic source result accepted")
        assert queue.task(task_id)["state"] == "accepted"
        with pytest.raises(WorkflowError, match="discussion is blocked"):
            Discussion(ledger).begin(round_id)
        revised = portable_contract().to_dict()
        revised["roles"] = payload["roles"]
        ledger.revise_contract(ResearchContract.from_value(revised), expected_revision=1)
        ledger.rebind_round(round_id, expected_revision=3)
        ledger.transition(round_id, "prepare", expected_revision=4)
        Discussion(ledger).begin(round_id)
        assert ledger.round_status(round_id)["state"] == "proposals"
        assert queue.budget()["spent_or_reserved_usd"] == 0.02


def test_late_start_receipt_after_cancel_does_not_reopen_a_job(tmp_path: Path) -> None:
    with Ledger.create(tmp_path / "state", portable_contract()) as ledger:
        _round, ideas = released_round(ledger)
        queue = JobQueue(ledger)
        task_id = assigned_task(ledger, ideas[0], tmp_path / "workspace")
        job_id = queue.reserve(task_id, "0.1")
        queue.claim_start(job_id)
        queue.stop(job_id, "Cancelled while the runner was starting")
        queue.started(job_id, "late-test-instance")
        assert queue.job(job_id)["state"] == "stop_requested"
        queue.started(job_id, "late-test-instance")
        queue.finish(job_id, "late-test-instance", state="stopped", cost="0.01")
        queue.finish(job_id, "late-test-instance", state="stopped", cost="0.01")
        queue.started(job_id, "late-test-instance")
        assert queue.job(job_id)["state"] == "stopped"
        assert queue.budget()["spent_or_reserved_usd"] == 0.01
