"""Real native decoding through shared contracts, repair and durable acceptance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_employee.capabilities import readiness
from ai_employee.cli import projection
from ai_employee.history import Journal, Stopped
from ai_employee.models import (
    Authority,
    Check,
    Clarification,
    Finding,
    Plan,
    StagePolicy,
    Task,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.native import decode_response, provider_schema
from ai_employee.stage_contracts import OutputViolation, StageContract

from .test_autonomous_runtime import ExternalModel, OfflineModel, clarification, config, runtime
from .test_autonomous_stage_policy import ReviewModel


def stream(payload: Any, tokens: int = 12) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "fixture"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(payload)},
            },
            {"type": "turn.completed", "usage": {"input_tokens": tokens, "output_tokens": 0}},
        )
    )


class RawModel(OfflineModel):
    def __init__(self, fault: str, *, always: bool = False) -> None:
        super().__init__()
        self.fault, self.always = fault, always
        self.prompts: list[dict[str, Any]] = []

    def generate(
        self,
        policy: Any,
        prompt: str,
        schema: Any,
        workspace: Path,
        authority: Any,
        timeout: float,
        cancelled: Any,
        **kwargs: Any,
    ) -> Any:
        body = json.loads(prompt)
        if schema is Clarification:
            self.prompts.append(body)
            payload = clarification().model_dump(mode="json")
            if len(self.prompts) == 1 or self.always:
                if self.fault == "foreign_local":
                    payload["requirements"][0]["criteria"] = ["foreign"]
                elif self.fault == "foreign_check":
                    payload["criteria"][0]["checks"] = ["not-registered"]
                elif self.fault == "malformed":
                    payload = {"private_input": "secret-must-not-be-recorded"}
            return decode_response(stream(payload), schema)
        return super().generate(policy, prompt, schema, workspace, authority, timeout, cancelled)


@pytest.mark.parametrize("fault", ["foreign_local", "foreign_check", "malformed"])
def test_native_response_repairs_and_retains_usage_without_rejected_payload(
    tmp_path: Path, fault: str
):
    model = RawModel(fault)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write result", config(), source)
    view = projection(engine.journal, run)
    assert view["status"] == "completed"
    assert len(model.prompts) == 2
    assert model.prompts[0]["stage_contract"] == model.prompts[1]["stage_contract"]
    assert model.prompts[1]["contract_feedback"]["code"]
    events = engine.journal.events(run)
    measured = [e["body"]["usage"]["tokens"] for e in events if e["kind"] == "settled"]
    assert measured[:2] == [12, 12]
    assert "secret-must-not-be-recorded" not in json.dumps(events)
    rejected = [e for e in events if e["kind"] == "output_rejected"]
    assert len(rejected) == 1 and rejected[0]["body"]["authoritative"] is False
    assert len(view["stage_invocations"]) == 6


def test_provider_projection_constrains_existing_but_allows_initial_local_dag():
    cfg = config().model_copy(update={"checks": (Check(id="public", argv=("true",)),)})
    binding = StageContract.bind("planning", {}, cfg)
    schema = provider_schema(Plan, binding.projection())
    assert schema["$defs"]["Criterion"]["properties"]["checks"]["items"]["enum"] == ["public"]
    assert "enum" not in schema["$defs"]["Task"]["properties"]["dependencies"]["items"]
    first = Task(
        id="a",
        description="research",
        criteria=clarification().criteria,
        verification_plan="Review research against goal",
    )
    second = first.model_copy(update={"id": "b", "dependencies": ("a",)})
    plan, _ = decode_response(
        stream(Plan(tasks=(first, second), result_task="b").model_dump()), Plan
    )
    assert binding.validate(plan, {}, cfg) == plan
    assert readiness(second, cfg)["state"] == "conditionally_plannable"


def test_reviewer_repairs_own_foreign_finding_against_same_target(tmp_path: Path):
    class BadReviewer(ReviewModel):
        def __init__(self):
            super().__init__()
            self.targets: list[str] = []

        def generate(
            self,
            policy: Any,
            prompt: str,
            schema: Any,
            workspace: Path,
            authority: Any,
            timeout: float,
            cancelled: Any,
            **kwargs: Any,
        ) -> Any:
            body = json.loads(prompt)
            if schema is Verification and "proposal" in body:
                self.targets.append(body["stage_contract"]["evaluation_target_digest"])
                if len(self.targets) == 1:
                    return Verification(
                        findings=(
                            Finding(criterion_id="foreign", passed=True, evidence="untrusted"),
                        ),
                        summary="wrong refs",
                    ), Usage(tokens=3)
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )

    model = BadReviewer()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review="always")})
    run = engine.start("Write result", cfg, source)
    assert len(model.targets) == 2 and len(set(model.targets)) == 1
    assert model.calls.count("Clarification") == 1
    assert any(e["kind"] == "review_diagnostic" for e in engine.journal.events(run))


def test_rejected_unresolved_proposal_is_repaired_before_human_wait(tmp_path: Path):
    model = ReviewModel(reject=True)
    model.ambiguous = True
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review="always")})
    with pytest.raises(ValueError, match="CLARIFICATION_REJECTED"):
        engine.start("Write result", cfg, source)
    run = engine.journal.runs()[0]
    assert not any(e["kind"] == "clarification_wait" for e in engine.journal.events(run))
    assert model.calls.count("Clarification") == 2


def test_repair_exhaustion_and_resume_cannot_reset_persisted_attempt_count(tmp_path: Path):
    model = RawModel("foreign_local", always=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.execute(run)
    with pytest.raises(Stopped, match="STAGE_INVOCATION_LIMIT"):
        engine.execute(run)
    assert len(model.prompts) == 2


def test_reservation_crash_keeps_budget_and_counter(tmp_path: Path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    journal.reserve(run, "clarification", call_key="stable", call_limit=1)
    resumed = Journal(journal.path)
    with pytest.raises(Stopped, match="STAGE_INVOCATION_LIMIT"):
        resumed.reserve(run, "clarification", call_key="stable", call_limit=1)
    assert resumed.budget(run)["open_reservations"] == 1
    assert resumed.budget(run)["active_seconds_charged"] > 0


def test_late_response_after_revoke_preserves_usage_but_never_accepts(tmp_path: Path):
    class Late(OfflineModel):
        def generate(self, *args: Any, **kwargs: Any) -> Any:
            result = super().generate(*args, **kwargs)
            engine.journal.stop(run, "AUTHORITY_REVOKED")
            return result

    engine, source = runtime(tmp_path, Late())
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(Stopped):
        engine.execute(run)
    events = engine.journal.events(run)
    assert any(e["kind"] == "settled" and e["body"]["usage"]["tokens"] == 10 for e in events)
    assert not any(e["kind"] in {"goal", "stage_result", "plan"} for e in events)
    with pytest.raises(Stopped):
        engine.journal.append(run, "accepted", candidate={})


def test_duplicate_usage_delivery_is_idempotent_but_conflict_is_rejected(tmp_path: Path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    reservation, _ = journal.reserve(run, "clarification")
    journal.settle(run, reservation, 1, Usage(tokens=7))
    journal.settle(run, reservation, 1, Usage(tokens=7))
    assert len([e for e in journal.events(run) if e["kind"] == "settled"]) == 1
    with pytest.raises(ValueError, match="CONFLICTING_USAGE_DELIVERY"):
        journal.settle(run, reservation, 1, Usage(tokens=8))


def test_malformed_output_after_external_effect_does_not_repeat_work(tmp_path: Path):
    class MalformedExternal(ExternalModel):
        def generate(
            self,
            policy: Any,
            prompt: str,
            schema: Any,
            workspace: Path,
            authority: Any,
            timeout: float,
            cancelled: Any,
            **kwargs: Any,
        ) -> Any:
            result = super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )
            if schema is WorkerResult:
                return decode_response(stream({"malformed": True}), schema)
            return result

    model = MalformedExternal()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            "security": "balanced",
            "authority_ceiling": Authority(external_writes=True),
            "checks": (Check(id="receipt", argv=("true",), evidence_kind="external_effect"),),
        }
    )
    run = engine.start("Write result", cfg, source)
    assert projection(engine.journal, run)["external_outcome_uncertain"]
    assert model.workers == 1
    engine.execute(run)
    assert model.workers == 1
    assert not any(e["kind"] == "accepted" for e in engine.journal.events(run))


def test_unsupported_later_backend_is_detected_before_any_model_call(tmp_path: Path):
    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"verification": StagePolicy(model="test", backend="claude")})
    with pytest.raises(Stopped, match="UNSUPPORTED_BACKEND"):
        engine.start("Write result", cfg, source)
    assert not model.calls


def test_external_plan_without_verification_path_stops_before_worker(tmp_path: Path):
    model = ExternalModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"security": "balanced", "authority_ceiling": Authority(external_writes=True)}
    )
    with pytest.raises(Stopped, match="EXTERNAL_VERIFICATION_PATH_UNAVAILABLE"):
        engine.start("Write result", cfg, source)
    assert model.workers == 0


def test_transport_retry_separate_from_output_repair_and_preserves_binding(tmp_path: Path):
    class Transient(RawModel):
        once = True

        def generate(self, *args: Any, **kwargs: Any) -> Any:
            if self.once:
                self.once = False
                raise TimeoutError("owned process already cleaned up")
            return super().generate(*args, **kwargs)

    model = Transient("foreign_check")
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={"clarification": StagePolicy(model="test", transport_retries=1)}
    )
    run = engine.start("Write result", cfg, source)
    reserved = [
        e["body"]
        for e in engine.journal.events(run)
        if e["kind"] == "reserved" and e["body"]["stage"] == "clarification"
    ]
    assert [x["call_key"].rsplit(":", 1)[1] for x in reserved] == ["output", "transport", "output"]
    assert reserved[0]["binding"] == reserved[2]["binding"]


def test_reviewer_output_repair_uses_fresh_exact_evaluation_files(tmp_path: Path):
    class Mutating(ReviewModel):
        reviews = 0

        def generate(
            self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs
        ):
            body = json.loads(prompt)
            if "proposal" in body:
                self.reviews += 1
                assert not (workspace / "invented-evidence").exists()
                if self.reviews == 1:
                    (workspace / "invented-evidence").write_text("not original evidence")
                    return Verification(findings=(), summary="malformed"), Usage(tokens=2)
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )

    model = Mutating()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review="always")})
    run = engine.start("Write result", cfg, source)
    assert model.reviews == 2
    assert projection(engine.journal, run)["status"] == "completed"


def test_preflight_failure_is_terminal_and_never_invokes_model_or_graph_repair(tmp_path: Path):
    class Unavailable(OfflineModel):
        def preflight(self, *args, **kwargs):
            raise ValueError("missing sandbox")

    model = Unavailable()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(Stopped, match="ENVIRONMENT_UNAVAILABLE"):
        engine.start("Write result", config(), source)
    assert not model.calls
    run = engine.journal.runs()[0]
    assert engine.journal.budget(run)["measured_usage"]["tokens"] == 0


def test_inspector_keeps_other_runs_visible_when_one_record_is_unreadable(tmp_path: Path):
    from ai_employee.inspector import run_list

    journal = Journal(tmp_path / "history.db")
    broken = journal.create("Write result", config())
    good = journal.create("Write result", config())
    with journal.connect() as db:
        db.execute("UPDATE events SET body='{}' WHERE run=?", (broken,))
    rows = {row["run_id"]: row for row in run_list(journal)}
    assert rows[broken]["status"] == "unreadable"
    assert rows[good]["status"] == "running"


def test_acceptance_and_downstream_start_are_blocked_atomically_after_revoke(tmp_path: Path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    journal.append(run, "authority_revocation_requested")
    for kind in ("accepted", "attempt_started", "completed", "authority_applied"):
        with pytest.raises(Stopped):
            journal.append(run, kind)
    with pytest.raises(Stopped):
        journal.append_many(run, (("accepted", {}), ("candidate_reused", {})))
    journal.append(run, "output_rejected", reason="late diagnostic")
    assert journal.events(run)[-1]["kind"] == "output_rejected"


def test_duplicate_accepted_delivery_does_not_create_two_acceptances(tmp_path: Path):
    journal = Journal(tmp_path / "history.db")
    run = journal.create("Write result", config())
    journal.append(run, "accepted", task="a", candidate={"digest": "fixture"})
    journal.append(run, "accepted", task="a", candidate={"digest": "fixture"})
    journal.append_many(run, (("accepted", {"task": "a", "candidate": {"digest": "fixture"}}),))
    assert sum(e["kind"] == "accepted" for e in journal.events(run)) == 1


def test_observed_usage_survives_adapter_cleanup_failure(tmp_path: Path):
    class CleanupFailure(OfflineModel):
        def generate(self, *args, observation=None, **kwargs):
            observation({"event": "usage_observed", "tokens": 17})
            raise RuntimeError("cleanup unconfirmed")

    engine, source = runtime(tmp_path, CleanupFailure())
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(RuntimeError, match="cleanup unconfirmed"):
        engine.execute(run)
    assert engine.journal.budget(run)["measured_usage"]["tokens"] == 17
    assert not any(e["kind"] == "stage_result" for e in engine.journal.events(run))


def test_artifact_check_cannot_establish_external_goal_even_for_offline_worker(tmp_path: Path):
    from ai_employee.models import Criterion

    cfg = config().model_copy(update={"checks": (Check(id="syntax", argv=("true",)),)})
    criterion = Criterion(
        id="remote",
        description="ticket created remotely",
        checks=("syntax",),
        outcome="external_effect",
    )
    task = Task(
        id="create",
        description="create ticket",
        criteria=(criterion,),
        verification_plan="check completion",
    )
    with pytest.raises(Stopped, match="EXTERNAL_VERIFICATION_PATH_UNAVAILABLE"):
        readiness(task, cfg)
    body = clarification().model_dump(mode="json")
    body["criteria"][0].update(outcome="external_effect", checks=["syntax"])
    parsed, _ = decode_response(stream(body), Clarification)
    binding = StageContract.bind("clarification", {"original_input": "Write result"}, cfg)
    with pytest.raises(OutputViolation, match="MISSING_EXTERNAL_EVIDENCE_CHECK"):
        binding.validate(parsed, {"original_input": "Write result"}, cfg)


def test_explicit_external_evidence_route_is_ready_without_future_evidence():
    from ai_employee.models import Criterion

    cfg = config().model_copy(
        update={
            "checks": (Check(id="signed-receipt", argv=("true",), evidence_kind="external_effect"),)
        }
    )
    task = Task(
        id="observe",
        description="verify remote result",
        criteria=(
            Criterion(
                id="remote",
                description="remote completion",
                outcome="external_effect",
                checks=("signed-receipt",),
            ),
        ),
        verification_plan="validate signed receipt",
        dependencies=("produce-receipt",),
    )
    result = readiness(task, cfg)
    assert result["state"] == "conditionally_plannable"
    assert result["future_evidence_is_observed"] is False


def test_offline_artifact_does_not_complete_external_goal_when_receipt_check_fails(tmp_path: Path):
    class ExternalGoal(OfflineModel):
        def generate(
            self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs
        ):
            result, usage = super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled
            )
            if schema is Clarification:
                body = result.model_dump(mode="json")
                body["criteria"][0].update(outcome="external_effect", checks=["remote"])
                return schema.model_validate(body), usage
            return result, usage

        def check(self, *args, **kwargs):
            return False, "no valid external receipt"

    model = ExternalGoal()
    engine, source = runtime(tmp_path, model)
    cfg = config(replans=0).model_copy(
        update={"checks": (Check(id="remote", argv=("true",), evidence_kind="external_effect"),)}
    )
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.start("Write result", cfg, source)
    run = engine.journal.runs()[0]
    assert model.workers == 1
    assert not any(e["kind"] == "completed" for e in engine.journal.events(run))
    results = [e["body"] for e in engine.journal.events(run) if e["kind"] == "verification"]
    assert results[-1]["goal_level"] and not results[-1]["passed"]
