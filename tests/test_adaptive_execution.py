from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.adaptive_execution import (
    AdaptiveExecutionDecision,
    choose_adaptive_path,
)
from ai_employee.adaptive_execution import (
    EffectAwareExecutionRecommendation as ExecutionRecommendation,
)
from ai_employee.domain import RoutingMode, SemanticTaskProfile
from ai_employee.execution_profile import inspect_profile
from ai_employee.routing import assess_task, merge_semantic_profile
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeRouteRecord,
    NodeSemanticAssessmentRecord,
)
from ai_employee.task_planning import CliProposedGraphPlanner, ProposedGraph
from ai_employee.task_review import TaskReviewDecision
from tests.test_assessment_reuse import PROFILE, assessor, deterministic
from tests.test_cli_graph_e2e import _fixture

DIRECT = {
    "path": "direct",
    "effect_scope": "read_only_or_local_reversible",
    "scope_clear": True,
    "criteria_clear": True,
    "coordinated_work_required": False,
    "planning_requested": False,
    "reason": "One worker can inspect, edit and verify the accepted local change.",
}


@pytest.mark.parametrize(
    "profile_change,advice_change,gate_change,expected",
    [
        ({}, {}, {}, "direct"),
        ({"task_type": "implementation", "reasoning_class": "moderate"}, {}, {}, "direct"),
        ({"task_type": "architecture"}, {}, {}, "planned"),
        ({"scope": "multi_component"}, {}, {}, "planned"),
        ({"ambiguity": "medium"}, {}, {}, "planned"),
        ({"reasoning_class": "deep"}, {}, {}, "planned"),
        ({}, {"scope_clear": False}, {}, "planned"),
        ({}, {"criteria_clear": False}, {}, "planned"),
        ({}, {"coordinated_work_required": True}, {}, "planned"),
        ({}, {"planning_requested": True}, {}, "planned"),
        ({}, {"path": "unknown"}, {}, "planned"),
        ({}, {"effect_scope": "external_or_protected_change"}, {}, "planned"),
        ({}, {"effect_scope": "unknown"}, {}, "planned"),
        ({}, {}, {"plan_review_required": True}, "planned"),
        ({}, {}, {"planning_requested": True}, "planned"),
        ({}, {}, {"has_completion_criteria": False}, "planned"),
    ],
)
def test_route_boundaries_preserve_risk_and_authority(
    profile_change, advice_change, gate_change, expected
):
    profile = SemanticTaskProfile.model_validate_json(
        json.dumps(json.loads(PROFILE) | profile_change)
    )
    # Length and risk are not proxies for whether optional planning helps.
    assessment = merge_semantic_profile(
        assess_task(
            "Inspect; implement; verify " * 100,
            run_id="run",
            risk=9,
            required_capabilities=("process",),
        ),
        profile,
    )
    before = assessment.model_dump_json()
    path, _ = choose_adaptive_path(
        assessment,
        ExecutionRecommendation(**(DIRECT | advice_change)),
        **(
            {
                "plan_review_required": False,
                "planning_requested": False,
                "has_completion_criteria": True,
            }
            | gate_change
        ),
    )
    assert path == expected
    assert assessment.model_dump_json() == before
    assert assessment.risk == 9 and assessment.required_capabilities == ("process",)


@pytest.mark.parametrize(
    "advice",
    [
        None,
        {},
        {**DIRECT, "path": "lightweight"},
        {**DIRECT, "scope_clear": "true"},
        {**DIRECT, "grant_permission": True},
    ],
)
def test_missing_or_invalid_advice_preserves_valid_semantics_and_plans(advice):
    adapter, execution, _ = assessor()
    adapter.execution_context = {"completion_criteria": [{"id": "check"}]}
    execution.response = json.dumps(
        json.loads(PROFILE) | {"execution_recommendation": advice}
    ).encode()
    profile = adapter.assess("Sort values", deterministic())
    assert profile == SemanticTaskProfile.model_validate_json(PROFILE)
    assert adapter.execution_recommendation is None
    assert (
        choose_adaptive_path(
            merge_semantic_profile(deterministic(), profile),
            adapter.execution_recommendation,
            plan_review_required=False,
            planning_requested=False,
            has_completion_criteria=True,
        )[0]
        == "planned"
    )


