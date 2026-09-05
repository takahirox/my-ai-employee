import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.productivity_evaluation import (
    AcceptanceCriterion,
    ArmConfigManifest,
    ArmIdentity,
    ArmKind,
    CheckDisposition,
    CheckOutcome,
    EnvironmentManifest,
    FailureClassification,
    FairnessConfigManifest,
    GenericOSSAdapter,
    HistoryCompatibility,
    PricingManifest,
    RegressionCheck,
    ResourceBudgetManifest,
    ResultBundle,
    StoppingManifest,
    SWEBenchAdapter,
    TaskClass,
    TaskIdentity,
    TerminalOutcome,
    TrialMetrics,
    TrialResult,
    aggregate_trials,
    combine_result_bundles,
    compare_direct_to_fleet,
    compare_history,
    component_ablation_contribution,
    dump_result_bundle,
    load_result_bundle,
    trial_id,
    validate_paired_comparability,
)
from ai_employee.serialization import canonical_digest, canonical_json

D1, D2, D3 = "1" * 64, "2" * 64, "3" * 64


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
        regression_checks=(
            RegressionCheck(id="regression", description="stay passing", authority="test"),
        ),
    )


def environment(machine: str = D1) -> EnvironmentManifest:
    return EnvironmentManifest(
        executable="/usr/bin/fixture-worker",
        executable_version="1.0.0",
        dependency_lock_digest=D2,
        sandbox_mode="workspace-write",
        network_mode="disabled",
        cache_policy="cold",
        machine_digest=machine,
    )


def fairness() -> FairnessConfigManifest:
    return FairnessConfigManifest(
        prompt_digest=D1,
        context_digest=D2,
        model_provider="openai",
        model_name="model",
        model_version="snapshot",
        reasoning_effort="high",
        tools=("edit_intent", "process"),
        budgets=ResourceBudgetManifest(
            wall_seconds=60, input_tokens=10_000, output_tokens=2_000, cost_limit=10
        ),
        stopping=StoppingManifest(
            maximum_attempts=2,
            maximum_retries=1,
            maximum_repairs=1,
            terminal_conditions=("accepted", "checks-failed"),
        ),
        pricing=PricingManifest(
            currency="USD",
            input_per_million=1,
            output_per_million=2,
            subscription_allocation="none; API list price",
        ),
        randomized_order=("direct", "fleet", "no-review"),
    )


def config(kind: ArmKind) -> ArmConfigManifest:
    if kind is ArmKind.DIRECT_AGENT:
        return ArmConfigManifest(planning=False, review=False, repair=False, maximum_parallelism=1)
    if kind is ArmKind.FLEET_ABLATION:
        return ArmConfigManifest(planning=True, review=False, repair=True, maximum_parallelism=2)
    return ArmConfigManifest(planning=True, review=True, repair=True, maximum_parallelism=2)


def arm(kind: ArmKind, name: str, repetition: int = 1, machine: str = D1) -> ArmIdentity:
    environment_manifest = environment(machine)
    fairness_manifest = fairness()
    arm_manifest = config(kind)
    return ArmIdentity(
        id=name,
        kind=kind,
        adapter="fixture",
        worker="codex",
        environment=environment_manifest,
        fairness_config=fairness_manifest,
        arm_config=arm_manifest,
        environment_digest=canonical_digest(environment_manifest),
        fairness_config_digest=canonical_digest(fairness_manifest),
        arm_config_digest=canonical_digest(arm_manifest),
        seed=7,
        repetition=repetition,
        assignment_index=("direct", "fleet", "no-review").index(name),
        disabled_components=("review",) if kind is ArmKind.FLEET_ABLATION else (),
    )


