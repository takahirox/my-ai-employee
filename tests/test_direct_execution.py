"""Bounded direct execution through the real runtime and immutable candidates."""

import json

import pytest

from ai_employee.engine import Engine
from ai_employee.history import Stopped
from ai_employee.models import (
    Authority,
    Clarification,
    DirectExecution,
    Finding,
    Plan,
    Verification,
    WorkerResult,
)

from .test_autonomous_runtime import OfflineModel, config, runtime


class DirectModel(OfflineModel):
    def __init__(self, behavior="complete"):
        super().__init__()
        self.behavior = behavior
        self.prompts = []
        self.checks = 0

    def generate(
        self,
        policy,
        prompt,
        schema,
        workspace,
        authority,
        timeout,
        cancelled,
        observer=None,
        observation=None,
    ):
        body = json.loads(prompt)
        self.prompts.append(body)
        if schema is Verification and "proposal" in body:
            self.calls.append("Review")
            return Verification(
                findings=(Finding(criterion_id="review", passed=True, evidence="reviewed"),),
                summary="reviewed",
            ), self.usage()
        if schema is WorkerResult and "direct_execution" in body:
            assert timeout is not None and timeout <= 60
            assert authority == Authority()
            assert body["context"]["goal"]["specification"]["observations"]
            (workspace / "partial.txt").write_text("preserve me")
            if self.behavior == "timeout":
                raise TimeoutError("simulated timeout")
            if self.behavior == "tokens":
                observation({"event": "usage_observed", "tokens": 100001})
            if self.behavior == "quota":
                observation({"event": "usage_limit", "source": "provider"})
            if self.behavior == "crash":
                raise RuntimeError("controller interrupted")
            if self.behavior in ("failed", "uncertain", "authority_requested"):
                self.calls.append("WorkerResult")
                return WorkerResult(
                    status=self.behavior,
                    summary="need normal planning",
                    authority_request=Authority()
                    if self.behavior == "authority_requested"
                    else None,
                ), self.usage()
        if schema is Plan and self.behavior != "complete":
            assert (workspace / "partial.txt").read_text() == "preserve me"
            assert body["direct_execution_handoff"]["authoritative"] is False
        result, usage = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, observer, observation
        )
        if schema is Clarification:
            result = result.model_copy(update={"observations": ("Inspected input snapshot.",)})
        return result, usage

    @staticmethod
    def usage():
        from ai_employee.models import Usage

        return Usage(tokens=10, cost=0)

    def check(self, argv, workspace, timeout, cancelled):
        self.checks += 1
        return True, "checked actual output"


def direct_config():
    return config().model_copy(update={"direct_execution": DirectExecution()})


def test_direct_candidate_two_acceptances_one_independent_verifier_and_replay(tmp_path):
    model = DirectModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", direct_config(), source)
    engine.promote(run, tmp_path / "published")
    assert (tmp_path / "published/result.txt").read_text() == "correct"
    assert not (tmp_path / "published/verifier-output.txt").exists()
    assert model.calls == ["Clarification", "WorkerResult", "Verification"]
    events = engine.journal.events(run)
    verifications = [e for e in events if e["kind"] == "verification"]
    assert len(verifications) == 2
    assert verifications[1]["body"]["reused_from"] == __import__(
        "ai_employee.stage_contracts", fromlist=["digest"]
    ).digest(verifications[0])
    assert [e["kind"] for e in events if e["kind"] in ("accepted", "completed")] == [
        "accepted",
        "completed",
    ]
    engine.execute(run)
    assert len(model.calls) == 3


@pytest.mark.parametrize("behavior", ["failed", "timeout", "tokens", "authority_requested"])
def test_direct_fallback_preserves_partial_work_without_recovery_or_budget_reset(
    tmp_path, behavior
):
    model = DirectModel(behavior)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", direct_config(), source)
    engine.promote(run, tmp_path / "published")
    assert (tmp_path / "published/partial.txt").read_text() == "preserve me"
    events = engine.journal.events(run)
    assert sum(e["kind"] == "direct_started" for e in events) == 1
    assert sum(e["kind"] == "direct_fallback" for e in events) == 1
    assert sum(e["kind"] == "attempt_started" for e in events) == 2
    assert not any(e["kind"] in ("approval_wait", "transport_failed", "recovery") for e in events)
    if behavior == "tokens":
        assert engine.journal.budget(run)["measured_usage"]["tokens"] >= 100001
    before = list(model.calls)
    engine.execute(run)
    assert model.calls == before


def test_quota_stops_without_fallback(tmp_path):
    model = DirectModel("quota")
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", direct_config(), source)
    with pytest.raises(Stopped, match="USAGE_LIMIT"):
        engine.execute(run)
    assert not any(e["kind"] == "direct_fallback" for e in engine.journal.events(run))


