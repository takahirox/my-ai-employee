"""Generic execution alternatives through the real engine and candidate lifecycle.

The deterministic model simulates semantic review; these tests prove propagation,
revision and acceptance behavior, not that every live model will judge prose correctly.
"""

import json
import subprocess
import sys

import pytest

from ai_employee.cli import projection
from ai_employee.models import (
    Clarification,
    Criterion,
    DownstreamOutcome,
    Finding,
    Plan,
    Requirement,
    StagePolicy,
    Task,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.native import provider_schema
from ai_employee.semantics import METHOD_SELECTION
from ai_employee.stage_contracts import StageContract

from .test_autonomous_runtime import config, runtime
from .test_lifecycle_contract import PROGRAM, HandoffModel

ORIGINAL = (
    "Obtain processed.txt containing 'finished'.\n"
    "Either execute directly or deliver a program and instructions to the receiving operator, "
    "who will run it once after handoff."
)
DIRECT_ONLY = "Execute directly and produce processed.txt now. Do not delegate execution."
CRITERION = (
    "Either processed.txt contains 'finished' from direct execution, or an executable program "
    "and instructions are ready for the receiving operator to create that exact result "
    "after handoff."
)


def alternatives():
    return Clarification(
        clarified_goal=ORIGINAL,
        criteria=(Criterion(id="deliverable", description=CRITERION),),
        requirements=(Requirement(original_refs=("s1", "s2"), criteria=("deliverable",)),),
        downstream_outcomes=(
            DownstreamOutcome(
                description="If handoff is used, run the delivered program once to create "
                "processed.txt containing 'finished'.",
                owner="receiving operator",
                original_refs=("s1", "s2"),
                criteria=("deliverable",),
            ),
        ),
    )


class AlternativesModel(HandoffModel):
    def __init__(self, *, narrow=False, recover=False, broken=False):
        super().__init__(broken=broken)
        self.narrow = narrow
        self.recover = recover
        self.recovered = False
        self.rejections = 0
        self.goal_digests = []

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
        body = json.loads(prompt)
        stage = body["stage_contract"]["stage"]
        assert body["stage_contract"]["semantics"]["method_selection"] == METHOD_SELECTION
        if schema is Clarification:
            self.prompts.append(body)
            self.clarifications += 1
            value = alternatives()
            if body.get("original_input") == DIRECT_ONLY:
                # Deliberately invalid semantic substitution with structurally valid refs.
                value = value.model_copy(
                    update={
                        "requirements": (
                            Requirement(original_refs=("s1",), criteria=("deliverable",)),
                        ),
                        "downstream_outcomes": tuple(
                            o.model_copy(update={"original_refs": ("s1",)})
                            for o in value.downstream_outcomes
                        ),
                    }
                )
            if self.narrow and self.clarifications == 1:
                value = value.model_copy(
                    update={
                        "clarified_goal": "Execute directly and produce processed.txt now.",
                        "criteria": (
                            Criterion(id="deliverable", description="processed.txt exists now"),
                        ),
                        "downstream_outcomes": (),
                        "assumptions": (
                            "Direct execution was selected, so handoff is not required.",
                        ),
                    }
                )
            if self.clarifications > 1:
                assert body["feedback"]
            return value, Usage(tokens=10, cost=0)
        if stage == "clarification_review":
            self.prompts.append(body)
            candidate = Clarification.model_validate(body["proposal"])
            original = body["original"]["original_input"]
            # Fixture's semantic oracle: alternatives must survive; direct-only may
            # not be weakened. No product code claims to decide arbitrary prose.
            passed = (
                original == ORIGINAL
                and candidate.clarified_goal == ORIGINAL
                and candidate.criteria[0].description == CRITERION
                and bool(candidate.downstream_outcomes)
                and not candidate.assumptions
            )
            self.rejections += not passed
            return Verification(
                findings=(
                    Finding(
                        criterion_id="review",
                        passed=passed,
                        category="satisfied" if passed else "unsupported_expansion",
                        evidence="Compared original methods and timing to proposed criteria.",
                    ),
                ),
                summary="Accepted"
                if passed
                else "Keep the outcome and both allowed methods; preference is not a requirement. "
                "Never delegate a user-mandated direct execution.",
            ), Usage(tokens=10, cost=0)
        if schema is Plan:
            self.prompts.append(body)
            goal = body["goal"]
            self.goal_digests.append(json.dumps(goal, sort_keys=True))
            assert goal["specification"]["criteria"][0]["description"] == CRITERION
            assert not body["stage_contract"]["authority"]["authority_ceiling"]["network_hosts"]
            route = "direct" if self.recover and stage == "planning" else "handoff"
            if stage == "recovery":
                assert body["failed_tasks"]
                self.recovered = True
            return Plan(
                tasks=(
                    Task(
                        id=route,
                        description="Try direct execution"
                        if route == "direct"
                        else "Prepare handoff deliverable",
                        criteria=(
                            Criterion(id="deliverable", description="processed.txt exists now"),
                        )
                        if route == "direct"
                        else alternatives().criteria,
                        verification_plan="Inspect route against original outcome and constraints.",
                    ),
                ),
                result_task=route,
            ), Usage(tokens=10, cost=0)
        if schema is WorkerResult and self.recover and not self.recovered:
            self.prompts.append(body)
            return WorkerResult(
                status="failed", summary="Direct route unavailable in this environment."
            ), Usage(tokens=10, cost=0)
        return super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
        )