def test_advice_cache_is_bound_to_accepted_criteria_and_semantics_stay_strict():
    adapter, execution, _ = assessor()
    adapter.execution_context = {"completion_criteria": [{"id": "check"}]}
    execution.response = json.dumps(
        json.loads(PROFILE) | {"execution_recommendation": DIRECT}
    ).encode()
    adapter.assess("Sort values", deterministic())
    adapter.execution_recommendation = None
    adapter.assess("Sort values", deterministic())
    assert execution.calls == 1
    assert adapter.execution_recommendation == ExecutionRecommendation(**DIRECT)
    adapter.execution_context = {"completion_criteria": [{"id": "other-check"}]}
    adapter.assess("Sort values", deterministic())
    assert execution.calls == 2
    execution.response = json.dumps({"execution_recommendation": DIRECT}).encode()
    with pytest.raises(ValueError, match="invalid semantic task assessment"):
        adapter.assess("Other goal", deterministic())


def direct_fixture(tmp_path: Path, effect_scope: str = "read_only_or_local_reversible"):
    repository, operator, database, state = _fixture(tmp_path, task_review=True)
    worker = tmp_path / "fake-worker"
    source = worker.read_text()
    source = source.replace(
        '"reasons": ["independent accepted-node assessment"],',
        '"execution_recommendation": ' + repr(DIRECT | {"effect_scope": effect_scope}) + ",\n"
        '        "reasons": ["independent accepted-node assessment"],',
    )
    start = source.index('if name in {"a", "b"}:')
    end = source.index("patch = (", start)
    source = source[:start] + source[end:]
    worker.write_text(source)
    verifier = tmp_path / "fake-parent-verifier"
    verifier.write_text(verifier.read_text().replace('("a", "b", "c")', '("a",)'))
    return repository, operator, database, state


