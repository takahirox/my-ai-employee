"""Product-level regression cases for autonomous execution and deterministic boundaries."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import pytest

from ai_employee.candidates import Candidates
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.models import (
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
from ai_employee.native import run_process

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
    def reconcile(self, run_directory: Path) -> None:
        pass

    def apply_authority(
        self, workspace: Path, authority: Authority, timeout: float, cancelled: Callable[[], bool]
    ) -> None:
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
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
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
                "import json; print(json.dumps({'type':'error', 'error': "
                "{'code':'usage_limit_reached'}}), flush=True); import time; time.sleep(10)",
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

    def apply_authority(
        self, workspace: Path, authority: Authority, timeout: float, cancelled: Callable[[], bool]
    ) -> None:
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
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
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
    kinds = [event["kind"] for event in engine.journal.events(run)]
    assert "authority_approved" in kinds
    assert "authority_applied" not in kinds
    assert kinds[-1] == "settled"
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
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
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
    from ai_employee.models import Candidate

    assert accepted[1]["upstream"] == [Candidate.model_validate(accepted[0]).digest]
    assert events[-1]["kind"] == "completed"


def test_configured_replan_limit_prevents_new_model_attempt(tmp_path: Path) -> None:
    model = RepairModel()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.start("Write result", config(replans=0), source)
    assert model.calls.count("Plan") == 1
    assert model.workers == 1


class ExternalModel(OfflineModel):
    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[T, Usage]:
        result, usage = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, observer
        )
        if schema is Plan:
            plan = Plan.model_validate(result.model_dump())
            task = plan.tasks[0].model_copy(update={"authority": Authority(external_writes=True)})
            return schema.model_validate(
                plan.model_copy(update={"tasks": (task,)}).model_dump()
            ), usage
        return result, usage


def test_unverified_external_effect_is_not_repeated_by_worker_retry(tmp_path: Path) -> None:
    model = ExternalModel(fail_once=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(external_writes=True), "security": "balanced"}
    )
    run = engine.start("Write result", cfg, source)
    assert model.workers == 1
    assert engine.journal.events(run)[-1]["kind"] == "uncertain"
    calls = len(model.calls)
    engine.execute(run)
    assert len(model.calls) == calls


class GraphModel(OfflineModel):
    def generate(
        self,
        policy: StagePolicy,
        prompt: str,
        schema: type[T],
        workspace: Path,
        authority: Authority,
        timeout: float,
        cancelled: Callable[[], bool],
        observer: Callable[[float, int], None] | None = None,
        observation: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[T, Usage]:
        body = json.loads(prompt)
        if schema is Plan:
            tasks = tuple(
                Task(
                    id=key,
                    description=key,
                    criteria=clarification().criteria,
                    verification_plan="inspect",
                    dependencies=parents,
                    kind="integration" if parents else "work",
                )
                for key, parents in (
                    ("a", ()),
                    ("b", ()),
                    ("join", ("a", "b")),
                    ("c", ()),
                    ("final", ("join", "c")),
                )
            )
            return schema.model_validate(
                Plan(tasks=tasks, result_task="final").model_dump()
            ), Usage(tokens=1)
        if schema is WorkerResult:
            key = body["context"]["task"]["id"]
            inputs = workspace / ".fleet-inputs"
            if inputs.exists():
                for upstream in inputs.iterdir():
                    for marker in upstream.glob("*.branch"):
                        (workspace / marker.name).write_text(marker.read_text())
            (workspace / (key + ".branch")).write_text(key)
            if key == "final":
                assert {path.stem for path in workspace.glob("*.branch")} == {
                    "a",
                    "b",
                    "join",
                    "c",
                    "final",
                }
        return super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, observer
        )


def test_multiple_integrations_materialize_exact_upstream_candidates(tmp_path: Path) -> None:
    model = GraphModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    assert model.workers == 5
    engine.promote(run, tmp_path / "published")
    assert not (tmp_path / "published/.fleet-inputs").exists()
    assert {path.stem for path in (tmp_path / "published").glob("*.branch")} == {
        "a",
        "b",
        "join",
        "c",
        "final",
    }


def test_integration_resume_rejects_stale_upstream_identity(tmp_path: Path) -> None:
    model = GraphModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    # Inject an otherwise valid accepted record with a different predecessor identity.
    events = engine.journal.events(run)
    records = [event for event in events if event["kind"] == "accepted"]
    first = records[0]["body"]
    candidate = dict(first["candidate"], attempt_id="foreign-attempt")
    engine.journal.append(run, "accepted", task=first["task"], candidate=candidate)
    # Completed history must also be checked before promotion, not just on worker resume.
    with pytest.raises(ValueError, match="STALE_UPSTREAM_LINEAGE"):
        engine.promote(run, tmp_path / "published")


def test_revocation_prevents_resume_and_promotion(tmp_path: Path) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    engine.revoke_authority(run)
    calls = list(model.calls)
    with pytest.raises(Stopped):
        engine.execute(run)
    with pytest.raises(ValueError, match="PROMOTION_AUTHORITY_UNAVAILABLE"):
        engine.promote(run, tmp_path / "published")
    assert model.calls == calls
    assert "authority_revoked" in [event["kind"] for event in engine.journal.events(run)]


def test_strict_policy_blocks_coarse_external_grants_before_worker(tmp_path: Path) -> None:
    model = ExternalModel()
    engine, source = runtime(tmp_path, model)
    configured = config().model_copy(update={"authority_ceiling": Authority(external_writes=True)})
    with pytest.raises(ValueError, match="STRICT_OPERATION_BOUNDARY_REQUIRED"):
        engine.start("Write result", configured, source)
    assert model.workers == 0


def test_explicit_goal_revision_keeps_provenance_and_invalidates_old_completion(
    tmp_path: Path,
) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.start("Write result", config(), source)
    successor = engine.revise_goal(run, "Write a different result")
    assert engine.journal.original(run) == "Write result"
    assert engine.journal.original(successor) == "Write a different result"
    assert not any(event["kind"] == "verification" for event in engine.journal.events(successor))
    with pytest.raises(ValueError, match="PROMOTION_AUTHORITY_UNAVAILABLE"):
        engine.promote(run, tmp_path / "obsolete")
    with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
        engine.promote(successor, tmp_path / "unverified")


def test_goal_revision_cannot_automatically_restart_a_usage_limited_run(tmp_path: Path) -> None:
    model = OfflineModel(quota=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(Stopped):
        engine.execute(run)
    with pytest.raises(Stopped):
        engine.revise_goal(run, "Try again")
    assert len(engine.journal.runs()) == 1
    assert model.calls == ["Clarification"]


@pytest.mark.parametrize("crash_at", ["candidate", "accepted"])
def test_completed_worker_survives_controller_crash_without_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_at: str
) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    append = engine.journal.append

    def interrupted(run_id: str, kind: str, **body: Any) -> None:
        if kind == crash_at:
            raise RuntimeError("simulated controller crash")
        append(run_id, kind, **body)

    monkeypatch.setattr(engine.journal, "append", interrupted)
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        engine.execute(run)
    assert model.workers == 1
    monkeypatch.undo()
    engine.execute(run)
    assert model.workers == 1
    assert engine.journal.events(run)[-1]["kind"] == "completed"
    assert any(event["kind"] == "candidate_reused" for event in engine.journal.events(run))


def test_external_completion_crash_does_not_repeat_unverified_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ExternalModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(external_writes=True), "security": "balanced"}
    )
    run = engine.prepare("Write result", cfg, source)
    append = engine.journal.append

    def interrupted(run_id: str, kind: str, **body: Any) -> None:
        if kind == "candidate":
            raise RuntimeError("simulated controller crash")
        append(run_id, kind, **body)

    monkeypatch.setattr(engine.journal, "append", interrupted)
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        engine.execute(run)
    monkeypatch.undo()
    engine.execute(run)
    assert model.workers == 1
    assert engine.journal.events(run)[-1]["kind"] == "uncertain"
    engine.execute(run)
    assert model.workers == 1


def test_authority_application_obeys_remaining_budget_and_cancellation(tmp_path: Path) -> None:
    model = AuthorityModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"authority_ceiling": Authority(network_hosts=("example.com",))}
    )
    run = engine.start("Write result", cfg, source)
    attempt = engine.journal.events(run)[-1]["body"]["attempt"]
    observed: list[float] = []

    def apply(
        workspace: Path, authority: Authority, timeout: float, cancelled: Callable[[], bool]
    ) -> None:
        observed.append(timeout)
        engine.journal.stop(run, "OPERATOR_CANCELLED")
        assert cancelled()

    model.apply_authority = apply  # type: ignore[method-assign]
    with pytest.raises(Stopped):
        engine.approve_authority(run, attempt, approve=True)
    assert 0 < observed[0] <= cfg.limits.invocation_seconds
    events = engine.journal.events(run)
    assert not any(e["kind"] == "authority_applied" for e in events)
    reserve = next(
        e
        for e in events
        if e["kind"] == "reserved" and e["body"]["stage"] == "authority_application"
    )
    assert reserve["body"]["tokens"] == 0
    assert reserve["body"]["cost"] == 0


def test_quota_observation_survives_cleanup_failure(tmp_path: Path) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)

    def lost_cleanup(*args: Any, **kwargs: Any) -> Any:
        kwargs["observation"]({"event": "usage_limit", "source": "provider"})
        raise RuntimeError("simulated cleanup failure")

    model.generate = lost_cleanup  # type: ignore[method-assign]
    with pytest.raises(Stopped, match="USAGE_LIMIT"):
        engine.execute(run)
    assert any(
        e["kind"] == "stopped" and e["body"]["reason"] == "USAGE_LIMIT"
        for e in engine.journal.events(run)
    )
    with pytest.raises(Stopped):
        engine.execute(run)


def test_authority_request_journal_group_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, source = runtime(tmp_path, OfflineModel())
    run = engine.prepare("Write result", config(), source)
    event = engine.journal._event

    def fail_wait(db: Any, run_id: str, kind: str, body: Any) -> None:
        if kind == "approval_wait":
            raise RuntimeError("simulated transaction interruption")
        event(db, run_id, kind, body)

    monkeypatch.setattr(engine.journal, "_event", fail_wait)
    with pytest.raises(RuntimeError):
        engine.journal.append_many(
            run,
            (
                ("worker_result", {"attempt": "test"}),
                ("authority_requested", {"attempt": "test"}),
                ("approval_wait", {"attempt": "test"}),
            ),
        )
    assert [e["kind"] for e in engine.journal.events(run)] == ["created", "input"]


def test_accepted_task_crash_releases_lease_without_worker_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)

    def interrupted(run_id: str, task: str) -> None:
        # Simulate an external lease retained between durable acceptance and release.
        assert engine.journal.acquire_resources(run_id, task, Authority(external_writes=True))
        raise RuntimeError("simulated controller crash")

    monkeypatch.setattr(engine.journal, "release_resources", interrupted)
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        engine.execute(run)
    monkeypatch.undo()
    other = engine.journal.create("Other task", config())
    assert not engine.journal.acquire_resources(other, "other", Authority(external_writes=True))
    engine.execute(run)
    assert model.workers == 1
    assert engine.journal.events(run)[-1]["kind"] == "completed"
    assert engine.journal.acquire_resources(other, "other", Authority(external_writes=True))