def test_uncertain_effect_never_falls_back(tmp_path):
    engine, source = runtime(tmp_path, DirectModel("uncertain"))
    run = engine.start("Write result", direct_config(), source)
    events = engine.journal.events(run)
    assert any(e["kind"] == "uncertain" for e in events)
    assert not any(e["kind"] == "direct_fallback" for e in events)


def test_interrupted_worker_goes_to_normal_path_on_resume(tmp_path):
    model = DirectModel("crash")
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", direct_config(), source)
    with pytest.raises(RuntimeError, match="controller interrupted"):
        engine.execute(run)
    # Recreate the controller using durable state; no second direct attempt.
    engine = Engine(engine.journal, engine.candidates, model, engine.root)
    engine.execute(run)
    engine.promote(run, tmp_path / "published")
    assert (tmp_path / "published/partial.txt").exists()
    assert sum("direct_execution" in p for p in model.prompts) == 1


def test_required_reviews_remain_and_planning_review_uses_normal_route(tmp_path):
    model = DirectModel()
    engine, source = runtime(tmp_path, model)
    cfg = direct_config()
    cfg = cfg.model_copy(
        update={
            "clarification": cfg.clarification.model_copy(update={"review": "always"}),
            "worker": cfg.worker.model_copy(update={"review": "always"}),
            "verification": cfg.verification.model_copy(update={"review": "always"}),
        }
    )
    run = engine.start("Write result", cfg, source)
    assert model.calls == [
        "Clarification",
        "Review",
        "WorkerResult",
        "Review",
        "Verification",
        "Review",
    ]
    engine.promote(run, tmp_path / "published")

    (tmp_path / "normal").mkdir()
    model2 = DirectModel()
    engine2, source2 = runtime(tmp_path / "normal", model2)
    cfg = direct_config()
    cfg = cfg.model_copy(update={"planning": cfg.planning.model_copy(update={"review": "always"})})
    run2 = engine2.start("Write result", cfg, source2)
    assert "Plan" in model2.calls
    assert not any("direct_execution_handoff" in p for p in model2.prompts)
    assert not any(e["kind"] == "direct_started" for e in engine2.journal.events(run2))


def test_historical_config_digest_unchanged():
    cfg = config()
    body = cfg.model_dump(mode="json")
    body.pop("snapshot_max_bytes")
    body.pop("command_capture")
    body.pop("direct_execution")
    assert cfg.canonical() == json.dumps(body, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "mutation", ["candidate", "criteria", "policy", "lineage", "authority", "missing_check"]
)
def test_joint_evidence_rejects_non_equivalent_context(tmp_path, mutation):
    from ai_employee.models import Candidate, Criterion, Goal, Task

    engine, source = runtime(tmp_path, DirectModel())
    cfg = direct_config()
    run = engine.start("Write result", cfg, source)
    events = engine.journal.events(run)
    goal = Goal.model_validate(next(e["body"]["goal"] for e in events if e["kind"] == "goal"))
    task = Task.model_validate(
        next(e["body"]["plan"]["tasks"][0] for e in events if e["kind"] == "plan")
    )
    candidate = engine.result(run)
    if mutation == "candidate":
        candidate = Candidate.model_validate({**candidate.model_dump(), "tree": "0" * 64})
    elif mutation == "criteria":
        task = task.model_copy(update={"criteria": (Criterion(id="different", description="new"),)})
    elif mutation == "policy":
        cfg = cfg.model_copy(
            update={"verification": cfg.verification.model_copy(update={"effort": "low"})}
        )
    elif mutation == "lineage":
        candidate = candidate.model_copy(update={"upstream": ("0" * 64,)})
    elif mutation == "authority":
        candidate = candidate.model_copy(update={"authority_version": 1})
    else:
        goal = goal.model_copy(update={"mandatory_checks": ("missing",)})
    assert engine._joint_record(run, cfg, goal, task, candidate) is None


@pytest.mark.parametrize("check_passes", [True, False])
def test_mandatory_check_and_preservation_still_gate_both_acceptances(tmp_path, check_passes):
    from ai_employee.models import Check, Criterion

    class CheckedModel(DirectModel):
        def generate(self, *args, **kwargs):
            result, usage = super().generate(*args, **kwargs)
            if isinstance(result, Clarification):
                result = result.model_copy(
                    update={
                        "criteria": (
                            Criterion(
                                id="result",
                                description="result exists and input preserved",
                                checks=("required",),
                                preserved_paths=("input.txt",),
                            ),
                        )
                    }
                )
            return result, usage

        def check(self, *args):
            self.checks += 1
            return check_passes, "real check receipt"

    model = CheckedModel()
    engine, source = runtime(tmp_path, model)
    (source / "input.txt").write_text("original")
    cfg = direct_config().model_copy(
        update={
            "checks": (Check(id="required", argv=("check",)),),
            "mandatory_checks": ("required",),
        }
    )
    run = engine.prepare("Write result", cfg, source)
    if not check_passes:
        # Stop after fallback before normal planning; a failed protected check
        # must not have yielded either direct acceptance boundary.
        def stop_plan(*args):
            raise RuntimeError("normal path reached")

        engine._plan = stop_plan
        with pytest.raises(RuntimeError, match="normal path reached"):
            engine.execute(run)
        assert not any(e["kind"] in ("accepted", "completed") for e in engine.journal.events(run))
    else:
        engine.execute(run)
        engine.promote(run, tmp_path / "published")
        assert model.checks == 1
        assert model.verifications == 1
        assert (tmp_path / "published/input.txt").read_text() == "original"