def test_direct_cli_keeps_adaptive_routing_reviews_evidence_and_resume(
    tmp_path, capsys, monkeypatch
):
    repository, operator, database, state = direct_fixture(tmp_path)
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_args, **_kwargs: database)

    def forbidden_plan(*args, **kwargs):
        pytest.fail("direct execution called the planner")

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", forbidden_plan)
    assert (
        cli.main(
            [
                "work",
                "change a.txt",
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--non-interactive",
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "ready_to_promote"
    assert emitted["execution_path"] == "direct"
    run_id = emitted["run_id"]
    assert (state / "parent.verified").exists()
    # Promotion still needs its original approval.
    assert (repository / "a.txt").read_text() == "a-before\n"
    with SQLiteStore(database) as store:
        decision = store.get(
            "adaptive_execution_decision_v2", "adaptive-path-" + run_id, AdaptiveExecutionDecision
        )
        profile = inspect_profile(store, run_id)
        assert profile["choice"]["profile"] == "adaptive"
        assert profile["choice"]["fixed_strategy_id"] is None
        assert profile["adaptive_execution"]["path"] == "direct"
        assert {
            item["stage"]
            for item in profile["effective_stages"]
            if item["disposition"] == "required"
        } >= {"task_review", "verification_and_approval"}
        assert not store.list_records("proposed_graph_v2", ProposedGraph, run_id=run_id)
        assert not store.list_records(
            "node_semantic_assessment_v2", NodeSemanticAssessmentRecord, run_id=run_id
        )
        routes = store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)
        assert len(routes) == 1
        assert routes[0].selected_strategy.routing_mode is RoutingMode.ADAPTIVE
        assert routes[0].assessment.semantic_profile == decision.assessment.semantic_profile
        assert routes[0].assessment.complexity == decision.assessment.complexity
        assert store.list_records("task_review_decision_v2", TaskReviewDecision)
    # Read-only replay never makes another model call.
    monkeypatch.setattr(
        "ai_employee.worker_adapters.CliTaskAssessmentAdapter.assess", forbidden_plan
    )
    assert cli.main(["replay", run_id]) == 0
    capsys.readouterr()
    with SQLiteStore(database) as store:
        assert (
            store.get(
                "adaptive_execution_decision_v2",
                "adaptive-path-" + run_id,
                AdaptiveExecutionDecision,
            )
            == decision
        )


@pytest.mark.parametrize(
    "override",
    [
        "plan-only",
        "planner-strategy",
        "mandatory-review",
        "consequential-effects",
        "unknown-effects",
    ],
)
def test_planning_gates_override_direct_recommendation(tmp_path, monkeypatch, override):
    effect_scope = {
        "consequential-effects": "external_or_protected_change",
        "unknown-effects": "unknown",
    }.get(override, "read_only_or_local_reversible")
    repository, operator, database, _ = direct_fixture(tmp_path, effect_scope)
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_args, **_kwargs: database)
    argv = ["work", "change a.txt", "--repo", str(repository), "--operator-config", str(operator)]
    if override == "mandatory-review":
        original = cli.discover_project_harness

        def require_plan(path):
            harness = original(path)
            review = harness.verification.review.model_copy(update={"plan_review": True})
            return harness.model_copy(
                update={"verification": harness.verification.model_copy(update={"review": review})}
            )

        monkeypatch.setattr(cli, "discover_project_harness", require_plan)
    elif override in {"plan-only", "planner-strategy"}:
        argv += ["--plan-only"] if override == "plan-only" else ["--planner-strategy", "low"]

    class PlannerReached(Exception):
        pass

    def stop_at_planner(*args, **kwargs):
        raise PlannerReached

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", stop_at_planner)
    with pytest.raises(PlannerReached):
        cli.main(argv)
    with SQLiteStore(database) as store:
        decisions = store.list_records("adaptive_execution_decision_v2", AdaptiveExecutionDecision)
        assert len(decisions) == 1 and decisions[0].path == "planned"


@pytest.mark.parametrize("stale_binding", [None, "goal_digest", "direct_graph_digest"])
def test_paused_direct_run_retains_decision_and_does_not_reclassify(
    tmp_path, capsys, monkeypatch, stale_binding
):
    repository, operator, database, state = direct_fixture(tmp_path)
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_args, **_kwargs: database)
    worker = tmp_path / "fake-worker"
    worker.write_text(
        worker.read_text().replace(
            "patch = (",
            """
(state / "started").write_text("started")
deadline = time.monotonic() + 10
while not (state / "release").exists():
    if time.monotonic() >= deadline:
        raise SystemExit("fixture release timed out")
    time.sleep(0.01)
patch = (""",
        )
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            cli.main,
            [
                "work",
                "change a.txt",
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--non-interactive",
            ],
        )
        deadline = time.monotonic() + 15
        while not (state / "started").exists():
            if future.done():
                pytest.fail(f"worker did not start: {future.result()}")
            if time.monotonic() >= deadline:
                pytest.fail("worker did not start")
            time.sleep(0.01)
        with SQLiteStore(database) as store:
            run_id = store.list_records("graph_run_v2", GraphRunRecord)[0].id
            decision = store.get(
                "adaptive_execution_decision_v2",
                "adaptive-path-" + run_id,
                AdaptiveExecutionDecision,
            )
            store.request_control(run_id, "pause")
        (state / "release").write_text("release")
        assert future.result(timeout=15) == 5
    assert json.loads(capsys.readouterr().out)["status"] == "paused"

    def no_reassessment(*args, **kwargs):
        pytest.fail("resume reclassified or replanned the accepted direct path")

    monkeypatch.setattr(
        "ai_employee.worker_adapters.CliTaskAssessmentAdapter.assess", no_reassessment
    )
    monkeypatch.setattr(CliProposedGraphPlanner, "plan", no_reassessment)
    if stale_binding:
        changed = decision.model_dump(mode="json") | {
            stale_binding: "f" * 64,
            "content_digest": None,
        }
        invalid = AdaptiveExecutionDecision.model_validate_json(json.dumps(changed))
        with SQLiteStore(database) as store:
            store.put("adaptive_execution_decision_v2", invalid, run_id=run_id)
        with pytest.raises(
            ValueError, match=r"stale run bindings|does not match its accepted decision"
        ):
            cli.main(["resume", run_id])
        return
    assert cli.main(["resume", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["execution_path"] == "direct"
    with SQLiteStore(database) as store:
        assert (
            store.get(
                "adaptive_execution_decision_v2",
                "adaptive-path-" + run_id,
                AdaptiveExecutionDecision,
            )
            == decision
        )


def test_direct_failure_preserves_outcome_and_requires_explicit_continuation(
    tmp_path, capsys, monkeypatch
):
    repository, operator, database, _ = direct_fixture(tmp_path)
    monkeypatch.setattr(cli, "resolve_database_path", lambda *_args, **_kwargs: database)
    worker = tmp_path / "fake-worker"
    source = worker.read_text()
    start = source.index('name = Path(prompt["goal"].split()[-1]).stem')
    worker.write_text(
        source[:start]
        + """
print(json.dumps({"schema_version": "2", "proposals": [],
                  "assistant_note": "Investigation found missing cross-component design criteria.",
                  "usage_json": "{}"}))
raise SystemExit(0)
"""
    )

    def no_automatic_planning(*args, **kwargs):
        pytest.fail("direct failure silently started planning")

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", no_automatic_planning)
    result = cli.main(
        [
            "work",
            "change a.txt",
            "--repo",
            str(repository),
            "--operator-config",
            str(operator),
            "--non-interactive",
        ]
    )
    assert result != 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "failed" and emitted["execution_path"] == "direct"
    assert "explicit planned continuation" in emitted["continuation"]
    assert (repository / "a.txt").read_text() == "a-before\n"
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", emitted["run_id"], GraphRunRecord)
        assert run.status == "failed" and run.replan_count == 0
        from ai_employee.domain.v2 import WorkerResult

        results = store.list_records("worker_result_v2", WorkerResult)
        assert results and all(result.stdout_artifact_digest for result in results)


def test_legacy_decision_keeps_exact_serialization_and_digest():
    from ai_employee.adaptive_execution import ExecutionRecommendation as LegacyRecommendation
    from ai_employee.serialization import canonical_json

    payload = (Path(__file__).parent / "fixtures/adaptive-decision-legacy.json").read_text().strip()
    decision = AdaptiveExecutionDecision.model_validate_json(payload)
    assert type(decision.recommendation) is LegacyRecommendation
    assert (
        decision.content_digest
        == "3f9b6622f0de2a5017e561097a5f07582d3de2c1594f5e77a65f540ff2699e40"
    )
    assert canonical_json(decision) == payload
    assert decision.path == "direct"  # Historical accepted routes are not reselected.
    assert (
        choose_adaptive_path(
            decision.assessment,
            decision.recommendation,
            plan_review_required=False,
            planning_requested=False,
            has_completion_criteria=True,
        )[0]
        == "planned"
    )


@pytest.mark.parametrize(
    "goal", ["Approve the transfer", "Grant admin access", "Delete the production dataset"]
)
def test_short_explicit_consequential_work_is_not_direct_even_at_zero_policy_floor(goal):
    assessment = merge_semantic_profile(
        assess_task(goal, run_id="effects", risk=0),
        SemanticTaskProfile.model_validate_json(PROFILE),
    )
    advice = ExecutionRecommendation(**(DIRECT | {"effect_scope": "external_or_protected_change"}))
    assert (
        choose_adaptive_path(
            assessment,
            advice,
            plan_review_required=False,
            planning_requested=False,
            has_completion_criteria=True,
        )[0]
        == "planned"
    )
    assert assessment.risk == 0


def test_missing_effect_advice_is_conservative_and_new_provider_schema_requires_it():
    from ai_employee.worker_adapters import semantic_assessment_schema_json

    adapter, execution, _ = assessor()
    adapter.execution_context = {"completion_criteria": [{"id": "check"}]}
    old_advice = {k: v for k, v in DIRECT.items() if k != "effect_scope"}
    execution.response = json.dumps(
        json.loads(PROFILE) | {"execution_recommendation": old_advice}
    ).encode()
    assert adapter.assess(
        "Sort values", deterministic()
    ) == SemanticTaskProfile.model_validate_json(PROFILE)
    assert adapter.execution_recommendation is None
    schema = json.loads(semantic_assessment_schema_json(recommend_execution_path=True))
    advice_schema = schema["$defs"]["EffectAwareExecutionRecommendation"]
    assert set(advice_schema["required"]) == set(advice_schema["properties"])


@pytest.mark.parametrize("effects", ["external_or_protected_change", "unknown"])
def test_persisted_direct_decision_cannot_contradict_explicit_effect_advice(effects):
    payload = json.loads(
        (Path(__file__).parent / "fixtures/adaptive-decision-legacy.json").read_text()
    )
    payload["recommendation"]["effect_scope"] = effects
    payload["content_digest"] = None
    with pytest.raises(ValueError, match="contradicts its intended-effect assessment"):
        AdaptiveExecutionDecision.model_validate_json(json.dumps(payload))
    payload["path"] = "planned"
    payload["direct_graph_digest"] = None
    assert AdaptiveExecutionDecision.model_validate_json(json.dumps(payload)).path == "planned"
