"""Original-source references through schema, review, acceptance and durable replay."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection
from ai_employee.engine import Engine
from ai_employee.history import Journal, Stopped
from ai_employee.models import (
    Clarification,
    ClarificationNeed,
    Criterion,
    Finding,
    Goal,
    Requirement,
    StagePolicy,
    Usage,
    Verification,
)
from ai_employee.native import decode_response, provider_schema
from ai_employee.source_refs import (
    MAX_SOURCE_FRAGMENTS,
    SOURCE_REFERENCE_RULE,
    original_source,
    resolve_source_refs,
    source_fragments,
)
from ai_employee.stage_contracts import VERSION, OutputViolation, StageContract, repair_feedback

from .test_autonomous_runtime import clarification, config, runtime
from .test_autonomous_stage_policy import ReviewModel
from .test_stage_contracts import stream


@pytest.mark.parametrize(
    "original",
    [
        "Write\nresult",
        "\r\nWrite\r\nresult\r\n",
        "a\ra\r",
        "値😀e\u0301\u2028é\n値😀e\u0301\n",
        "Write result; preserve inputs. " * 500,
        "\n" * 19999 + "x",
    ],
)
def test_fragments_preserve_every_character_with_bounded_reference_set(original):
    fragments = source_fragments(original)
    assert "".join(fragments.values()) == original
    assert 0 < len(fragments) <= MAX_SOURCE_FRAGMENTS
    assert original_source(original) == original_source(original)
    assert resolve_source_refs(original, tuple(fragments)) == tuple(fragments.values())


def test_repeated_and_disjoint_source_locations_remain_distinct():
    original = "Same.\nDifferent.\nSame.\n"
    assert resolve_source_refs(original, ("s1", "s3")) == ("Same.\n", "Same.\n")
    assert source_fragments(original)["s2"] == "Different.\n"
    assert original_source(original)["digest"] != original_source(original.rstrip())["digest"]


def test_whitespace_alone_cannot_be_a_requirement_source():
    with pytest.raises(ValueError, match="INVALID_ORIGINAL_REFERENCES"):
        resolve_source_refs("\nWrite result", ("s1",))
    assert resolve_source_refs("\nWrite result", ("s1", "s2")) == ("\n", "Write result")


def proposal(refs, *, need=False):
    value = clarification()
    if need:
        return value.model_copy(
            update={
                "unresolved": (
                    ClarificationNeed(
                        kind="human_input",
                        question="Which result?",
                        reason="A decision is required",
                        evidence="Inspected the request",
                        original_refs=refs,
                    ),
                )
            }
        )
    return value.model_copy(
        update={"requirements": (Requirement(original_refs=refs, criteria=("result",)),)}
    )


@pytest.mark.parametrize("need", [False, True])
@pytest.mark.parametrize("refs", [("s3",), ("s0",), ("s1", "s1"), ("s2", "s1")])
def test_both_reference_fields_reject_foreign_duplicate_and_reordered_ids(need, refs):
    original = "Write\nresult"
    context = {"original_input": original}
    binding = StageContract.bind("clarification", context, config())
    with pytest.raises(OutputViolation, match="INVALID_ORIGINAL_REFERENCES") as caught:
        binding.validate(proposal(refs, need=need), context, config())
    diagnostic = repair_feedback(str(caught.value), caught.value.details)
    assert diagnostic["rule"] == SOURCE_REFERENCE_RULE
    assert diagnostic["path"] == ("unresolved" if need else "requirements") + ".original_refs"
    assert original not in json.dumps(diagnostic)
    if not need:
        with pytest.raises(ValidationError, match="INVALID_ORIGINAL_REFERENCES"):
            Goal(original_input=original, specification=proposal(refs))


@pytest.mark.parametrize("need", [False, True])
def test_empty_and_legacy_quotation_outputs_cannot_use_current_contract(need):
    with pytest.raises(ValidationError):
        proposal((), need=need)
    value = proposal(("s1",), need=need).model_dump(mode="json")
    item = value["unresolved" if need else "requirements"][0]
    item.pop("original_refs")
    item["original_fragment"] = "Write result"
    with pytest.raises(ValidationError):
        Clarification.model_validate(value)


def test_native_schema_and_review_derive_from_same_bound_source():
    original = "Write\r\nresult.\nKeep inputs."
    value = proposal(("s1", "s2"))
    context = {"original_input": original}
    producer = StageContract.bind("clarification", context, config())
    reviewer = StageContract.bind(
        "clarification_review", {"original": context, "proposal": value.model_dump()}, config()
    )
    assert producer.projection()["original_source"] == reviewer.projection()["original_source"]
    schema = provider_schema(Clarification, producer.projection())
    decoded, _ = decode_response(stream(value.model_dump(mode="json")), Clarification)
    assert producer.validate(decoded, context, config()) == value
    for name in ("Requirement", "ClarificationNeed"):
        fields = schema["$defs"][name]["properties"]
        assert "original_fragment" not in fields
        assert fields["original_refs"]["items"]["enum"] == ["s1", "s2", "s3"]
        assert fields["original_refs"]["maxItems"] == 3
        assert fields["original_refs"]["description"] == SOURCE_REFERENCE_RULE
    evidence = value.source_evidence(original)
    assert evidence["requirements"][0]["fragments"] == ["Write\r\n", "result.\n"]
    assert (
        Goal.model_validate_json(
            Goal(original_input=original, specification=value).model_dump_json()
        ).specification.source_evidence(original)
        == evidence
    )
    # IDs are local selectors, never a claim about the model's source of those characters.
    other = StageContract.bind("clarification", {"original_input": "Other"}, config())
    assert other.identity != producer.identity
    assert other.projection()["original_source"]["fragments"]["s1"] == "Other"


def test_reference_validity_does_not_attest_semantic_interpretation():
    value = clarification().model_copy(
        update={"criteria": (Criterion(id="result", description="An unrelated result"),)}
    )
    original = "Write result"
    context = {"original_input": original}
    StageContract.bind("clarification", context, config()).validate(value, context, config())
    assert Goal(original_input=original, specification=value).specification == value
    assert "not that it supports" in SOURCE_REFERENCE_RULE


class RevisingReferences(ReviewModel):
    def __init__(self, *, invalid_first=False, wait=False, crash=False):
        super().__init__()
        self.invalid_first, self.wait, self.crash = invalid_first, wait, crash
        self.proposals, self.reviews, self.bindings = [], [], []

    def generate(self, policy, prompt, schema, workspace, *args, **kwargs):
        body = json.loads(prompt)
        if schema is Clarification:
            if self.crash and self.proposals:
                raise SystemExit("controller lost during reference repair")
            self.bindings.append(body["stage_contract"])
            value = proposal(("s1", "s2"), need=False)
            if self.wait and not body["clarification_answers"]:
                value = value.model_copy(
                    update={"unresolved": proposal(("s2",), need=True).unresolved}
                )
            if self.invalid_first and not self.proposals:
                value = proposal(("s99",))
            if self.invalid_first and self.proposals:
                assert body["contract_feedback"]["code"] == "INVALID_ORIGINAL_REFERENCES"
            self.proposals.append(value)
            return value, Usage(tokens=0, cost=0)
        if schema is Verification and body["stage_contract"]["stage"] == "clarification_review":
            self.reviews.append(body)
            # Real independent review receives quotations derived by the runtime.
            assert body["source_evidence"]["requirements"][0]["fragments"] == [
                "Write\r\n",
                "result\n",
            ]
            return Verification(
                findings=(
                    Finding(
                        criterion_id="review",
                        passed=len(self.reviews) > 1 or self.wait or self.invalid_first,
                        evidence="Reviewed the bound source",
                    ),
                ),
                summary="Review the meaning, retaining the same source references",
            ), Usage(tokens=0, cost=0)
        return super().generate(policy, prompt, schema, workspace, *args, **kwargs)


def reviewed_config():
    return config().model_copy(update={"clarification": StagePolicy(model="test", review="always")})


@pytest.mark.parametrize("invalid_first", [False, True])
def test_multiline_reference_revision_reaches_worker_within_default_budget(tmp_path, invalid_first):
    model = RevisingReferences(invalid_first=invalid_first)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write\r\nresult\n", reviewed_config(), source)
    assert len(model.proposals) == 2 and model.workers == 1
    assert model.bindings[0] == model.bindings[1]
    assert len(model.reviews) == (1 if invalid_first else 2)
    events = engine.journal.events(run)
    rejected = [e["body"]["reason"] for e in events if e["kind"] == "output_rejected"]
    assert rejected == (["INVALID_ORIGINAL_REFERENCES"] if invalid_first else [])
    goal = next(e["body"]["goal"] for e in events if e["kind"] == "goal")
    assert "original_fragment" not in json.dumps(goal)
    view = projection(engine.journal, run)
    assert view["source_evidence"]["requirements"][0]["fragments"] == ["Write\r\n", "result\n"]
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    reopened.execute(run)
    reopened.promote(run, tmp_path / "published")
    assert (tmp_path / "published/result.txt").read_text() == "correct"
    assert model.workers == 1 and len(model.proposals) == 2


def test_human_need_uses_same_source_before_and_after_durable_wait(tmp_path):
    model = RevisingReferences(wait=True)
    engine, source = runtime(tmp_path, model)
    run = engine.start("Write\r\nresult\n", reviewed_config(), source)
    view = projection(engine.journal, run)
    assert view["status"] == "waiting_for_clarification"
    assert view["source_evidence"]["unresolved"][0]["fragments"] == ["result\n"]
    source_binding = view["original_source"]
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    reopened.answer(run, "The requested result")
    reopened.execute(run)
    assert projection(reopened.journal, run)["original_source"] == source_binding
    assert model.workers == 1


def test_reference_repair_crash_does_not_reset_durable_budget(tmp_path):
    model = RevisingReferences(invalid_first=True, crash=True)
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Write\r\nresult\n", reviewed_config(), source)
    with pytest.raises(SystemExit):
        engine.execute(run)
    model.crash = False
    reopened = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    with pytest.raises(Stopped, match="STAGE_INVOCATION_LIMIT"):
        reopened.execute(run)
    assert len(model.proposals) == 1 and model.workers == 0


def test_goal_from_another_original_cannot_be_replayed(tmp_path):
    model = ReviewModel()
    engine, source = runtime(tmp_path, model)
    run = engine.prepare("Different request", config(), source)
    foreign = Goal(original_input="Write result", specification=clarification())
    engine.journal.append(run, "goal", goal=foreign.model_dump(mode="json"))
    with pytest.raises(ValueError, match="GOAL_PROVENANCE_CHANGED"):
        engine.execute(run)
    assert not model.calls


def test_old_quotation_history_is_readable_but_cannot_resume(tmp_path, monkeypatch):
    import ai_employee.history as history

    model = ReviewModel()
    engine, source = runtime(tmp_path, model)
    monkeypatch.setattr(history, "VERSION", "stage-contract-4")
    run = engine.prepare("Write result", config(), source)
    legacy = clarification().model_dump(mode="json")
    legacy["requirements"] = [{"original_fragment": "Write result", "criteria": ["result"]}]
    engine.journal.append(
        run,
        "goal",
        goal={"original_input": "Write result", "specification": legacy, "mandatory_checks": []},
    )
    monkeypatch.setattr(history, "VERSION", VERSION)
    view = projection(engine.journal, run)
    assert view["goal"]["specification"] == legacy
    assert view["source_evidence"] is None
    with pytest.raises(Stopped, match="CONTRACT_VERSION_CHANGED"):
        engine.execute(run)
    assert not model.calls
