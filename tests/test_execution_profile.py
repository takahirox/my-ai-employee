from __future__ import annotations

import json
from time import monotonic

import pytest

from ai_employee import cli
from ai_employee.domain import ProjectHarnessV2, RoutingMode
from ai_employee.execution_profile import choose_profile, inspect_profile, observe_profile
from ai_employee.serialization import canonical_digest, project_harness_digest
from ai_employee.storage import SQLiteStore
from tests.test_cli_task_routing import _write_routing_fixture


def test_profile_preserves_required_reviews_and_disabled_harness_digests():
    harness = ProjectHarnessV2()
    payload = harness.model_dump(mode="python")
    review = payload["verification"]["review"]
    review.pop("plan_review")
    review.pop("independent_task_review")
    review.pop("parent_semantic_review")
    payload["worker"].pop("isolated_workspace_tools")
    payload["worker"].pop("scratch_validation")
    payload["worker"].pop("observation_hosts")
    assert project_harness_digest(harness) == canonical_digest(payload)
    strict = ProjectHarnessV2.model_validate_json(
        '{"verification":{"review":{"independent_task_review":true,"parent_semantic_review":true}}}'
    )
    profile = choose_profile("test", strict, "a" * 64, RoutingMode.FIXED, "fixed")
    assert all(
        item.disposition == "required"
        for item in profile.stages
        if item.stage in {"task_review", "parent_review", "verification_and_approval"}
    )
    with pytest.raises(ValueError):
        ProjectHarnessV2.model_validate_json(
            '{"verification":{"review":{"required":false,"plan_review":true}}}'
        )


def test_harness_plan_review_blocks_lightweight_before_model_access(tmp_path, monkeypatch):
    repository, operator, database = _write_routing_fixture(tmp_path)
    path = repository / ".fleet/project.json"
    payload = json.loads(path.read_text())
    payload["verification"] = {"review": {"plan_review": True}}
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_a, **_kw: database)
    monkeypatch.setattr(
        cli, "load_operator_config", lambda *_: pytest.fail("model config accessed")
    )
    with pytest.raises(ValueError, match="Harness requires plan review"):
        cli.main(
            [
                "work",
                "fix a typo",
                "--repo",
                str(repository),
                "--profile",
                "lightweight",
                "--strategy",
                "sol",
                "--operator-config",
                str(operator),
            ]
        )
    assert not database.exists()


def test_profile_persistence_resume_mismatch_and_read_only_projection(tmp_path):
    path = tmp_path / "history.db"
    profile = choose_profile("test", ProjectHarnessV2(), "a" * 64, RoutingMode.FIXED, "fixed")
    with SQLiteStore(path) as store:
        with observe_profile(store, profile, started=monotonic()) as observation:
            observation.record("before_first_worker")
            observation.record("before_first_worker")
        original = inspect_profile(store, "test")
        assert len(original["timings"]) == 3
    with SQLiteStore(path) as store:
        assert inspect_profile(store, "test") == original
        resumed = choose_profile("test", ProjectHarnessV2(), "a" * 64, RoutingMode.FIXED, "fixed")
        with observe_profile(store, resumed, started=monotonic()):
            pass
        current = inspect_profile(store, "test")
        assert current["choice"] == original["choice"]
        changed = choose_profile("test", ProjectHarnessV2(), "a" * 64, RoutingMode.ADAPTIVE, None)
        with (
            pytest.raises(ValueError, match="changed"),
            observe_profile(store, changed, started=monotonic()),
        ):
            pytest.fail("changed path executed")
        assert inspect_profile(store, "test") == current
        from ai_employee.execution_profile import ProfileObservation

        interrupted = ProfileObservation(store, profile, monotonic())
        interrupted.record("invocation_start")
        partial = inspect_profile(store, "test")
        assert partial["active_invocation_wall_seconds"] is None
        assert partial["timing_complete"] is False


@pytest.mark.parametrize("routing_mode", ["fixed", "adaptive"])
@pytest.mark.parametrize("profile", ["lightweight", "adaptive"])
@pytest.mark.parametrize("reverse", [False, True])
def test_profile_and_routing_override_are_mutually_exclusive(routing_mode, profile, reverse):
    options = [("--profile", profile), ("--routing-mode", routing_mode)]
    if reverse:
        options.reverse()
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["work", "fix", *(value for pair in options for value in pair)]
        )


@pytest.mark.parametrize(
    "protocol",
    [
        "fleet-proposed-graph/2",
        "fleet-worker-proposal/2",
        "fleet-isolated-candidate/1",
        "fleet-plan-review/2",
        "fleet-task-result-review/2",
        "fleet-parent-semantic-review/2",
    ],
)
def test_guidance_ablation_never_changes_task_data_or_review_authority(protocol):
    from ai_employee.engineering_guidance import (
        COMMENT_GUIDANCE,
        SIMPLICITY_GUIDANCE,
        SIMPLICITY_REVIEW_GUIDANCE,
        guidance_scope,
    )
    from ai_employee.prompt_transport import prompt_json

    payload = {
        "protocol": protocol,
        "instruction": SIMPLICITY_GUIDANCE + COMMENT_GUIDANCE + "Preserve mandatory checks.",
        "goal": {"statement": SIMPLICITY_GUIDANCE},
        "rubric": {"rules": (SIMPLICITY_REVIEW_GUIDANCE, "Evidence and freshness are required.")},
    }
    original = prompt_json(payload)
    with guidance_scope(False):
        ablated = json.loads(prompt_json(payload))
    assert prompt_json(payload) == original
    assert SIMPLICITY_GUIDANCE not in ablated["instruction"]
    assert COMMENT_GUIDANCE in ablated["instruction"]
    assert "Preserve mandatory checks." in ablated["instruction"]
    assert ablated["goal"] == payload["goal"]
    assert ablated["rubric"]["rules"] == ["Evidence and freshness are required."]
