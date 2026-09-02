import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.cli import main
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
    PricingManifest,
    RegressionCheck,
    ResourceBudgetManifest,
    ResultBundle,
    StoppingManifest,
    TaskClass,
    TaskIdentity,
    TerminalOutcome,
    TrialMetrics,
    TrialResult,
    dump_result_bundle,
    trial_id,
)
from ai_employee.serialization import canonical_digest

D1, D2 = "1" * 64, "2" * 64


def _task() -> TaskIdentity:
    return TaskIdentity(
        benchmark="fixture",
        benchmark_version="v1",
        task_id="case-1",
        task_version="v1",
        repository="owner/repo",
        baseline_commit="a" * 40,
        task_class=TaskClass.BUG_FIX,
        acceptance_criteria=(AcceptanceCriterion(id="test", description="pass", authority="test"),),
        regression_checks=(
            RegressionCheck(id="regression", description="stay passing", authority="test"),
        ),
    )


def _arm(kind: ArmKind, arm_id: str, environment: str = D1) -> ArmIdentity:
    environment_manifest = EnvironmentManifest(
        executable="/usr/bin/fixture-worker",
        executable_version="1.0.0",
        dependency_lock_digest=D2,
        sandbox_mode="workspace-write",
        network_mode="disabled",
        cache_policy="cold",
        machine_digest=environment,
    )
    fairness_manifest = FairnessConfigManifest(
        prompt_digest=D1,
        context_digest=D2,
        model_provider="openai",
        model_name="model",
        model_version="snapshot",
        reasoning_effort="high",
        tools=("edit_intent", "process"),
        budgets=ResourceBudgetManifest(wall_seconds=60, input_tokens=10_000, output_tokens=2_000),
        stopping=StoppingManifest(
            maximum_attempts=2,
            maximum_retries=1,
            maximum_repairs=1,
            terminal_conditions=("accepted",),
        ),
        pricing=PricingManifest(
            currency="USD",
            input_per_million=1,
            output_per_million=2,
            subscription_allocation="none",
        ),
        randomized_order=("direct", "fleet"),
    )
    arm_manifest = (
        ArmConfigManifest(planning=False, review=False, repair=False, maximum_parallelism=1)
        if kind is ArmKind.DIRECT_AGENT
        else ArmConfigManifest(planning=True, review=True, repair=True, maximum_parallelism=2)
    )
    return ArmIdentity(
        id=arm_id,
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
        repetition=1,
        assignment_index=("direct", "fleet").index(arm_id),
    )


def _result(bound_arm: ArmIdentity, *, accepted: bool, active: float) -> TrialResult:
    bound_task = _task()
    wall = 10.0 if accepted else 12.0
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
        maximum_parallelism=bound_arm.arm_config.maximum_parallelism,
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
        acceptance_outcomes=(
            CheckOutcome(
                check_id="test",
                authority="test",
                disposition=CheckDisposition.PASSED if accepted else CheckDisposition.FAILED,
                evidence_digest=D1,
            ),
        ),
        regression_outcomes=(
            CheckOutcome(
                check_id="regression",
                authority="test",
                disposition=CheckDisposition.PASSED if accepted else CheckDisposition.FAILED,
                evidence_digest=D2,
            ),
        ),
        terminal_outcome=TerminalOutcome.ACCEPTED if accepted else TerminalOutcome.CHECKS_FAILED,
        failure_classification=None if accepted else FailureClassification.ASSERTION,
        process_exit_code=0,
        metrics=metrics,
    )


def _bundle(path: Path, *, fleet_environment: str = D1) -> Path:
    direct = _result(_arm(ArmKind.DIRECT_AGENT, "direct"), accepted=True, active=2)
    fleet = _result(_arm(ArmKind.FLEET, "fleet", fleet_environment), accepted=False, active=5)
    bundle = ResultBundle(
        id="bundle-v1",
        run_id="bundle-v1",
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
        benchmark="fixture",
        benchmark_version="v1",
        results=(direct, fleet),
    )
    path.write_bytes(dump_result_bundle(bundle))
    return path


def test_productivity_validate_and_json_report_are_canonical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path = _bundle(tmp_path / "bundle.json")

    assert main(["productivity", "validate", str(bundle_path)]) == 0
    validation_text = capsys.readouterr().out
    validation = json.loads(validation_text)
    assert validation["valid"] is True
    assert validation["bundle_id"] == "bundle-v1"
    assert (
        validation_text
        == json.dumps(validation, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )

    assert (
        main(
            [
                "productivity",
                "report",
                str(bundle_path),
                "--direct-arm",
                "direct",
                "--fleet-arm",
                "fleet",
            ]
        )
        == 0
    )
    report_text = capsys.readouterr().out
    report = json.loads(report_text)
    assert (
        report_text
        == json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    )
    assert set(report["arms"][0]["metric_families"]) == {
        "quality",
        "human_effort",
        "wall_time",
        "cost_tokens",
        "reliability_recovery",
        "orchestration",
    }
    comparison = report["paired_comparison"]
    assert comparison["direct_arm_id"] == "direct"
    assert comparison["fleet_arm_id"] == "fleet"
    assert comparison["task_classes_where_fleet_hurts"] == ["bug_fix"]
    reliability = report["arms"][0]["metric_families"]["reliability_recovery"]
    assert {item["metric"] for item in reliability} >= {"terminal_failure", "failure_assertion"}


def test_productivity_markdown_distinguishes_active_and_wall_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle_path = _bundle(tmp_path / "bundle.json")
    assert (
        main(
            [
                "productivity",
                "report",
                str(bundle_path),
                "--direct-arm",
                "direct",
                "--fleet-arm",
                "fleet",
                "--format",
                "markdown",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Human active time (operator attention)" in output
    assert "## Wall-clock time (elapsed latency)" in output
    assert "the two are not interchangeable" in output
    assert "### Task classes where Fleet hurts\n\n- `bug_fix`" in output


def test_productivity_cli_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    canonical_path = _bundle(tmp_path / "bundle.json")

    def fails(arguments: list[str], message: str) -> None:
        with pytest.raises(SystemExit) as raised:
            main(arguments)
        assert raised.value.code == 2
        assert message in capsys.readouterr().err

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_bytes(b" " + canonical_path.read_bytes())
    fails(["productivity", "validate", str(noncanonical)], "not canonical JSON")

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}\n", encoding="utf-8")
    fails(["productivity", "validate", str(malformed)], "validation error")

    fails(
        [
            "productivity",
            "report",
            str(canonical_path),
            "--direct-arm",
            "missing",
            "--fleet-arm",
            "fleet",
        ],
        "direct arm not found",
    )
    fails(
        ["productivity", "report", str(canonical_path), "--format", "yaml"],
        "unsupported output format: yaml",
    )
    fails(
        ["productivity", "report", str(canonical_path), "--direct-arm", "direct"],
        "must be supplied together",
    )

    incomparable_path = _bundle(tmp_path / "incomparable.json", fleet_environment=D2)
    fails(
        [
            "productivity",
            "report",
            str(incomparable_path),
            "--direct-arm",
            "direct",
            "--fleet-arm",
            "fleet",
        ],
        "same capability and controls",
    )
