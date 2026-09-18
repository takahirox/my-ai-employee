"""Selection capacity is distinct from descendant enforcement and diagnostic limits."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_employee.cli import projection
from ai_employee.models import Clarification, Criterion, Plan, WorkerResult

from .test_autonomous_runtime import OfflineModel, config, runtime
from .test_preservation_schema import criterion_payload, projected


@pytest.mark.parametrize("stage", ["clarification", "planning", "recovery"])
@pytest.mark.parametrize("count", [64, 65, 96, 256, 257])
def test_selection_boundary_in_local_and_actual_provider_contract(stage, count):
    _, validator = projected(stage)
    payload = criterion_payload([f"protected/file-{i:03}.txt" for i in range(count)])
    assert (
        validator.schema["$defs"]["Criterion"]["properties"]["preserved_paths"]["maxItems"] == 256
    )
    assert validator.is_valid(payload) == (count <= 256)
    if count <= 256:
        value = Criterion.model_validate(payload)
        assert len(value.preserved_paths) == count
        assert Criterion.model_validate_json(value.model_dump_json()) == value
    else:
        with pytest.raises(ValidationError):
            Criterion.model_validate(payload)


@pytest.mark.parametrize("last", ["../outside", "protected/file-000.txt"])
def test_larger_allowance_preserves_syntax_and_duplicate_rejection(last):
    paths = [f"protected/file-{i:03}.txt" for i in range(256)]
    paths[-1] = last
    with pytest.raises(ValueError, match="INVALID_PRESERVATION_PATH"):
        Criterion.model_validate(criterion_payload(paths))


class ManySelectionsModel(OfflineModel):
    def __init__(self, paths, mode, mutation):
        super().__init__()
        self.paths, self.mode, self.mutation = paths, mode, mutation

    def generate(self, policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs):
        result, usage = super().generate(
            policy, prompt, schema, workspace, authority, timeout, cancelled, **kwargs
        )
        if schema in {Clarification, Plan}:
            payload = result.model_dump(mode="json")
            criteria = (
                payload["criteria"] if schema is Clarification else payload["tasks"][0]["criteria"]
            )
            criteria[0].update(preserved_paths=self.paths, preservation_mode=self.mode)
            return schema.model_validate(payload), usage
        if schema is WorkerResult:
            last = workspace / self.paths[-1]
            if self.mutation == "change":
                last.write_text("modified")
            elif self.mutation == "delete":
                last.unlink()
        # The ordinary fixture verifier passes because result.txt is correct.
        # The runtime must independently reject the changed protected file.
        return result, usage


@pytest.mark.parametrize("mode", ["exact", "existing"])
@pytest.mark.parametrize("mutation", ["none", "change", "delete"])
def test_all_256_selections_enforced_at_acceptance_and_publication(tmp_path, mode, mutation):
    paths = [f"protected/file-{i:03}.txt" for i in range(256)]
    model = ManySelectionsModel(paths, mode, mutation)
    engine, source = runtime(tmp_path, model)
    (source / "protected").mkdir()
    for path in paths:
        (source / path).write_text("original")
    run = engine.prepare(
        "Write result and preserve input files", config(task_attempts=1, replans=0), source
    )
    if mutation == "none":
        engine.execute(run)
        before = list(model.calls)
        engine.execute(run)
        engine.promote(run, tmp_path / "published")
        assert model.calls == before
        assert (tmp_path / "published" / paths[-1]).read_text() == "original"
    else:
        with pytest.raises(ValueError, match="REPLAN_LIMIT_EXHAUSTED"):
            engine.execute(run)
        with pytest.raises(ValueError, match="GOAL_NOT_VERIFIED"):
            engine.promote(run, tmp_path / "published")
    view = projection(engine.journal, run)
    assert view["plan"]["tasks"][0]["criteria"][0]["preserved_paths"] == paths
    record = next(e["body"] for e in view["events"] if e["kind"] == "verification")
    comparison = record["input_comparison"]["results"]["result"]
    assert comparison["compared_paths"] == 256
    assert comparison["matches"] == (mutation == "none")
    if mutation != "none":
        assert comparison["changes"][0]["path"] == paths[-1]
        assert record["result"]["findings"][0]["passed"]
        assert not record["passed"]


def test_diagnostic_excerpt_remains_64_while_counting_all_256_changes(tmp_path):
    engine, source = runtime(tmp_path, OfflineModel())
    paths = tuple(f"file-{i:03}.txt" for i in range(256))
    for path in paths:
        (source / path).write_text("original")
    before = engine.candidates.capture(source)
    for path in paths:
        (source / path).write_text("changed")
    after = engine.candidates.capture(source)
    result = engine.candidates.compare_inputs(before, after, paths)
    assert not result["matches"]
    assert result["changed_count"] == result["compared_paths"] == 256
    assert len(result["changes"]) == 64
    assert result["truncated"]