def test_completed_worker_reused_after_interrupted_verification(tmp_path):
    class InterruptedVerifier(DirectModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            if schema is Verification and self.behavior == "complete":
                self.behavior = "resumed"
                raise RuntimeError("verification interrupted")
            return super().generate(policy, prompt, schema, *args, **kwargs)

    model = InterruptedVerifier()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", direct_config(), source)
    with pytest.raises(RuntimeError, match="verification interrupted"):
        engine.execute(run)
    engine.execute(run)
    engine.promote(run, tmp_path / "published")
    assert model.workers == 1
    assert "Plan" not in model.calls
    assert model.verifications == 1


def test_fallback_resume_does_not_repeat_direct_attempt_or_lose_partial_artifacts(tmp_path):
    model = DirectModel("failed")
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", direct_config(), source)
    original_plan = engine._plan

    def interrupt(*args):
        raise RuntimeError("planning interrupted")

    engine._plan = interrupt
    with pytest.raises(RuntimeError, match="planning interrupted"):
        engine.execute(run)
    engine._plan = original_plan
    engine.execute(run)
    engine.promote(run, tmp_path / "published")
    assert sum("direct_execution" in p for p in model.prompts) == 1
    assert (tmp_path / "published/partial.txt").read_text() == "preserve me"


def test_run_budget_exhaustion_is_not_a_direct_fallback(tmp_path):
    from ai_employee.models import Limits

    model = DirectModel("tokens")
    engine, source = runtime(tmp_path, model)
    cfg = direct_config().model_copy(update={"limits": Limits(tokens=50000, reservation_tokens=10)})
    run = engine.prepare("Write result", cfg, source)
    with pytest.raises(Stopped, match="RUN_BUDGET_EXHAUSTED"):
        engine.execute(run)
    assert not any(e["kind"] == "direct_fallback" for e in engine.journal.events(run))


@pytest.mark.parametrize("status", ["usage_limit", "uncertain"])
def test_negative_safety_report_wins_over_local_token_threshold(tmp_path, status):
    from ai_employee.models import Usage

    class SafetyReport(DirectModel):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            if schema is WorkerResult:
                return WorkerResult(status=status, summary="stop"), Usage(tokens=100001)
            return super().generate(policy, prompt, schema, *args, **kwargs)

    engine, source = runtime(tmp_path, SafetyReport())
    run = engine.prepare("Write result", direct_config(), source)
    if status == "usage_limit":
        with pytest.raises(Stopped, match="USAGE_LIMIT"):
            engine.execute(run)
    else:
        engine.execute(run)
    assert not any(e["kind"] == "direct_fallback" for e in engine.journal.events(run))
    assert not any(e["kind"] == "completed" for e in engine.journal.events(run))


def test_direct_retry_uses_remaining_time_and_token_allowance(tmp_path):
    from ai_employee.models import Usage
    from ai_employee.stage_contracts import OutputViolation

    class RetryModel(DirectModel):
        def __init__(self):
            super().__init__()
            self.worker_timeouts = []

        def generate(self, policy, prompt, schema, workspace, authority, timeout, *args, **kwargs):
            if schema is WorkerResult:
                self.worker_timeouts.append(timeout)
                if len(self.worker_timeouts) == 1:
                    raise OutputViolation("INVALID_STRUCTURED_OUTPUT", Usage(tokens=99995))
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, *args, **kwargs
            )

    model = RetryModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", direct_config(), source)

    def stop_plan(*args):
        raise RuntimeError("normal planning reached")

    engine._plan = stop_plan
    with pytest.raises(RuntimeError, match="normal planning reached"):
        engine.execute(run)
    assert len(model.worker_timeouts) == 2
    assert model.worker_timeouts[1] < model.worker_timeouts[0] <= 60
    fallback = next(e["body"] for e in engine.journal.events(run) if e["kind"] == "direct_fallback")
    assert fallback["reason"] == "direct_token_budget"
    assert engine.journal.budget(run)["measured_usage"]["tokens"] >= 100005
