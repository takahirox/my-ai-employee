"""Product-level regression cases for autonomous execution and deterministic boundaries."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

import pytest

from ai_employee.autonomous.candidates import Candidates
from ai_employee.autonomous.engine import Engine
from ai_employee.autonomous.history import Journal, Stopped
from ai_employee.autonomous.models import (
    Authority,
    Clarification,
    Contract,
    Criterion,
    Finding,
    Goal,
    Limits,
    Plan,
    Requirement,
    RunConfig,
    StagePolicy,
    Task,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.autonomous.native import run_process

T = TypeVar("T", bound=Contract)


def config(**limits: object) -> RunConfig:
    stage = StagePolicy(model="test-only")
    return RunConfig(
        clarification=stage,
        planning=stage,
        worker=stage,
        verification=stage,
        recovery=stage,
        limits=Limits.model_validate({"active_seconds": 10000, **limits}),
    )


def clarification() -> Clarification:
    return Clarification(
        clarified_goal="Write result",
        criteria=(Criterion(id="result", description="result exists"),),
        requirements=(Requirement(original_fragment="Write result", criteria=("result",)),),
    )


class OfflineModel:
    def apply_authority(self, workspace: Path, authority: Authority) -> None:
        assert workspace.is_dir()

    def __init__(
        self, *, ambiguous: bool = False, quota: bool = False, fail_once: bool = False
    ) -> None:
        self.calls: list[str] = []
        self.ambiguous = ambiguous
        self.quota = quota
        self.fail_once = fail_once
        self.workers = 0
        self.verifications = 0

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[T, Usage]:
        self.calls.append(schema.__name__)
        body = json.loads(prompt)
        if self.quota:
            raise Stopped("USAGE_LIMIT")
        result: Contract
        if schema is Clarification:
            result = clarification().model_copy(
                update={"unresolved": ("Which result?",) if self.ambiguous else ()}
            )
        elif schema is Plan:
            result = Plan(
                tasks=(
                    Task(
                        id="write",
                        description="Write result",
                        criteria=clarification().criteria,
                        verification_plan="Inspect file",
                    ),
                ),
                result_task="write",
            )
        elif schema is WorkerResult:
            self.workers += 1
            # This edits the actual workspace, with no Fleet action proposal.
            (workspace / "result.txt").write_text(
                "correct" if self.workers > 1 or not self.fail_once else "wrong"
            )
            result = WorkerResult(status="completed", summary="result written")
        elif schema is Verification:
            self.verifications += 1
            passed = (workspace / "result.txt").read_text() == "correct"
            # Verification's modifications must never become the published result.
            (workspace / "verifier-output.txt").write_text("untrusted generated evidence")
            result = Verification(
                findings=tuple(
                    Finding(
                        criterion_id=item["id"], passed=passed, evidence="inspected actual file"
                    )
                    for item in body["criteria"]
                ),
                summary="checked actual candidate",
            )
        else:
            raise AssertionError(schema)
        return schema.model_validate(result.model_dump()), Usage(tokens=10, cost=0)

    def check(
        self, argv: tuple[str, ...], workspace: Path, timeout: float, cancelled: Callable[[], bool]
    ) -> tuple[bool, str]:
        return True, "test-receipt"


def runtime(tmp_path: Path, model: OfflineModel) -> tuple[Engine, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "original.txt").write_text("keep")
    engine = Engine(
        Journal(tmp_path / "history.db"), Candidates(tmp_path / "objects"), model, tmp_path / "work"
    )
    return engine, source


def test_actual_worker_candidate_is_verified_published_and_replayed_without_work(
    tmp_path: Path,
) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    assert not (source / "result.txt").exists()
    engine.promote(run, tmp_path / "published")
    assert (tmp_path / "published/result.txt").read_text() == "correct"
    assert not (tmp_path / "published/verifier-output.txt").exists()
    assert model.verifications == 2
    calls = len(model.calls)
    engine.execute(run)
    assert len(model.calls) == calls


def test_failed_candidate_is_repaired_in_same_workspace_with_distinct_attempts(
    tmp_path: Path,
) -> None:
    model = OfflineModel(fail_once=True)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    events = engine.journal.events(run)
    attempts = [event["body"]["context"] for event in events if event["kind"] == "attempt_started"]
    assert len(attempts) == 2
    assert attempts[0]["workspace"] == attempts[1]["workspace"]
    assert attempts[0]["attempt_id"] != attempts[1]["attempt_id"]
    candidates = [event["body"]["candidate"] for event in events if event["kind"] == "candidate"]
    assert candidates[0]["tree"] != candidates[1]["tree"]
    assert engine.candidates.manifest(candidates[0]["tree"])


def test_unresolved_clarification_never_reaches_planning(tmp_path: Path) -> None:
    model = OfflineModel(ambiguous=True)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    engine.execute(run)
    assert model.calls == ["Clarification"]
    assert engine.journal.events(run)[-1]["kind"] == "clarification_wait"


def test_quota_stops_model_calls_and_prevents_resume(tmp_path: Path) -> None:
    model = OfflineModel(quota=True)
    engine, source = runtime(tmp_path, model)
    with pytest.raises(Stopped, match="USAGE_LIMIT"):
        engine.start("Write result", config(), source)
    with engine.journal.connect() as db:
        run = db.execute("SELECT id FROM runs").fetchone()[0]
    with pytest.raises(Stopped):
        engine.execute(run)
    assert len(model.calls) == 1
    assert any(event["kind"] == "settled" for event in engine.journal.events(run))


def test_parallel_reservations_cannot_spend_same_tokens_and_unknown_retains_reservation(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config(tokens=100, reservation_tokens=100))

    def reserve() -> str | None:
        try:
            return journal.reserve(run, "worker")[0]
        except Stopped:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    journal.settle(run, winners[0], 1, Usage())
    assert reserve() is None


def test_journal_detects_changed_configuration_and_history(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    with journal.connect() as db:
        db.execute("UPDATE runs SET config=? WHERE id=?", (config(attempts=100).canonical(), run))
    with pytest.raises(ValueError, match="RUN_CONFIG_CHANGED"):
        journal.config(run)
    with journal.connect() as db:
        db.execute("UPDATE events SET body='{}' WHERE run=?", (run,))
    with pytest.raises(ValueError, match="HISTORY_INTEGRITY_FAILURE"):
        journal.events(run)


def test_foreign_fragments_missing_mandatory_checks_and_cycle_are_rejected() -> None:
    with pytest.raises(ValueError, match="FOREIGN_REQUIREMENT_FRAGMENT"):
        Goal(original_input="other", specification=clarification())
    with pytest.raises(ValueError, match="MANDATORY_CHECK_OMITTED"):
        Goal(
            original_input="Write result",
            specification=clarification(),
            mandatory_checks=("required",),
        )
    task = Task(
        id="a",
        description="work",
        criteria=clarification().criteria,
        verification_plan="inspect",
        dependencies=("a",),
    )
    with pytest.raises(ValueError, match="CYCLIC_OR_MISSING_DEPENDENCY"):
        Plan(tasks=(task,), result_task="a")


def test_snapshot_tampering_symlinks_and_promotion_overwrite_are_rejected(tmp_path: Path) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    (source / "link").symlink_to(tmp_path / "host-secret")
    with pytest.raises(ValueError, match="CANDIDATE_SPECIAL_FILE"):
        engine.candidates.capture(source)
    (source / "link").unlink()
    run = engine.start("Write result", config(), source)
    with pytest.raises(ValueError, match="PROMOTION_TARGET_EXISTS"):
        engine.promote(run, source)
    final = engine.journal.events(run)[-1]["body"]["candidate"]
    manifest = engine.candidates.manifest(final["tree"])
    blob = engine.candidates.root / final["tree"] / str(manifest["result.txt"]["blob"])
    blob.chmod(0o600)
    blob.write_text("tampered")
    with pytest.raises(ValueError, match="CANDIDATE_BYTES_CHANGED"):
        engine.promote(run, tmp_path / "destination")


def test_process_quota_detection_stops_before_sleep(tmp_path: Path) -> None:
    with pytest.raises(Stopped, match="USAGE_LIMIT"):
        run_process(
            (
                sys.executable,
                "-c",
                "print('usage_limit_reached', flush=True); import time; time.sleep(10)",
            ),
            tmp_path,
            2,
            lambda: False,
        )


class AuthorityModel(OfflineModel):
    def __init__(self, *, apply_fails: bool = False) -> None:
        super().__init__()
        self.requested = False
        self.apply_fails = apply_fails

    def apply_authority(self, workspace: Path, authority: Authority) -> None:
        if self.apply_fails:
            raise ValueError("ENVIRONMENT_APPLICATION_FAILED")

    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[T, Usage]:
        if schema is WorkerResult and not self.requested:
            self.requested = True
            (workspace / "retained.txt").write_text("useful progress")
            return schema.model_validate(
                WorkerResult(
                    status="authority_requested",
                    summary="need network",
                    authority_request=Authority(network_hosts=("example.com",)),
                ).model_dump()
            ), Usage(tokens=10)
        if schema is WorkerResult:
            assert (workspace / "retained.txt").read_text() == "useful progress"
            assert authority.network_hosts == ("example.com",)
        return super().generate(policy, prompt, schema, workspace, authority, timeout, cancelled)


def test_authority_approval_applies_before_resume_and_preserves_workspace(tmp_path: Path) -> None:
    model = AuthorityModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(network_hosts=("example.com",))}
    )
    run = engine.start("Write result", cfg, source)
    waiting = engine.journal.events(run)[-1]
    assert waiting["kind"] == "approval_wait"
    calls = len(model.calls)
    engine.execute(run)
    assert len(model.calls) == calls
    engine.approve_authority(run, waiting["body"]["attempt"], approve=True)
    assert engine.journal.events(run)[-1]["kind"] == "authority_applied"
    engine.execute(run)
    assert engine.journal.events(run)[-1]["kind"] == "completed"


def test_failed_authority_application_does_not_resume(tmp_path: Path) -> None:
    model = AuthorityModel(apply_fails=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(network_hosts=("example.com",))}
    )
    run = engine.start("Write result", cfg, source)
    waiting = engine.journal.events(run)[-1]
    with pytest.raises(ValueError, match="ENVIRONMENT_APPLICATION_FAILED"):
        engine.approve_authority(run, waiting["body"]["attempt"], approve=True)
    assert engine.journal.events(run)[-1]["kind"] == "authority_approved"
    calls = len(model.calls)
    engine.execute(run)
    assert len(model.calls) == calls


class RepairModel(OfflineModel):
    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
    ) -> tuple[T, Usage]:
        body = json.loads(prompt)
        if (
            schema is Verification
            and body.get("task") is None
            and "criteria" in body
            and self.workers == 1
        ):
            return schema.model_validate(
                Verification(
                    findings=(
                        Finding(
                            criterion_id="result",
                            passed=False,
                            evidence="additional integration needed",
                        ),
                    ),
                    summary="goal incomplete",
                ).model_dump()
            ), Usage(tokens=1)
        if schema is Plan and "previous_plan" in body:
            previous = Plan.model_validate(body["previous_plan"])
            repair = Task(
                id="repair",
                description="Complete missing integration",
                criteria=clarification().criteria,
                verification_plan="inspect",
                dependencies=(previous.result_task,),
                kind="repair",
            )
            return schema.model_validate(
                Plan(tasks=(*previous.tasks, repair), result_task="repair").model_dump()
            ), Usage(tokens=1)
        return super().generate(policy, prompt, schema, workspace, authority, timeout, cancelled)


def test_goal_failure_extends_graph_and_preserves_accepted_work(tmp_path: Path) -> None:
    model = RepairModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    events = engine.journal.events(run)
    plans = [event["body"]["plan"] for event in events if event["kind"] == "plan"]
    assert len(plans) == 2
    assert plans[0]["tasks"][0] == plans[1]["tasks"][0]
    assert model.workers == 2
    accepted = [event["body"]["candidate"] for event in events if event["kind"] == "accepted"]
    from ai_employee.autonomous.models import Candidate

    assert accepted[1]["upstream"] == [Candidate.model_validate(accepted[0]).digest]
    assert events[-1]["kind"] == "completed"


def test_configured_replan_limit_prevents_new_model_attempt(tmp_path: Path) -> None:
    model = RepairModel()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.start("Write result", config(replans=0), source)
    assert model.calls.count("Plan") == 1
    assert model.workers == 1
