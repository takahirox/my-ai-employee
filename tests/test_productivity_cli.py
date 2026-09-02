import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.cli import main
from ai_employee.productivity_evaluation import (
    AcceptanceCriterion,
    ArmIdentity,
    ArmKind,
    ResultBundle,
    TaskClass,
    TaskIdentity,
    TrialMetrics,
    TrialResult,
    dump_result_bundle,
    trial_id,
)

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
    )


def _arm(kind: ArmKind, arm_id: str, environment: str = D1) -> ArmIdentity:
    return ArmIdentity(
        id=arm_id,
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
        repetition=1,
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
