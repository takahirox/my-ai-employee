"""Clarification semantics through native decoding, admission, repair and durable waits."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection
from ai_employee.container import ContainerModel
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.isolated_worker import IsolatedWorkerProfile
from ai_employee.models import (
    CLARIFICATION_NEEDS,
    CLARIFICATION_RULES,
    Authority,
    Clarification,
    ClarificationNeed,
    Goal,
    StagePolicy,
    Verification,
)
from ai_employee.native import decode_response, provider_schema
from ai_employee.stage_contracts import StageContract

from .test_autonomous_runtime import clarification, config, runtime
from .test_autonomous_stage_policy import ReviewModel
from .test_stage_contracts import stream


def need(kind="investigation", **changes):
    return ClarificationNeed.model_validate(
        {
            "kind": kind,
            "question": "Which result should be produced?" if kind == "human_input" else None,
            "reason": "The requested result needs a decision"
            if kind == "human_input"
            else "Input inspection is pending",
            "original_fragment": "Write result",
            "evidence": "Inspected the original request and permitted input listing",
            **changes,
        }
    )


class Clarifier(ReviewModel):
    def __init__(self, kind="investigation", *, always=False, legacy=False, crash=False):
        super().__init__()
        self.kind, self.always, self.legacy, self.crash = kind, always, legacy, crash
        self.prompts = []
        self.reviews = []
        self.launches = 0

    def generate(self, policy, prompt, schema, workspace, *args, **kwargs):
        body = json.loads(prompt)
        if schema is Verification and "proposal" in body:
            self.reviews.append(body)
        if schema is Clarification:
            self.launches += 1
            if self.crash and self.launches == 2:
                raise SystemExit("controller lost during repair")
            self.prompts.append(body)
            payload = clarification().model_dump(mode="json")
            if not body["clarification_answers"] and (self.always or self.launches == 1):
                payload["unresolved"] = [
                    "Input structure must be investigated during execution. "
                    "No user-intent or authority question remains."
                    if self.legacy
                    else need(self.kind).model_dump(mode="json")
                ]
                # A rejected invocation must not contaminate the next inspection.
                (workspace / "original.txt").write_text("untrusted change")
            else:
                assert (workspace / "original.txt").read_text() == "keep"
            return decode_response(stream(payload), schema)
        return super().generate(policy, prompt, schema, workspace, *args, **kwargs)


@pytest.mark.parametrize("review", ["never", "always"])
@pytest.mark.parametrize("legacy", [False, True])
def test_pending_investigation_repairs_before_wait_even_with_accepting_reviewer(
    tmp_path, review, legacy
):
    model = Clarifier(legacy=legacy)
    engine, source = runtime(tmp_path, model)
    (source / "input").mkdir()
    (source / "input/data.csv").write_text("column,value\nitem,1\n")
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review=review)})
    run = engine.start("Write result", cfg, source)
    assert model.workers == 1 and len(model.prompts) == 2
    assert model.prompts[0]["stage_contract"] == model.prompts[1]["stage_contract"]
    assert model.prompts[0]["input_snapshot"] == model.prompts[1]["input_snapshot"]
    assert "input/data.csv" in model.prompts[1]["input_snapshot"]["files"]
    feedback = model.prompts[1]["contract_feedback"]
    assert feedback["code"] == (
        "INVALID_STRUCTURED_OUTPUT" if legacy else "CLARIFICATION_REQUIRES_INVESTIGATION"
    )
    if not legacy:
        assert feedback["violation"]["rule"] == CLARIFICATION_NEEDS["investigation"]["rule"]
    assert len(model.reviews) == (1 if review == "always" else 0)
    if model.reviews:
        assert (
            model.reviews[0]["stage_contract"]["clarification"]
            == model.prompts[0]["stage_contract"]["clarification"]
        )
        assert model.reviews[0]["input_snapshot"] == model.prompts[0]["input_snapshot"]
    events = Journal(engine.journal.path).events(run)
    assert not any(e["kind"] == "clarification_wait" for e in events)
    assert sum(e["kind"] == "output_rejected" for e in events) == 1
    assert [e["body"]["usage"]["tokens"] for e in events if e["kind"] == "settled"][:2] == [12, 12]


@pytest.mark.parametrize("review", ["never", "always"])
def test_real_human_question_is_durable_and_answer_resumes_same_goal(tmp_path, review):
    model = Clarifier("human_input", always=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review=review)})
    run = engine.start("Write result", cfg, source)
    assert projection(engine.journal, run)["status"] == "waiting_for_clarification"
    assert model.workers == 0
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    reopened.execute(run)
    assert len(model.prompts) == 1
    reopened.answer(run, "Produce the result file")
    reopened.execute(run)
    assert model.prompts[-1]["clarification_answers"] == ["Produce the result file"]
    assert model.workers == 1
    assert reopened.journal.original(run) == "Write result"
    assert projection(reopened.journal, run)["status"] == "completed"
    assert reopened.journal.budget(run)["invocations"] > 1


@pytest.mark.parametrize("review", ["never", "always"])
def test_reported_environment_blocker_stops_without_a_human_question(tmp_path, review):
    model = Clarifier("environment", always=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", review=review)})
    with pytest.raises(Stopped, match="CLARIFICATION_ENVIRONMENT_BLOCKED"):
        engine.start("Write result", cfg, source)
    events = engine.journal.events(engine.journal.runs()[0])
    assert model.workers == 0 and len(model.prompts) == 1
    assert not any(e["kind"] == "clarification_wait" for e in events)
    diagnostic = next(
        e["body"]
        for e in events
        if e["kind"] == "diagnostic"
        and json.loads(e["body"]["context"]["text"]).get("kind")
        == "clarification_environment_blocked"
    )
    assert json.loads(diagnostic["record"]["text"])["unresolved"][0]["kind"] == "environment"
    assert diagnostic["authoritative"] is False


def test_shared_definition_drives_schema_and_mixed_need_decisions():
    binding = StageContract.bind("clarification", {"original_input": "Write result"}, config())
    schema = provider_schema(Clarification, binding.projection())
    fields = schema["$defs"]["ClarificationNeed"]["properties"]
    assert set(fields["kind"]["enum"]) == set(CLARIFICATION_NEEDS)
    assert (
        json.loads(fields["kind"]["description"]) == binding.projection()["clarification"]["needs"]
    )
    for kind, expected in CLARIFICATION_NEEDS.items():
        value = clarification().model_copy(update={"unresolved": (need(kind),)})
        assert value.disposition == expected["action"]
        with pytest.raises(ValidationError):
            Goal(original_input="Write result", specification=value)
    for kinds, expected in (
        (["human_input", "investigation"], "repair"),
        (["human_input", "environment"], "stop"),
    ):
        value = clarification().model_copy(update={"unresolved": tuple(need(k) for k in kinds)})
        assert value.disposition == expected


@pytest.mark.parametrize(
    "kind,question",
    [
        ("human_input", None),
        ("investigation", "Can you inspect?"),
        ("environment", "May I bypass isolation?"),
    ],
)
def test_question_cannot_disagree_with_resolution_action(kind, question):
    with pytest.raises(ValidationError, match="CLARIFICATION_QUESTION_ACTION_MISMATCH"):
        need(kind, question=question)


def test_unrequested_question_does_not_enter_human_wait():
    from ai_employee.stage_contracts import OutputViolation

    value = clarification().model_copy(
        update={
            "unresolved": (need("human_input", original_fragment="Handle conflicting duplicates"),)
        }
    )
    prompt = {"original_input": "Write result"}
    contract = StageContract.bind("clarification", prompt, config())
    with pytest.raises(OutputViolation, match="FOREIGN_CLARIFICATION_REFERENCE"):
        contract.validate(value, prompt, config())


def test_investigation_exhaustion_and_reopen_keep_revision_limit(tmp_path):
    model = Clarifier(always=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write result", config(), source)
    with pytest.raises(RuntimeError, match="OUTPUT_REPAIR_EXHAUSTED"):
        engine.execute(run)
    budget = engine.journal.budget(run)
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    with pytest.raises(Stopped, match="STAGE_INVOCATION_LIMIT"):
        reopened.execute(run)
    assert model.launches == 2 and model.workers == 0
    assert reopened.journal.budget(run)["measured_usage"] == budget["measured_usage"]


def test_crash_preserves_investigation_feedback_and_charges_launch(tmp_path):
    model = Clarifier(crash=True)
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(update={"clarification": StagePolicy(model="test", revisions=2)})
    run = engine.prepare("Write result", cfg, source)
    with pytest.raises(SystemExit, match="controller lost"):
        engine.execute(run)
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    reopened.execute(run)
    assert model.launches == 3 and model.workers == 1
    assert (
        model.prompts[-1]["contract_feedback"]["violation"]["rule"]
        == CLARIFICATION_RULES["CLARIFICATION_REQUIRES_INVESTIGATION"]["rule"]
    )
    assert (
        sum(
            e["kind"] == "reserved" and e["body"]["stage"] == "clarification"
            for e in reopened.journal.events(run)
        )
        == 3
    )


def test_shared_budget_stops_investigation_without_a_new_model_call(tmp_path):
    model = Clarifier()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(Stopped, match="RUN_BUDGET_EXHAUSTED"):
        engine.start("Write result", config(attempts=1), source)
    assert model.launches == 1 and model.workers == 0


def test_input_inventory_is_bounded_and_does_not_claim_complete_listing(tmp_path):
    model = Clarifier()
    engine, source = runtime(tmp_path, model)
    for i in range(70):
        (source / f"input-{i}.txt").write_text("data")
    engine.start("Write result", config(), source)
    snapshot = model.prompts[0]["input_snapshot"]
    assert snapshot["file_count"] == 71 and len(snapshot["files"]) == 64 and snapshot["truncated"]
    assert snapshot["tree"] == model.prompts[0]["input_tree"]


def test_native_prompt_exposes_actual_workspace_without_rewriting_original(tmp_path):
    profile = IsolatedWorkerProfile(image="sha256:" + "a" * 64, auth_file="/explicit-test-auth")
    model = ContainerModel(profile)
    candidate = MagicMock()
    candidate.profile = profile
    candidate.proxy = None
    candidate.run_guarded.return_value = (
        0,
        stream(clarification().model_dump(mode="json")).encode(),
        b"",
    )
    prompt = {
        "original_input": "Write result in /app",
        "input_snapshot": {"files": ["input/data.csv"]},
    }
    with (
        patch.object(model, "_candidate") as factory,
        patch.object(model, "_native_probe"),
        patch.object(model, "_copy_workspace"),
    ):
        factory.return_value.__enter__.return_value = candidate
        model.generate(
            config().clarification,
            json.dumps(prompt),
            Clarification,
            tmp_path,
            Authority(),
            10,
            lambda: False,
        )
    args, kwargs = candidate.run_guarded.call_args
    body = json.loads(kwargs["stdin"])
    assert body["execution_workspace"] == args[0][args[0].index("--cd") + 1] == "/work"
    assert body["original_input"] == prompt["original_input"]
    assert body["input_snapshot"] == prompt["input_snapshot"]


def test_invalid_question_shape_receives_the_same_semantic_rule_on_repair(tmp_path):
    class MissingQuestion(Clarifier):
        def generate(self, policy, prompt, schema, *args, **kwargs):
            result, usage = super().generate(policy, prompt, schema, *args, **kwargs)
            if schema is Clarification and self.launches == 1:
                payload = result.model_dump(mode="json")
                payload["unresolved"][0]["question"] = None
                return decode_response(stream(payload), schema)
            return result, usage

    model = MissingQuestion("human_input")
    engine, source = runtime(tmp_path, model)
    engine.start("Write result", config(), source)
    feedback = model.prompts[1]["contract_feedback"]
    assert feedback["code"] == "CLARIFICATION_QUESTION_ACTION_MISMATCH"
    assert feedback["violation"] == CLARIFICATION_RULES[feedback["code"]]
    assert model.workers == 1


def test_previous_string_question_contract_cannot_resume_under_new_meaning(tmp_path, monkeypatch):
    import ai_employee.history as history

    model = Clarifier()
    engine, source = runtime(tmp_path, model)
    with monkeypatch.context() as changed:
        changed.setattr(history, "VERSION", "stage-contract-2")
        run = engine.prepare("Write result", config(), source)
    with pytest.raises(Stopped, match="CONTRACT_VERSION_CHANGED"):
        engine.execute(run)
    assert model.launches == 0 and model.workers == 0