def result(
    bound_task: TaskIdentity,
    bound_arm: ArmIdentity,
    accepted: bool = True,
    active: float = 2,
    wall: float = 10,
    api_cost: float | None = 0.5,
    compute_cost: float | None = 0.1,
) -> TrialResult:
    metrics = TrialMetrics(
        human_active_seconds=active,
        human_interventions=1,
        time_to_accepted_seconds=wall if accepted else None,
        wall_seconds=wall,
        input_tokens=100,
        output_tokens=20,
        api_cost=api_cost,
        compute_seconds=8,
        compute_cost=compute_cost,
        retries=1,
        repairs=1,
        replans=0,
        escalations=0,
        recoveries=1,
        decomposed_nodes=2,
        dependency_edges=1,
        maximum_parallelism=bound_arm.arm_config.maximum_parallelism,
        critical_path_seconds=7,
        context_input_tokens=50,
        context_output_tokens=10,
        unnecessary_work_items=0,
        unnecessary_work_seconds=0,
    )
    disposition = CheckDisposition.PASSED if accepted else CheckDisposition.FAILED
    return TrialResult(
        id=trial_id(bound_task, bound_arm),
        task=bound_task,
        arm=bound_arm,
        acceptance_outcomes=(
            CheckOutcome(
                check_id="test", authority="test", disposition=disposition, evidence_digest=D1
            ),
        ),
        regression_outcomes=(
            CheckOutcome(
                check_id="regression",
                authority="test",
                disposition=disposition,
                evidence_digest=D2,
            ),
        ),
        terminal_outcome=TerminalOutcome.ACCEPTED if accepted else TerminalOutcome.CHECKS_FAILED,
        failure_classification=None if accepted else FailureClassification.ASSERTION,
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


def source_bundle(name: str, value: TrialResult, day: int = 3) -> ResultBundle:
    return ResultBundle(
        id=name,
        run_id=name,
        created_at=datetime(2026, 9, day, tzinfo=UTC),
        benchmark=value.task.benchmark,
        benchmark_version=value.task.benchmark_version,
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
    with pytest.raises(ValidationError, match="exactly cover"):
        TrialResult(
            id=trial_id(task(), arm(ArmKind.DIRECT_AGENT, "direct")),
            task=task(),
            arm=arm(ArmKind.DIRECT_AGENT, "direct"),
            acceptance_outcomes=(),
            regression_outcomes=(),
            terminal_outcome=TerminalOutcome.ACCEPTED,
            process_exit_code=0,
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


def test_combine_result_bundles_is_deterministic_and_comparison_ready() -> None:
    direct_source = source_bundle(
        "source-direct", result(task(), arm(ArmKind.DIRECT_AGENT, "direct")), day=2
    )
    fleet_source = source_bundle(
        "source-fleet",
        result(task(), arm(ArmKind.FLEET, "fleet"), accepted=False),
        day=3,
    )
    combined = combine_result_bundles((direct_source, fleet_source))
    reverse = combine_result_bundles((fleet_source, direct_source))

    assert dump_result_bundle(combined) == dump_result_bundle(reverse)
    assert combined.id == combined.run_id
    assert combined.id.startswith("productivity-combined-")
    assert combined.created_at == fleet_source.created_at
    assert tuple(item.arm.id for item in combined.results) == ("direct", "fleet")
    comparison = compare_direct_to_fleet(
        tuple(item for item in combined.results if item.arm.id == "direct"),
        tuple(item for item in combined.results if item.arm.id == "fleet"),
    )
    assert comparison.pairs == 1
    for retained, original in zip(
        combined.results,
        (direct_source.results[0], fleet_source.results[0]),
        strict=True,
    ):
        assert retained.arm.environment_digest == original.arm.environment_digest
        assert retained.arm.fairness_config_digest == original.arm.fairness_config_digest
        assert retained.arm.arm_config_digest == original.arm.arm_config_digest
        assert retained.acceptance_outcomes == original.acceptance_outcomes
        assert retained.regression_outcomes == original.regression_outcomes

    ablation_source = source_bundle(
        "source-ablation",
        result(task(), arm(ArmKind.FLEET_ABLATION, "no-review"), accepted=False),
    )
    ablation_bundle = combine_result_bundles((fleet_source, ablation_source))
    contribution = component_ablation_contribution(
        tuple(item for item in ablation_bundle.results if item.arm.id == "fleet"),
        tuple(item for item in ablation_bundle.results if item.arm.id == "no-review"),
    )
    assert contribution.component == "review"


def test_combine_result_bundles_fails_closed() -> None:
    direct = source_bundle("source-direct", result(task(), arm(ArmKind.DIRECT_AGENT, "direct")))

    with pytest.raises(ValueError, match="at least two"):
        combine_result_bundles((direct,))
    with pytest.raises(ValueError, match="duplicate trial identity"):
        combine_result_bundles((direct, direct))
    repeated_direct = source_bundle(
        "source-direct-repeated",
        result(task(), arm(ArmKind.DIRECT_AGENT, "direct", repetition=2)),
    )
    with pytest.raises(ValueError, match="duplicate arm identity"):
        combine_result_bundles((direct, repeated_direct))

    foreign_version = bundle("v2", result(task("v2"), arm(ArmKind.FLEET, "fleet")))
    with pytest.raises(ValueError, match="benchmark identity or version"):
        combine_result_bundles((direct, foreign_version))
    foreign_task = source_bundle(
        "foreign-task",
        result(task(baseline="b" * 40), arm(ArmKind.FLEET, "fleet")),
    )
    with pytest.raises(ValueError, match="mismatched task identity"):
        combine_result_bundles((direct, foreign_task))
    different_scope = source_bundle(
        "different-scope",
        result(task(), arm(ArmKind.FLEET, "fleet", repetition=2)),
    )
    with pytest.raises(ValueError, match=r"incompatible.*run scope"):
        combine_result_bundles((direct, different_scope))
    different_controls = source_bundle(
        "different-controls",
        result(task(), arm(ArmKind.FLEET, "fleet", machine=D3)),
    )
    with pytest.raises(ValueError, match="same capability and controls"):
        combine_result_bundles((direct, different_controls))


def test_adapters_bundles_tamper_and_history() -> None:
    generic = GenericOSSAdapter("oss", "1").normalize_case(
        {
            "task_id": "x",
            "task_version": "1",
            "repository": "o/r",
            "baseline_commit": "a" * 40,
            "task_class": "feature",
            "acceptance_criteria": [{"id": "test", "description": "pass", "authority": "upstream"}],
            "regression_checks": [
                {"id": "regression", "description": "stay passing", "authority": "upstream"}
            ],
        }
    )
    assert generic.task_class is TaskClass.FEATURE
    swe = SWEBenchAdapter("verified").normalize_case(
        {
            "instance_id": "django__django-1",
            "repo": "django/django",
            "base_commit": "b" * 40,
            "FAIL_TO_PASS": json.dumps(["fixed"]),
            "PASS_TO_PASS": ["regression"],
            "problem_statement": "projected standard field",
        }
    )
    assert len(swe.acceptance_criteria) == 1 and len(swe.regression_checks) == 1
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


@pytest.mark.parametrize("metric", ("api_cost", "compute_cost"))
@pytest.mark.parametrize(
    ("old_value", "new_value"),
    ((None, 0.5), (None, None), (0.5, None)),
    ids=("old-missing", "both-missing", "current-missing"),
)
def test_compare_history_requires_both_costs_on_both_sides(
    metric: str, old_value: float | None, new_value: float | None
) -> None:
    old_task, new_task = task("v1"), task("v2")
    base_arm = arm(ArmKind.FLEET, "fleet")
    budgets = base_arm.fairness_config.budgets.model_copy(update={"cost_limit": None})
    fairness_config = base_arm.fairness_config.model_copy(update={"budgets": budgets})
    uncapped_arm = base_arm.model_copy(
        update={
            "fairness_config": fairness_config,
            "fairness_config_digest": canonical_digest(fairness_config),
        }
    )
    old_costs = {"api_cost": 0.5, "compute_cost": 0.1}
    new_costs = dict(old_costs)
    old_costs[metric] = old_value
    new_costs[metric] = new_value
    old = bundle("v1", result(old_task, uncapped_arm, **old_costs))
    new = bundle("v2", result(new_task, uncapped_arm, **new_costs))
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

    with pytest.raises(ValueError, match=rf"historical cost evidence is missing for {metric}"):
        compare_history(old, new, compatibility)


def test_outcomes_bind_ids_authorities_dispositions_and_evidence() -> None:
    valid = result(task(), arm(ArmKind.FLEET, "fleet"))
    payload = valid.model_dump(mode="python")
    payload["acceptance_outcomes"] = (
        CheckOutcome(
            check_id="test",
            authority="caller",
            disposition=CheckDisposition.PASSED,
            evidence_digest=D1,
        ),
    )
    with pytest.raises(ValidationError, match="checks and authorities"):
        TrialResult.model_validate(payload)
    payload["acceptance_outcomes"] = ()
    with pytest.raises(ValidationError, match="exactly cover"):
        TrialResult.model_validate(payload)


def test_manifests_bind_digests_and_ablation_changes_exactly_one_component() -> None:
    data = arm(ArmKind.FLEET, "fleet").model_dump(mode="python")
    data["environment_digest"] = D3
    with pytest.raises(ValidationError, match="retained manifest"):
        ArmIdentity.model_validate(data)
    full = result(task(), arm(ArmKind.FLEET, "fleet"))
    ablated_arm = arm(ArmKind.FLEET_ABLATION, "no-review")
    broken_config = ArmConfigManifest(
        planning=False, review=False, repair=True, maximum_parallelism=2
    )
    broken_arm = ablated_arm.model_copy(
        update={
            "arm_config": broken_config,
            "arm_config_digest": canonical_digest(broken_config),
        }
    )
    with pytest.raises(ValueError, match="does not disable"):
        component_ablation_contribution((full,), (result(task(), broken_arm, accepted=False),))


def test_bundle_rejects_noncanonical_order_duplicate_ids_and_unstable_families() -> None:
    direct = result(task(), arm(ArmKind.DIRECT_AGENT, "direct"))
    fleet = result(task(), arm(ArmKind.FLEET, "fleet"))
    with pytest.raises(ValidationError, match="canonical"):
        ResultBundle(
            id="bundle-order",
            run_id="bundle-order",
            created_at=datetime(2026, 9, 3, tzinfo=UTC),
            benchmark="fixture",
            benchmark_version="v1",
            results=(fleet, direct),
        )
    with pytest.raises(ValidationError, match="duplicate trial ids"):
        ResultBundle(
            id="bundle-duplicate",
            run_id="bundle-duplicate",
            created_at=datetime(2026, 9, 3, tzinfo=UTC),
            benchmark="fixture",
            benchmark_version="v1",
            results=(direct, direct),
        )


def test_history_mapping_requires_canonical_bijection_and_exact_keys() -> None:
    with pytest.raises(ValidationError, match="bijection"):
        HistoryCompatibility(
            benchmark="fixture",
            previous_version="v1",
            current_version="v2",
            task_pairs=((D1, D2), (D3, D2)),
            arm_pairs=(("direct", "direct"),),
            baselines_equivalent=True,
            criteria_equivalent=True,
            capabilities_equivalent=True,
        )
    with pytest.raises(ValidationError, match="canonically sorted"):
        HistoryCompatibility(
            benchmark="fixture",
            previous_version="v1",
            current_version="v2",
            task_pairs=((D2, D2), (D1, D1)),
            arm_pairs=(("direct", "direct"), ("fleet", "fleet")),
            baselines_equivalent=True,
            criteria_equivalent=True,
            capabilities_equivalent=True,
        )


def test_swe_bench_native_records_fail_closed_without_network() -> None:
    adapter = SWEBenchAdapter("verified")
    with pytest.raises(ValidationError, match="JSON array"):
        adapter.normalize_case(
            {
                "instance_id": "django__django-1",
                "repo": "django/django",
                "base_commit": "b" * 40,
                "FAIL_TO_PASS": "not-json",
                "PASS_TO_PASS": "[]",
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        adapter.normalize_case(
            {
                "instance_id": "django__django-1",
                "repo": "django/django",
                "base_commit": "b" * 40,
                "FAIL_TO_PASS": ["fixed"],
                "PASS_TO_PASS": ["regression"],
                "unrecognized_projection": "rejected",
            }
        )


def test_swe_bench_standard_fields_are_retained_and_round_trip() -> None:
    projected = {
        "problem_statement": "Fix the regression.",
        "hints_text": "Inspect the parser.",
        "patch": "diff --git a/source.py b/source.py",
        "test_patch": "diff --git a/test_source.py b/test_source.py",
        "version": "4.2",
        "created_at": "2023-01-02T03:04:05Z",
        "environment_setup_commit": "c" * 40,
    }
    normalized = SWEBenchAdapter("verified").normalize_case(
        {
            "instance_id": "django__django-1",
            "repo": "django/django",
            "base_commit": "b" * 40,
            "FAIL_TO_PASS": ["fixed"],
            "PASS_TO_PASS": ["regression"],
            **projected,
        }
    )

    assert normalized.swe_bench_provenance is not None
    assert normalized.swe_bench_provenance.model_dump(exclude={"schema_version"}) == projected
    encoded = canonical_json(normalized)
    assert canonical_json(TaskIdentity.model_validate_json(encoded, strict=True)) == encoded

    unrelated = task()
    assert unrelated.swe_bench_provenance is None
    with pytest.raises(ValidationError, match="only valid for SWE-bench"):
        TaskIdentity.model_validate(
            {
                **unrelated.model_dump(mode="python"),
                "swe_bench_provenance": normalized.swe_bench_provenance,
            }
        )


def test_stopping_conditions_are_canonical_and_bind_terminal_outcomes() -> None:
    stopping = fairness().stopping
    complete = StoppingManifest(
        maximum_attempts=stopping.maximum_attempts,
        maximum_retries=stopping.maximum_retries,
        maximum_repairs=stopping.maximum_repairs,
        terminal_conditions=(
            "accepted",
            "cancelled",
            "checks-failed",
            "execution-failed",
            "timed-out",
        ),
    )
    assert len(complete.terminal_conditions) == len(TerminalOutcome)
    with pytest.raises(ValidationError, match="unknown terminal conditions"):
        StoppingManifest(
            maximum_attempts=stopping.maximum_attempts,
            maximum_retries=stopping.maximum_retries,
            maximum_repairs=stopping.maximum_repairs,
            terminal_conditions=("accepted", "budget-exhausted"),
        )

    bound_task = task()
    failed = result(bound_task, arm(ArmKind.FLEET, "fleet"), accepted=False)
    restricted_stopping = StoppingManifest(
        maximum_attempts=stopping.maximum_attempts,
        maximum_retries=stopping.maximum_retries,
        maximum_repairs=stopping.maximum_repairs,
        terminal_conditions=("accepted",),
    )
    restricted_fairness = failed.arm.fairness_config.model_copy(
        update={"stopping": restricted_stopping}
    )
    restricted_arm = failed.arm.model_copy(
        update={
            "fairness_config": restricted_fairness,
            "fairness_config_digest": canonical_digest(restricted_fairness),
        }
    )
    payload = failed.model_dump(mode="python")
    payload["arm"] = restricted_arm.model_dump(mode="python")
    payload["id"] = trial_id(bound_task, restricted_arm)
    with pytest.raises(ValidationError, match="not allowed by the retained stopping policy"):
        TrialResult.model_validate(payload)


def test_terminal_outcomes_and_durations_are_coherent() -> None:
    with pytest.raises(ValidationError, match="time to accepted"):
        TrialMetrics(
            **{
                **result(task(), arm(ArmKind.FLEET, "fleet")).metrics.model_dump(),
                "time_to_accepted_seconds": 11,
                "wall_seconds": 10,
            }
        )
    with pytest.raises(ValidationError, match="critical path"):
        TrialMetrics(
            **{
                **result(task(), arm(ArmKind.FLEET, "fleet")).metrics.model_dump(),
                "critical_path_seconds": 11,
                "wall_seconds": 10,
            }
        )
    payload = result(task(), arm(ArmKind.FLEET, "fleet"), accepted=False).model_dump(mode="python")
    payload["failure_classification"] = FailureClassification.TIMEOUT
    with pytest.raises(ValidationError, match="assertion classification"):
        TrialResult.model_validate(payload)


def test_offline_protocol_manifest_covers_required_scenarios() -> None:
    manifest = json.loads(Path("examples/productivity/protocols.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "fleet-productivity-protocols/2"
    required = {
        "codex-direct",
        "codex-fleet",
        "claude-direct",
        "claude-fleet",
        "ablation-no-planning",
        "ablation-no-review",
        "ablation-no-repair",
        "ablation-serial",
        "oss-comparator",
        "native-swe-bench",
        "randomized-real-ab",
        "release-regression",
    }
    assert {item["id"] for item in manifest["protocols"]} == required
    assert all(item["network"] == "disabled" for item in manifest["protocols"])
    for item in manifest["protocols"]:
        assert item["command"][:3] == ["python", "-m", "ai_employee.productivity_protocol"]
        assert "pytest" not in item["command"]
        assert item["artifacts"] and item["evidence"] and item["treatments"]
        assert item["command"].count("{task}") == 1
        assert item["command"].count("{repository}") == 1
        assert item["command"].count("{output}") == 1
        assert item["command"].count("{protocol}") == 1
