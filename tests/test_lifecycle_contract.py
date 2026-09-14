"""Model-free lifecycle contracts, actual candidates, stop admission and durable replay.

Semantic judgments below are deterministic reviewers, not evidence of live LLM quality.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.models import (
    Check,
    Clarification,
    ClarificationNeed,
    Criterion,
    DownstreamOutcome,
    Finding,
    Goal,
    Plan,
    Requirement,
    StagePolicy,
    Task,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.native import provider_schema
from ai_employee.semantics import COMPLETION, EXECUTION_CHECKS, LIFECYCLE
from ai_employee.stage_contracts import OutputViolation, StageContract, repair_feedback

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_semantic_contracts import SemanticModel

ORIGINAL = (
    "Deliver a program that writes processed.txt containing 'finished'.\n"
    "The receiving operator will run it once after you hand it over."
)
PROGRAM = "from pathlib import Path\nPath('processed.txt').write_text('finished')\n"


def handoff():
    return Clarification(
        clarified_goal=ORIGINAL,
        criteria=(
            Criterion(
                id="deliverable",
                description="A valid program and instructions implementing the requested output, "
                "ready for the receiving operator to execute after handoff.",
            ),
        ),
        requirements=(Requirement(original_refs=("s1", "s2"), criteria=("deliverable",)),),
        downstream_outcomes=(
            DownstreamOutcome(
                description="Run the program once and obtain processed.txt containing 'finished'.",
                owner="receiving operator",
                original_refs=("s1", "s2"),
                criteria=("deliverable",),
            ),
        ),
    )


def policy(**changes):
    return config(task_attempts=1, replans=0).model_copy(
        update={"clarification": StagePolicy(model="test", review="always"), **changes}
    )


class HandoffModel(OfflineModel):
    def __init__(
        self, *, broken=False, bad_clarification=False, bad_plan=False, bad_reference=False
    ):
        super().__init__()
        self.prompts = []
        self.broken = broken
        self.bad_clarification = bad_clarification
        self.bad_plan = bad_plan
        self.bad_reference = bad_reference
        self.clarifications = 0
        self.plans = 0

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        body = json.loads(prompt)
        self.prompts.append(body)
        self.calls.append(schema.__name__)
        stage = body["stage_contract"]["stage"]
        contract = body["stage_contract"]
        assert contract["semantics"]["completion"] == COMPLETION
        assert contract["lifecycle"]["boundaries"] == LIFECYCLE
        if schema is Clarification:
            self.clarifications += 1
            value = handoff()
            if self.bad_clarification and self.clarifications == 1:
                payload = value.model_dump()
                payload["criteria"][0]["description"] = "processed.txt already exists"
                value = Clarification.model_validate(payload)
            if self.bad_reference and self.clarifications == 1:
                payload = value.model_dump()
                payload["downstream_outcomes"][0]["original_refs"] = ["foreign"]
                value = Clarification.model_validate(payload)
        elif stage.endswith("_review"):
            proposal = body["proposal"]
            # Simulate a semantic reviewer using the original and boundary contract.
            # This does not assert that arbitrary natural language is schema-decidable.
            if stage == "clarification_review":
                passed = "after you hand it over" in body["original"]["original_input"]
                passed &= "already exists" not in proposal["criteria"][0]["description"]
                if passed:
                    assert body["source_evidence"]["downstream_outcomes"][0]["fragments"] == [
                        "Deliver a program that writes processed.txt containing 'finished'.\n",
                        "The receiving operator will run it once after you hand it over.",
                    ]
            elif stage == "planning_review":
                passed = "already exists" not in proposal["tasks"][0]["criteria"][0]["description"]
            else:
                passed = True
            value = Verification(
                findings=(
                    Finding(
                        criterion_id="review",
                        passed=passed,
                        evidence="Reviewed original authorization and pre-handoff evidence route.",
                    ),
                ),
                summary="Accepted"
                if passed
                else "Post-handoff results cannot precede promotion, "
                "and direct execution cannot be weakened to delivery.",
            )
        elif schema is Plan:
            self.plans += 1
            goal = Goal.model_validate(body["goal"])
            criteria = goal.specification.criteria
            if self.bad_plan and self.plans == 1:
                criteria = (
                    Criterion(id="deliverable", description="processed.txt already exists"),
                )
            value = Plan(
                tasks=(
                    Task(
                        id="prepare",
                        description="Prepare the deliverable without executing it.",
                        criteria=criteria,
                        required_evidence=("Program and run instructions",),
                        verification_plan="Inspect and compile program, compare against linked "
                        "downstream requirements before handoff; do not run the external step.",
                    ),
                ),
                result_task="prepare",
            )
        elif schema is WorkerResult:
            self.workers += 1
            assert body["context"]["goal"]["specification"]["downstream_outcomes"]
            (workspace / "program.py").write_text("invalid !" if self.broken else PROGRAM)
            (workspace / "README.txt").write_text("Run program.py once after handoff.")
            value = WorkerResult(
                status="completed",
                summary="Executable deliverable prepared; receiving operator has not run it.",
            )
        elif schema is Verification:
            self.verifications += 1
            assert body["goal"]["specification"]["downstream_outcomes"]
            assert not (workspace / "processed.txt").exists()
            program = (workspace / "program.py").read_text()
            try:
                compile(program, "program.py", "exec")
                passed = program == PROGRAM and (workspace / "README.txt").is_file()
            except SyntaxError:
                passed = False
            value = Verification(
                findings=tuple(
                    Finding(
                        criterion_id=c["id"],
                        passed=passed,
                        evidence="Inspected actual program and instructions; "
                        "external execution is not claimed.",
                    )
                    for c in body["criteria"]
                ),
                summary="Inspected deliverable against downstream requirements",
            )
        else:
            raise AssertionError(stage)
        return schema.model_validate(value.model_dump()), Usage(tokens=10, cost=0)


def test_handoff_is_verified_before_external_execution_and_survives_reopen(tmp_path):
    model = HandoffModel()
    engine, source = runtime(tmp_path, model)
    run = engine.start(ORIGINAL, policy(), source)
    view = projection(engine.journal, run)
    assert view["status"] == "completed"
    assert (
        view["goal"]["specification"]["downstream_outcomes"]
        == handoff().model_dump(mode="json")["downstream_outcomes"]
    )
    goal = Goal.model_validate(view["goal"])
    assert Goal.model_validate_json(goal.canonical()).digest == goal.digest
    assert model.workers == 1 and model.verifications == 2
    assert len(model.prompts) == 6  # Existing mandatory clarification review; no new stage.
    calls = len(model.prompts)
    reopened = Engine(Journal(tmp_path / "history.db"), engine.candidates, model, engine.root)
    reopened.execute(run)
    assert len(model.prompts) == calls
    destination = tmp_path / "published"
    reopened.promote(run, destination)
    assert not (destination / "processed.txt").exists()
    events = reopened.journal.events(run)
    kinds = [event["kind"] for event in events]
    assert (
        kinds.index("candidate")
        < kinds.index("accepted")
        < kinds.index("completed")
        < kinds.index("promoted")
    )
    verification_events = [e["body"] for e in events if e["kind"] == "verification"]
    assert [e["goal_level"] for e in verification_events] == [False, True]
    assert all(e["passed"] for e in verification_events)
    # This is the separate receiving actor, after publication, not a Fleet runtime action.
    subprocess.run([sys.executable, "program.py"], cwd=destination, check=True)
    assert (destination / "processed.txt").read_text() == "finished"
    assert projection(reopened.journal, run)["goal"] == view["goal"]


@pytest.mark.parametrize("fault", ["bad_clarification", "bad_plan"])
def test_circular_future_evidence_is_revised_before_worker(tmp_path, fault):
    model = HandoffModel(**{fault: True})
    engine, source = runtime(tmp_path, model)
    run = engine.start(
        ORIGINAL, policy(planning=StagePolicy(model="test", review="always")), source
    )
    assert projection(engine.journal, run)["status"] == "completed"
    reviews = [e["body"] for e in engine.journal.events(run) if e["kind"] == "review_diagnostic"]
    stage = "clarification" if fault == "bad_clarification" else "planning"
    assert [e["accepted"] for e in reviews if e["stage"] == stage] == [False, True]
    first_worker = next(
        i for i, p in enumerate(model.prompts) if p["stage_contract"]["stage"] == "worker"
    )
    prior = model.prompts[:first_worker]
    assert sum(p["stage_contract"]["stage"] == stage + "_review" for p in prior) == 2
    assert model.workers == 1


def test_broken_handoff_is_not_published(tmp_path):
    model = HandoffModel(broken=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare(ORIGINAL, policy(), source)
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.execute(run)
    assert any(
        e["kind"] == "verification" and not e["body"]["passed"] for e in engine.journal.events(run)
    )
    with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
        engine.promote(run, tmp_path / "published")
    assert not (tmp_path / "published").exists()


def test_direct_execution_cannot_be_silently_replaced_by_handoff(tmp_path):
    model = HandoffModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare(
        "Execute the program yourself and produce processed.txt now.", policy(), source
    )
    # Use valid references to isolate semantic review from source validation.
    original_generate = model.generate

    def generate(*args, **kwargs):
        value, usage = original_generate(*args, **kwargs)
        if isinstance(value, Clarification):
            payload = value.model_dump()
            payload["requirements"][0]["original_refs"] = ["s1"]
            payload["downstream_outcomes"][0]["original_refs"] = ["s1"]
            value = Clarification.model_validate(payload)
        return value, usage

    model.generate = generate
    with pytest.raises(ValueError, match="CLARIFICATION_REJECTED"):
        engine.execute(run)
    assert model.workers == 0
    assert not any(e["kind"] == "goal" for e in engine.journal.events(run))


def test_downstream_schema_source_validation_and_repair_share_contract():
    cfg = config()
    binding = StageContract.bind("clarification", {"original_input": ORIGINAL}, cfg)
    value = handoff()
    assert binding.validate(value, {}, cfg) == value
    schema = provider_schema(Clarification, binding.projection())
    assert schema["$defs"]["DownstreamOutcome"]["properties"]["original_refs"]["items"]["enum"] == [
        "s1",
        "s2",
    ]
    payload = value.model_dump()
    payload["downstream_outcomes"][0]["original_refs"] = ["unknown"]
    bad = Clarification.model_validate(payload)
    with pytest.raises(OutputViolation, match="INVALID_ORIGINAL_REFERENCES") as fault:
        binding.validate(bad, {}, cfg)
    feedback = repair_feedback(str(fault.value), fault.value.details)
    assert feedback["path"] == "downstream_outcomes.original_refs"
    with pytest.raises(ValueError, match="INVALID_ORIGINAL_REFERENCES"):
        Goal(original_input=ORIGINAL, specification=bad)


@pytest.mark.parametrize(
    "refs,outcome",
    [
        (["unknown"], "artifact"),
        (["deliverable", "deliverable"], "artifact"),
        (["deliverable"], "external_effect"),
    ],
)
def test_downstream_cannot_reference_unknown_duplicate_or_external_completion(refs, outcome):
    payload = handoff().model_dump()
    payload["criteria"][0]["outcome"] = outcome
    payload["downstream_outcomes"][0]["criteria"] = refs
    with pytest.raises(ValidationError, match="INVALID_DOWNSTREAM_CRITERIA"):
        Clarification.model_validate(payload)


def blocked(*kinds):
    return Clarification(
        clarified_goal="Perform the requested external operation",
        criteria=(
            Criterion(
                id="operation", description="Actual operation completed", outcome="external_effect"
            ),
        ),
        requirements=(Requirement(original_refs=("s1",), criteria=("operation",)),),
        unresolved=tuple(
            ClarificationNeed(
                kind=kind,
                question="Which target?" if kind == "human_input" else None,
                reason="Required access is unavailable",
                original_refs=("s1",),
                evidence="Inspected the configured environment and available checks",
            )
            for kind in kinds
        ),
    )


@pytest.mark.parametrize(
    "kinds,expected",
    [
        (("environment",), None),
        (("environment", "human_input"), None),
        ((), "MISSING_EXTERNAL_EVIDENCE_CHECK"),
        (("human_input",), "MISSING_EXTERNAL_EVIDENCE_CHECK"),
        (("investigation", "environment"), "CLARIFICATION_REQUIRES_INVESTIGATION"),
    ],
)
def test_stop_exempts_only_execution_admission_checks(kinds, expected):
    cfg = config()
    binding = StageContract.bind("clarification", {"original_input": "Perform operation"}, cfg)
    value = blocked(*kinds)
    assert binding.projection()["clarification"]["execution_checks"] == EXECUTION_CHECKS
    if expected:
        with pytest.raises(OutputViolation, match=expected):
            binding.validate(value, {}, cfg)
    else:
        assert binding.validate(value, {}, cfg) == value
        with pytest.raises(ValidationError, match="CLARIFICATION_NOT_READY"):
            Goal(original_input="Perform operation", specification=value)


def test_stop_retains_unknown_check_and_mandatory_execution_guards():
    check = Check(id="protected", argv=("true",), evidence_kind="external_effect")
    cfg = config().model_copy(update={"checks": (check,), "mandatory_checks": ("protected",)})
    binding = StageContract.bind("clarification", {"original_input": "Perform operation"}, cfg)
    assert binding.validate(blocked("environment"), {}, cfg).disposition == "stop"
    with pytest.raises(OutputViolation, match="MANDATORY_CHECK_OMITTED"):
        binding.validate(blocked(), {}, cfg)
    payload = blocked("environment").model_dump()
    payload["criteria"][0]["checks"] = ["invented"]
    with pytest.raises(OutputViolation, match="UNDECLARED_VERIFICATION_CHECK"):
        binding.validate(Clarification.model_validate(payload), {}, cfg)
    payload["criteria"][0]["checks"] = []
    payload["unresolved"][0]["original_refs"] = ["invented"]
    with pytest.raises(OutputViolation, match="INVALID_ORIGINAL_REFERENCES"):
        binding.validate(Clarification.model_validate(payload), {}, cfg)


class StopModel(OfflineModel):
    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        self.calls.append(schema.__name__)
        if schema is Clarification:
            return blocked("environment"), Usage()
        body = json.loads(prompt)
        assert body["stage_contract"]["stage"] == "clarification_review"
        assert body["stage_contract"]["clarification"]["execution_checks"] == EXECUTION_CHECKS
        return Verification(
            findings=(
                Finding(
                    criterion_id="review",
                    passed=True,
                    evidence="Blocker grounds reviewed; no execution admitted",
                ),
            ),
            summary="Stop is appropriate",
        ), Usage()


def test_real_engine_reviews_and_persists_stop_without_output_repair_or_work(tmp_path):
    model = StopModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Perform operation", policy(), source)
    with pytest.raises(Stopped, match="CLARIFICATION_ENVIRONMENT_BLOCKED"):
        engine.execute(run)
    events = engine.journal.events(run)
    assert projection(engine.journal, run)["status"] == "stopped"
    assert model.calls == ["Clarification", "Verification"]
    assert not {e["kind"] for e in events} & {"output_rejected", "goal", "plan", "attempt_started"}
    before = list(model.calls)
    reopened = Engine(Journal(tmp_path / "history.db"), engine.candidates, model, engine.root)
    with pytest.raises(Stopped):
        reopened.execute(run)
    assert model.calls == before


def test_verification_reviewer_gets_exact_original_evidence_and_scope(tmp_path):
    class ReceiptModel(SemanticModel):
        def generate(self, *args, **kwargs):
            value, usage = super().generate(*args, **kwargs)
            payload = value.model_dump()
            if isinstance(value, Clarification):
                payload["criteria"][0]["checks"] = ["proof"]
            elif isinstance(value, Plan):
                payload["tasks"][0]["criteria"][0]["checks"] = ["proof"]
            return type(value).model_validate(payload), usage

    model = ReceiptModel()
    engine, source = runtime(tmp_path, model)
    cfg = config().model_copy(
        update={
            name: StagePolicy(model="test", review="always")
            for name in ("clarification", "planning", "worker", "verification")
        }
    )
    cfg = cfg.model_copy(update={"checks": (Check(id="proof", argv=("true",)),)})
    run = engine.start("Write result", cfg, source)
    assert projection(engine.journal, run)["status"] == "completed"
    originals = [
        p
        for p in model.prompts
        if p["stage_contract"]["stage"] in {"task_verification", "goal_verification"}
    ]
    reviews = [p for p in model.prompts if p["stage_contract"]["stage"] == "verification_review"]
    assert len(originals) == len(reviews) == 2
    for original, review in zip(originals, reviews, strict=True):
        for key in (
            "goal",
            "task",
            "criteria",
            "candidate",
            "checks",
            "upstream_check_evidence",
            "worker_result",
            "verification_scope",
        ):
            assert review["original"][key] == original[key]
        assert (
            review["stage_contract"]["lifecycle"]["verification_scope"]
            == original["verification_scope"]
        )
        assert "candidate" in review["stage_contract"]["lifecycle"]["available_inputs"]
    assert originals[0]["worker_result"] is not None
    assert originals[1]["task"] is None
    assert originals[0]["checks"] and originals[1]["checks"]
    assert originals[1]["upstream_check_evidence"]
    assert reviews[0]["stage_contract"]["lifecycle"]["assessment"] == "actual_candidate_evidence"
    assert reviews[0]["stage_contract"]["lifecycle"]["snapshot_tree"]
    assert "input_tree" in reviews[0]["stage_contract"]["lifecycle"]["available_inputs"]
    context = {
        "previous_plan": {},
        "accepted": {"task": {"tree": "old"}},
        "evidence": [{"kind": "verification"}],
    }
    recovery = StageContract.bind("recovery_review", {"original": context}, cfg)
    assert {"accepted", "evidence"} <= set(recovery.lifecycle["available_inputs"])


def test_previous_reference_history_remains_readable_without_resuming(tmp_path, monkeypatch):
    import ai_employee.history as history
    from ai_employee.stage_contracts import VERSION

    model = OfflineModel()
    engine, source = runtime(tmp_path, model)
    monkeypatch.setattr(history, "VERSION", "stage-contract-5")
    run = engine.prepare(ORIGINAL, config(), source)
    legacy = handoff().model_dump(mode="json")
    del legacy["downstream_outcomes"]
    engine.journal.append(
        run,
        "goal",
        goal={"original_input": ORIGINAL, "specification": legacy, "mandatory_checks": []},
    )
    monkeypatch.setattr(history, "VERSION", VERSION)
    view = projection(engine.journal, run)
    assert view["goal"]["specification"] == legacy
    assert view["source_evidence"]["requirements"][0]["fragments"] == ORIGINAL.splitlines(
        keepends=True
    )
    with pytest.raises(Stopped, match="CONTRACT_VERSION_CHANGED"):
        engine.execute(run)
    assert not model.calls


def test_downstream_does_not_waive_direct_external_goal_checks():
    check = Check(id="protected", argv=("true",), evidence_kind="external_effect")
    cfg = config().model_copy(update={"checks": (check,)})
    payload = handoff().model_dump(mode="json")
    direct = Criterion(
        id="direct",
        description="Fleet must complete the external operation itself",
        outcome="external_effect",
        checks=("protected",),
    )
    payload["criteria"].append(direct.model_dump(mode="json"))
    payload["requirements"].append({"original_refs": ["s1"], "criteria": ["direct"]})
    value = Clarification.model_validate(payload)
    StageContract.bind("clarification", {"original_input": ORIGINAL}, cfg).validate(value, {}, cfg)
    goal = Goal(original_input=ORIGINAL, specification=value)
    context = {"goal": goal.model_dump(mode="json")}
    plan = Plan(
        tasks=(
            Task(
                id="prepare",
                description="Only deliver artifact",
                criteria=handoff().criteria,
                verification_plan="Inspect artifact",
            ),
        ),
        result_task="prepare",
    )
    with pytest.raises(OutputViolation, match="EXTERNAL_GOAL_WEAKENED"):
        StageContract.bind("planning", context, cfg).validate(plan, context, cfg)


def test_downstream_reference_repair_uses_unchanged_contract_before_review(tmp_path):
    model = HandoffModel(bad_reference=True)
    engine, source = runtime(tmp_path, model)
    run = engine.start(ORIGINAL, policy(), source)
    proposals = [p for p in model.prompts if p["stage_contract"]["stage"] == "clarification"]
    assert len(proposals) == 2
    assert proposals[0]["stage_contract"] == proposals[1]["stage_contract"]
    feedback = proposals[1]["contract_feedback"]
    assert feedback["code"] == "INVALID_ORIGINAL_REFERENCES"
    assert feedback["violation"]["path"] == "downstream_outcomes.original_refs"
    assert projection(engine.journal, run)["status"] == "completed"
    assert model.workers == 1
