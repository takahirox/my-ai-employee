"""Initial snapshot evidence through actual execution, review, replay and publication."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from ai_employee.cli import projection
from ai_employee.engine import Engine
from ai_employee.history import Journal
from ai_employee.models import (
    Clarification,
    Criterion,
    Finding,
    Plan,
    Usage,
    Verification,
    WorkerResult,
)
from ai_employee.native import provider_schema
from ai_employee.semantics import INPUT_PRESERVATION
from ai_employee.stage_contracts import OutputViolation, StageContract

from .test_autonomous_runtime import OfflineModel, config, runtime


class PreservationModel(OfflineModel):
    def __init__(self, mutation: str = "none", *, lie: bool = False, mode="exact"):
        super().__init__()
        self.mutation, self.lie = mutation, lie
        self.mode = mode
        self.contexts: list[dict[str, Any]] = []

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs):
        body = json.loads(prompt)
        if schema is Verification:
            self.calls.append("Verification")
            self.verifications += 1
            source = body.get("original", body)
            self.contexts.append(source)
            evidence = source["input_comparison"]
            # No Worker-created baseline, Git repository, or extra model call is needed.
            assert evidence["scope"]["paths"] == {"result": ["documents"]}
            passed = self.lie or evidence["results"]["result"]["matches"]
            return Verification(
                findings=tuple(
                    Finding(criterion_id=c["id"], passed=passed, evidence="runtime comparison")
                    for c in ([{"id": "review"}] if "proposal" in body else body["criteria"])
                ),
                summary="compared exact initial and Candidate snapshots",
            ), Usage(tokens=1, cost=0)
        result, usage = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs
        )
        if schema in {Clarification, Plan}:
            data = result.model_dump(mode="json")
            criteria = data["criteria"] if schema is Clarification else data["tasks"][0]["criteria"]
            criteria[0]["preserved_paths"] = ["documents"]
            criteria[0]["preservation_mode"] = self.mode
            return schema.model_validate(data), usage
        if schema is WorkerResult:
            p = workspace / "documents/record.txt"
            if self.mutation == "change":
                p.write_text("changed")
            elif self.mutation == "delete":
                p.unlink()
            elif self.mutation == "add":
                (p.parent / "new.txt").write_text("new")
            elif self.mutation == "mode":
                p.chmod(0o700)
        return result, usage


def prepared(tmp_path, model, *, review=False):
    engine, source = runtime(tmp_path, model)
    (source / "documents").mkdir()
    (source / "documents/record.txt").write_text("original bytes")
    cfg = config(task_attempts=1, replans=0)
    if review:
        cfg = cfg.model_copy(
            update={"verification": cfg.verification.model_copy(update={"review": "always"})}
        )
    run = engine.prepare("Write result; preserve documents unchanged", cfg, source)
    return engine, run, cfg


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("mode", ["exact", "existing"])
def test_initial_comparison_reaches_verifier_review_and_durable_replay(tmp_path, review, mode):
    model = PreservationModel(mode=mode)
    engine, run, _ = prepared(tmp_path, model, review=review)
    engine.execute(run)
    view = projection(engine.journal, run)
    assert view["status"] == "completed"
    assert model.workers == 1
    assert len(model.calls) == (7 if review else 5)
    assert not any(e["kind"] in {"output_rejected", "attempt_failed"} for e in view["events"])
    records = [e["body"] for e in view["events"] if e["kind"] == "verification"]
    assert len(records) == 2
    for record in records:
        comparison = record["input_comparison"]
        assert comparison["scope"]["run"] == run
        assert comparison["results"]["result"]["matches"]
        assert comparison["results"]["result"]["compared_paths"] == 1
    if review:
        assert model.contexts[0]["input_comparison"] == model.contexts[1]["input_comparison"]
        assert model.contexts[2]["input_comparison"] == model.contexts[3]["input_comparison"]
    before = list(model.calls)
    engine = Engine(Journal(engine.journal.path), engine.candidates, model, engine.root)
    engine.execute(run)
    engine.promote(run, tmp_path / "published")
    assert model.calls == before
    assert (tmp_path / "published/documents/record.txt").read_text() == "original bytes"
    assert (tmp_path / "published/result.txt").read_text() == "correct"


@pytest.mark.parametrize("mode", ["exact", "existing"])
@pytest.mark.parametrize("mutation", ["change", "delete", "mode"])
def test_mutated_inputs_never_accepted_even_when_verifier_says_pass(tmp_path, mutation, mode):
    model = PreservationModel(mutation, lie=True, mode=mode)
    engine, run, _ = prepared(tmp_path, model)
    with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
        engine.execute(run)
    records = [e["body"] for e in engine.journal.events(run) if e["kind"] == "verification"]
    assert len(records) == 1
    assert records[0]["result"]["findings"][0]["passed"]
    assert not records[0]["passed"]
    result = records[0]["input_comparison"]["results"]["result"]
    assert result["changed_count"] == 1
    assert not result["matches"]
    with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
        engine.promote(run, tmp_path / "published")


@pytest.mark.parametrize(
    "field", ["run", "initial_tree", "candidate", "goal", "task", "policy", "paths", "missing"]
)
@pytest.mark.parametrize("stage", ["task_verification", "verification_review"])
def test_contract_rejects_missing_or_foreign_comparison_before_model(tmp_path, field, stage):
    model = PreservationModel()
    engine, run, cfg = prepared(tmp_path, model)
    engine.execute(run)
    context = copy.deepcopy(model.contexts[0])
    if field == "missing":
        del context["input_comparison"]
    else:
        context["input_comparison"]["scope"][field] = "foreign"
    prompt = {"original": context} if stage.endswith("review") else context
    with pytest.raises(ValueError, match="INPUT_COMPARISON_CONTEXT_MISMATCH"):
        StageContract.bind(stage, prompt, cfg)


@pytest.mark.parametrize("mutation", ["missing", "other_run", "other_candidate"])
def test_publication_recomputes_saved_evidence_instead_of_trusting_references(
    tmp_path, monkeypatch, mutation
):
    model = PreservationModel()
    engine, run, _ = prepared(tmp_path, model)
    engine.execute(run)
    events = copy.deepcopy(engine.journal.events(run))
    record = next(e["body"] for e in events if e["kind"] == "verification")
    if mutation == "missing":
        del record["input_comparison"]
    else:
        field = "run" if mutation == "other_run" else "candidate"
        record["input_comparison"]["scope"][field] = "foreign"
    # Deliberately bypass the journal's chain here to exercise the consumer boundary too.
    monkeypatch.setattr(engine.journal, "events", lambda _: events)
    with pytest.raises(ValueError, match="INPUT_COMPARISON_CONTEXT_MISMATCH"):
        engine._completion(run)


@pytest.mark.parametrize(
    "path", ["/input", "../input", "a/../input", "./input", "a//b", "input/", "input/*", "a\\b"]
)
def test_comparison_scope_rejects_ambiguous_or_escaping_paths(path):
    with pytest.raises(ValueError, match="INVALID_PRESERVATION_PATH"):
        Criterion(id="preserve", description="keep", preserved_paths=(path,))


def test_generated_contract_and_plan_repair_share_preservation_rule(tmp_path):
    model = PreservationModel()
    engine, run, cfg = prepared(tmp_path, model)
    engine.execute(run)
    context = model.contexts[0]
    prompt = {"goal": context["goal"]}
    contract = StageContract.bind("planning", prompt, cfg)
    schema = provider_schema(Plan, contract.projection())
    assert (
        schema["$defs"]["Criterion"]["properties"]["preserved_paths"]["description"]
        == INPUT_PRESERVATION
    )
    assert contract.projection()["input_preservation"] == INPUT_PRESERVATION
    plan = projection(engine.journal, run)["plan"]
    plan["tasks"][0]["criteria"][0]["preserved_paths"] = []
    for stage in ("planning", "recovery"):
        bound = StageContract.bind(stage, prompt, cfg)
        with pytest.raises(OutputViolation, match="INPUT_PRESERVATION_WEAKENED"):
            bound.validate(Plan.model_validate(plan), prompt, cfg)


def test_absent_initial_scope_and_truncated_changes_never_prove_equality(tmp_path):
    model = PreservationModel()
    engine, source = runtime(tmp_path, model)
    original = engine.candidates.capture(source)
    assert not engine.candidates.compare_inputs(original, original, ("missing",))["matches"]
    for i in range(70):
        (source / f"added-{i}.txt").write_text("new")
    changed = engine.candidates.capture(source)
    result = engine.candidates.compare_inputs(original, changed, (".",))
    assert not result["matches"]
    assert result["changed_count"] == 70
    assert len(result["changes"]) == 64
    assert result["truncated"]


@pytest.mark.parametrize("mode", ["exact", "existing"])
def test_interrupted_acceptance_reuses_verified_comparison_without_repeating_worker(
    tmp_path, monkeypatch, mode
):
    model = PreservationModel(mode=mode)
    engine, run, _ = prepared(tmp_path, model)
    append = engine.journal.append

    class ControllerCrash(BaseException):
        pass

    def interrupt(run_id, kind, **body):
        if kind == "accepted":
            raise ControllerCrash()
        return append(run_id, kind, **body)

    with monkeypatch.context() as patch:
        patch.setattr(engine.journal, "append", interrupt)
        with pytest.raises(ControllerCrash):
            engine.execute(run)
    assert model.workers == 1
    assert model.verifications == 1
    engine.execute(run)
    assert projection(engine.journal, run)["status"] == "completed"
    assert model.workers == 1
    assert model.verifications == 2
    assert any(e["kind"] == "candidate_reused" for e in engine.journal.events(run))
    engine.promote(run, tmp_path / "published")


def test_corrupted_initial_blob_cannot_be_used_for_replayed_comparison(tmp_path):
    model = PreservationModel()
    engine, run, _ = prepared(tmp_path, model)
    engine.execute(run)
    tree = model.contexts[0]["initial_tree"]
    manifest = engine.candidates.manifest(tree)
    blob = engine.candidates.root / tree / manifest["documents/record.txt"]["blob"]
    blob.chmod(0o600)
    blob.write_text("corrupted runtime object")
    with pytest.raises(ValueError, match="CANDIDATE_BYTES_CHANGED"):
        engine.promote(run, tmp_path / "published")


@pytest.mark.parametrize("mode", ["exact", "existing"])
def test_additions_follow_explicit_mode_at_acceptance_and_publication(tmp_path, mode):
    model = PreservationModel("add", lie=True, mode=mode)
    engine, run, _ = prepared(tmp_path, model, review=True)
    if mode == "exact":
        with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
            engine.execute(run)
        with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
            engine.promote(run, tmp_path / "published")
    else:
        engine.execute(run)
        engine.execute(run)
        engine.promote(run, tmp_path / "published")
        assert (tmp_path / "published/documents/new.txt").read_text() == "new"
        assert all(
            c["input_comparison"]["scope"]["modes"] == {"result": "existing"}
            for c in model.contexts
        )


@pytest.mark.parametrize("mode", ["exact", "existing"])
@pytest.mark.parametrize(
    "mutation", ["add", "change", "delete", "move", "mode", "link", "type", "file_to_link"]
)
def test_directory_selection_protects_all_96_initial_entries(tmp_path, mode, mutation):
    engine, source = runtime(tmp_path, PreservationModel())
    factories, support = source / "spec/factories", source / "spec/support"
    factories.mkdir(parents=True)
    support.mkdir()
    for i in range(88):
        (factories / f"factory-{i:02}.rb").write_text("factory")
    for i in range(7):
        (support / f"helper-{i}.rb").write_text("helper")
    link = support / "helper-link.rb"
    link.symlink_to("helper-0.rb")
    initial = engine.candidates.capture(source)
    target = factories / "factory-87.rb"
    if mutation == "add":
        (support / "new.rb").write_text("new")
    elif mutation == "change":
        target.write_text("changed")
    elif mutation == "delete":
        target.unlink()
    elif mutation == "move":
        target.rename(support / "moved.rb")
    elif mutation == "mode":
        target.chmod(0o755)
    elif mutation == "link":
        link.unlink()
        link.symlink_to("helper-1.rb")
    elif mutation == "file_to_link":
        target.unlink()
        target.symlink_to("factory-00.rb")
    else:
        link.unlink()
        link.write_text("helper")
    candidate = engine.candidates.capture(source)
    result = engine.candidates.compare_inputs(
        initial, candidate, ("spec/factories", "spec/support"), mode
    )
    assert result["matches"] == (mode == "existing" and mutation == "add")
    assert result["compared_paths"] >= 96
    assert not engine.candidates.compare_inputs(initial, candidate, ("absent",), mode)["matches"]


def test_exact_serialization_remains_compatible_and_existing_is_explicit():
    legacy = {
        "id": "keep",
        "description": "preserve",
        "preserved_paths": ["."],
        "checks": [],
        "outcome": "artifact",
    }
    exact = Criterion.model_validate(legacy)
    assert exact.preservation_mode == "exact"
    assert exact.model_dump(mode="json") == legacy
    assert exact.canonical() == json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    existing = Criterion.model_validate({**legacy, "preservation_mode": "existing"})
    assert existing.digest != exact.digest
    assert Criterion.model_validate_json(existing.model_dump_json()) == existing


@pytest.mark.parametrize("stage", ["planning", "recovery"])
@pytest.mark.parametrize("mode", ["exact", "existing"])
def test_planning_and_recovery_retain_preservation_mode(tmp_path, stage, mode):
    model = PreservationModel(mode=mode)
    engine, run, cfg = prepared(tmp_path, model)
    engine.execute(run)
    prompt = {"goal": model.contexts[0]["goal"]}
    contract = StageContract.bind(stage, prompt, cfg)
    plan = projection(engine.journal, run)["plan"]
    contract.validate(Plan.model_validate(plan), prompt, cfg)
    plan["tasks"][0]["criteria"][0]["preservation_mode"] = (
        "existing" if mode == "exact" else "exact"
    )
    with pytest.raises(OutputViolation, match="INPUT_PRESERVATION_WEAKENED"):
        contract.validate(Plan.model_validate(plan), prompt, cfg)


def test_comparison_mode_cannot_be_rebound_or_removed(tmp_path, monkeypatch):
    model = PreservationModel(mode="existing")
    engine, run, cfg = prepared(tmp_path, model)
    engine.execute(run)
    source = copy.deepcopy(model.contexts[0])
    del source["input_comparison"]["scope"]["modes"]
    with pytest.raises(ValueError, match="INPUT_COMPARISON_CONTEXT_MISMATCH"):
        StageContract.bind("task_verification", source, cfg)
    events = copy.deepcopy(engine.journal.events(run))
    record = next(e["body"] for e in events if e["kind"] == "verification")
    record["input_comparison"]["scope"]["modes"]["result"] = "exact"
    monkeypatch.setattr(engine.journal, "events", lambda _: events)
    with pytest.raises(ValueError, match="INPUT_COMPARISON_CONTEXT_MISMATCH"):
        engine.promote(run, tmp_path / "published")


@pytest.mark.parametrize("schema", [Clarification, Plan])
def test_mode_is_explicit_in_provider_contract(schema):
    properties = provider_schema(schema)["$defs"]["Criterion"]["properties"]
    assert properties["preservation_mode"]["enum"] == ["exact", "existing"]
    assert properties["preservation_mode"]["description"] == INPUT_PRESERVATION
    assert "existing" in INPUT_PRESERVATION