def configuration():
    return config(task_attempts=1, replans=1).model_copy(
        update={
            "clarification": StagePolicy(model="test", review="always"),
        }
    )


@pytest.mark.parametrize("narrow,recover", [(False, False), (True, False), (False, True)])
def test_alternatives_survive_review_plan_recovery_and_handoff(tmp_path, narrow, recover):
    model = AlternativesModel(narrow=narrow, recover=recover)
    engine, source = runtime(tmp_path, model)
    run = engine.start(ORIGINAL, configuration(), source)
    view = projection(engine.journal, run)
    assert view["status"] == "completed"
    assert view["goal"]["original_input"] == ORIGINAL
    assert view["goal"]["specification"] == alternatives().model_dump(mode="json")
    assert model.rejections == int(narrow)
    assert model.recovered == recover
    assert len(set(model.goal_digests)) == 1
    assert not any(e["kind"] == "authority_applied" for e in engine.journal.events(run))
    destination = tmp_path / "published"
    engine.promote(run, destination)
    assert not (destination / "processed.txt").exists()
    assert (destination / "program.py").read_text() == PROGRAM
    assert (destination / "README.txt").is_file()
    # Independent receiver acts only after promotion; Fleet did not attest this effect.
    subprocess.run([sys.executable, "program.py"], cwd=destination, check=True)
    assert (destination / "processed.txt").read_text() == "finished"
    calls = len(model.prompts)
    engine.execute(run)
    assert len(model.prompts) == calls
    if not narrow and not recover:
        assert len(model.prompts) == 6  # Existing six stages including clarification review.


def test_user_mandated_direct_method_is_not_replaced(tmp_path):
    model = AlternativesModel()
    engine, source = runtime(tmp_path, model)
    with pytest.raises(ValueError, match="CLARIFICATION_REJECTED"):
        engine.start(DIRECT_ONLY, configuration(), source)
    assert model.rejections == 2
    assert not model.workers


def test_declaration_with_broken_program_is_not_success(tmp_path):
    model = AlternativesModel(broken=True)
    engine, source = runtime(tmp_path, model)
    cfg = configuration().model_copy(update={"limits": config(replans=0, task_attempts=1).limits})
    run = engine.prepare(ORIGINAL, cfg, source)
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.execute(run)
    with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
        engine.promote(run, tmp_path / "published")
    assert not (tmp_path / "published").exists()


def test_same_method_semantics_reach_provider_fields_and_all_stage_contracts():
    cfg = configuration()
    for stage in (
        "clarification",
        "clarification_review",
        "planning",
        "planning_review",
        "worker",
        "task_verification",
        "goal_verification",
        "recovery",
        "recovery_review",
    ):
        contract = StageContract.bind(stage, {}, cfg, original_input=ORIGINAL)
        assert contract.projection()["semantics"]["method_selection"] == METHOD_SELECTION
    schema = provider_schema(
        Clarification, StageContract.bind("clarification", {}, cfg).projection()
    )
    assert schema["properties"]["assumptions"]["description"] == METHOD_SELECTION["assumptions"]
    assert METHOD_SELECTION["requirements"] in schema["properties"]["clarified_goal"]["description"]
    plan = provider_schema(Plan, StageContract.bind("planning", {}, cfg).projection())
    assert METHOD_SELECTION["planning"] in plan["properties"]["tasks"]["description"]


def test_direct_route_can_satisfy_alternative_goal_without_handoff_files(tmp_path):
    class DirectModel(AlternativesModel):
        def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kw):
            body = json.loads(prompt)
            if schema is WorkerResult:
                self.prompts.append(body)
                (workspace / "processed.txt").write_text("finished")
                return WorkerResult(status="completed", summary="Local result created."), Usage(
                    tokens=1
                )
            if schema is Verification and "proposal" not in body:
                self.prompts.append(body)
                passed = (workspace / "processed.txt").read_text() == "finished"
                return Verification(
                    findings=tuple(
                        Finding(
                            criterion_id=c["id"],
                            passed=passed,
                            evidence="Inspected actual direct result.",
                        )
                        for c in body["criteria"]
                    ),
                    summary="Direct route satisfied.",
                ), Usage(tokens=1)
            return super().generate(
                policy, prompt, schema, workspace, authority, timeout, cancelled, **kw
            )

    model = DirectModel(recover=True)
    engine, source = runtime(tmp_path, model)
    run = engine.start(ORIGINAL, configuration(), source)
    assert projection(engine.journal, run)["status"] == "completed"
    assert not model.recovered
    destination = tmp_path / "published"
    engine.promote(run, destination)
    assert (destination / "processed.txt").read_text() == "finished"
    assert not (destination / "program.py").exists()
