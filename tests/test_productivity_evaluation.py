from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_employee.productivity_evaluation import (
    AcceptanceCriterion,
    ArmIdentity,
    ArmKind,
    GenericOSSAdapter,
    HistoryCompatibility,
    ResultBundle,
    SWEBenchAdapter,
    TaskClass,
    TaskIdentity,
    TrialMetrics,
    TrialResult,
    aggregate_trials,
    compare_direct_to_fleet,
    compare_history,
    component_ablation_contribution,
    dump_result_bundle,
    load_result_bundle,
    trial_id,
    validate_paired_comparability,
)
from ai_employee.serialization import canonical_digest

D1, D2 = "1" * 64, "2" * 64


def task(version: str = "v1", baseline: str = "a" * 40) -> TaskIdentity:
    return TaskIdentity(
        benchmark="fixture",
        benchmark_version=version,
        task_id="case-1",
        task_version=version,
        repository="owner/repo",
        baseline_commit=baseline,
        task_class=TaskClass.BUG_FIX,
        acceptance_criteria=(AcceptanceCriterion(id="test", description="pass", authority="test"),),
    )


def arm(kind: ArmKind, name: str, repetition: int = 1, environment: str = D1) -> ArmIdentity:
    return ArmIdentity(
        id=name,
        kind=kind,
        adapter="fixture",
        worker="codex",
        model_provider="openai",
        model_name="model",
        model_version="snapshot",
        tools=("edit_intent", "process"),
        environment_digest=environment,
        fairness_config_digest=D1,
        arm_config_digest=D2,
        seed=7,
        repetition=repetition,
        disabled_components=("review",) if kind is ArmKind.FLEET_ABLATION else (),
    )


def result(
    bound_task: TaskIdentity,
    bound_arm: ArmIdentity,
    accepted: bool = True,
    active: float = 2,
    wall: float = 10,
) -> TrialResult:
    metrics = TrialMetrics(
        human_active_seconds=active,
        human_interventions=1,
        time_to_accepted_seconds=wall if accepted else None,
        wall_seconds=wall,
        input_tokens=100,
        output_tokens=20,
        api_cost=0.5,
        compute_seconds=8,
        compute_cost=0.1,
        retries=1,
        repairs=1,
        replans=0,
        escalations=0,
        recoveries=1,
        decomposed_nodes=2,
        dependency_edges=1,
        maximum_parallelism=2,
        critical_path_seconds=7,
        context_input_tokens=50,
        context_output_tokens=10,
        unnecessary_work_items=0,
        unnecessary_work_seconds=0,
    )
    return TrialResult(
        id=trial_id(bound_task, bound_arm),
        task=bound_task,
        arm=bound_arm,
        authoritative_success=accepted,
        regression_free=accepted,
        acceptance_evidence_digests=(D1,) if accepted else (),
        regression_evidence_digests=(D2,) if accepted else (),
        process_exit_code=0,
        metrics=metrics,
    )


def bundle(version: str, value: TrialResult) -> ResultBundle:
    return ResultBundle(
        id=f"bundle-{version}",
        run_id=f"bundle-{version}",
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
        benchmark="fixture",
        benchmark_version=version,
        results=(value,),
    )


def test_authority_fairness_aggregation_and_ablation() -> None:
    direct = result(task(), arm(ArmKind.DIRECT_AGENT, "direct"))
    fleet = result(task(), arm(ArmKind.FLEET, "fleet"), accepted=False, active=5)
    assert not fleet.accepted and fleet.process_exit_code == 0
    validate_paired_comparability(direct, fleet)
    comparison = compare_direct_to_fleet((direct,), (fleet,))
    assert comparison.task_classes_where_fleet_hurts == (TaskClass.BUG_FIX,)
    assert (
        next(x for x in comparison.fleet_minus_direct if x.metric == "accepted").statistics.mean
        == -1
    )
    with pytest.raises(ValidationError, match="evidence"):
        TrialResult(
            id=trial_id(task(), arm(ArmKind.DIRECT_AGENT, "direct")),
            task=task(),
            arm=arm(ArmKind.DIRECT_AGENT, "direct"),
            authoritative_success=True,
            regression_free=True,
            metrics=direct.metrics,
        )
    with pytest.raises(ValueError, match="baseline"):
        validate_paired_comparability(direct, result(task(baseline="b" * 40), fleet.arm))
    repeated = result(task(), arm(ArmKind.DIRECT_AGENT, "direct", repetition=2), accepted=False)
    aggregate = aggregate_trials((repeated, direct))[0].metric("accepted")
    assert (aggregate.count, aggregate.rate, aggregate.sample_variance) == (2, 0.5, 0.5)
    ablated = result(task(), arm(ArmKind.FLEET_ABLATION, "no-review"), accepted=False)
    contribution = component_ablation_contribution((result(task(), fleet.arm),), (ablated,))
    assert contribution.component == "review"


def test_adapters_bundles_tamper_and_history() -> None:
    generic = GenericOSSAdapter("oss", "1").normalize_case(
        {
            "task_id": "x",
            "task_version": "1",
            "repository": "o/r",
            "baseline_commit": "a" * 40,
            "task_class": "feature",
            "acceptance_criteria": [{"id": "test", "description": "pass", "authority": "upstream"}],
        }
    )
    assert generic.task_class is TaskClass.FEATURE
    swe = SWEBenchAdapter("verified").normalize_case(
        {
            "instance_id": "django__django-1",
            "repo": "django/django",
            "base_commit": "b" * 40,
            "fail_to_pass": ["fixed"],
            "pass_to_pass": ["regression"],
        }
    )
    assert swe.benchmark == "swe-bench" and len(swe.acceptance_criteria) == 2
    old_task, new_task = task("v1"), task("v2")
    old = bundle("v1", result(old_task, arm(ArmKind.FLEET, "fleet")))
    encoded = dump_result_bundle(old)
    assert load_result_bundle(encoded) == old
    with pytest.raises(ValidationError, match="digest"):
        load_result_bundle(encoded.replace(b'"wall_seconds":10.0', b'"wall_seconds":11.0'))
    with pytest.raises(ValueError, match="canonical"):
        load_result_bundle(b" " + encoded)
    new = bundle("v2", result(new_task, arm(ArmKind.FLEET, "fleet"), accepted=False, wall=12))
    compatibility = HistoryCompatibility(
        benchmark="fixture",
        previous_version="v1",
        current_version="v2",
        task_pairs=((canonical_digest(old_task), canonical_digest(new_task)),),
        arm_pairs=(("fleet", "fleet"),),
        baselines_equivalent=True,
        criteria_equivalent=True,
        capabilities_equivalent=True,
    )
    assert "case-1:fleet:accepted" in compare_history(old, new, compatibility).regressions
