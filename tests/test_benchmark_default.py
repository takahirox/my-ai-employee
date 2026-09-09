import json
from types import SimpleNamespace

import pytest

from ai_employee import benchmark_default as connection


@pytest.mark.parametrize("completeness", [[], [True], [True, False]])
def test_default_connection_deducts_setup_and_keeps_live_failure_output(
    tmp_path, monkeypatch, capsys, completeness
):
    monkeypatch.setattr(connection, "uuid4", lambda: SimpleNamespace(hex="fixture"))
    root, home, logs = (tmp_path / name for name in ("repo", "home", "logs"))
    root.mkdir()
    (home / "pocket").mkdir(parents=True)
    tick = iter((0.0, 10.0))
    monkeypatch.setattr(connection.time, "monotonic", lambda: next(tick))
    monkeypatch.setattr(
        connection.shutil, "copyfile", lambda _src, dst: dst.write_text("# fixture")
    )
    monkeypatch.setattr(connection, "execute", lambda *args, **kwargs: b"")

    monkeypatch.setattr(connection, "inspect_profile", lambda *_args: {})
    monkeypatch.setattr(
        connection,
        "inspect_usage",
        lambda *_args: {"invocation_details": [{"complete": value} for value in completeness]},
    )

    def product(argv, **kwargs):
        harness = json.loads((root / ".fleet/project.json").read_text())
        assert harness["budgets"]["wall_seconds"] == 168.0
        assert harness["worker"]["adaptive_routing"] is True
        assert "capture_output" not in kwargs
        kwargs["stdout"].write(
            '{"run_id":"benchmark-fixture","status":"failed","stable_code":"TIMEOUT"}'
        )
        kwargs["stdout"].flush()
        assert json.loads((logs / "fleet-result.json").read_text())["status"] == "failed"
        from ai_employee.domain.base import freeze_json
        from ai_employee.model_usage import ModelProcessDiagnostic
        from ai_employee.services_v2._common import now
        from ai_employee.storage import SQLiteStore

        diagnostic = ModelProcessDiagnostic(
            id="process-fixture",
            run_id="benchmark-fixture",
            created_at=now(),
            graph_run_id="benchmark-fixture",
            stage="worker",
            request_digest="0" * 64,
            process_result_digest="1" * 64,
            status="failed",
            exit_code=1,
            duration_seconds=1.0,
            resource_usage=freeze_json({"stdout_bytes": 42}),
            transport_failures=("transport",),
        )
        with SQLiteStore(home / ".fleet/fleet.db") as store:
            store.put_once("model_process_diagnostic_v2", diagnostic, run_id="benchmark-fixture")
            from ai_employee.domain import EvaluationDecision, ExecutionStrategy, RoutingMode
            from ai_employee.parent_review import (
                ParentSemanticReviewDecision,
                ParentSemanticReviewResult,
            )

            strategy = ExecutionStrategy(
                id="reviewer",
                routing_mode=RoutingMode.ADAPTIVE,
                backend="codex_cli",
                model="fixture-model",
                effort="high",
                capabilities=("process",),
            )
            review = ParentSemanticReviewResult(
                id="review-fixture",
                run_id="benchmark-fixture",
                created_at=now(),
                request_digest="0" * 64,
                accepted_graph_revision_digest="1" * 64,
                generation=0,
                review_attempt=0,
                candidate_digest="2" * 64,
                candidate_artifact_digest="3" * 64,
                reviewer_strategy=strategy,
                reviewed_criterion_ids=("criterion",),
                reviewed_node_ids=("node",),
                limitations=("criterion requires execution evidence not supplied",),
            )
            decision = ParentSemanticReviewDecision(
                id="decision-fixture",
                run_id="benchmark-fixture",
                created_at=now(),
                request_digest=review.request_digest,
                result_digest=review.content_digest,
                accepted_graph_revision_digest=review.accepted_graph_revision_digest,
                generation=0,
                review_attempt=0,
                candidate_digest=review.candidate_digest,
                candidate_artifact_digest=review.candidate_artifact_digest,
                action=EvaluationDecision.ESCALATE,
                reason_code="PARENT_SEMANTIC_COVERAGE_LIMITED",
            )
            store.put_once("parent_semantic_review_result_v2", review, run_id="benchmark-fixture")
            store.put_once(
                "parent_semantic_review_decision_v2", decision, run_id="benchmark-fixture"
            )
        kwargs["stderr"].write("diagnostic fixture")
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(connection.subprocess, "run", product)
    assert connection.run({"instruction": "fixture", "seconds": 180.0}, root, home, logs) == 0
    assert (logs / "fleet.stderr").read_text() == "diagnostic fixture"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    usage = next(event for event in events if event["type"] == "pocket.usage")
    assert usage["usage"] == {}
    assert usage["complete"] is (bool(completeness) and all(completeness))
    details = json.loads((logs / "fleet-diagnostics.json").read_text())
    diagnostic = details["model_process_diagnostics"][0]
    assert diagnostic["resource_usage"] == {"stdout_bytes": 42}
    assert diagnostic["transport_failures"] == ["transport"]
    assert "limitations" not in details["parent_review_results"][0]
    assert "free_text_and_artifact_bodies" in details["omissions"]
    assert details["parent_review_decisions"][0]["action"] == "ESCALATE"
    assert details["parent_review_decisions"][0]["candidate_digest"] == "2" * 64


@pytest.mark.parametrize("seconds", [0, -1, True, float("nan"), float("inf")])
def test_default_connection_does_not_start_with_invalid_allowance(tmp_path, seconds):
    with pytest.raises(ValueError):
        connection.run({"seconds": seconds}, tmp_path, tmp_path, tmp_path)


def test_public_validation_binds_exact_goal_and_declared_commands(tmp_path):
    from ai_employee.cli import _work_goal
    from ai_employee.goal_acceptance import attach_goal_checks, harness_for_goal
    from ai_employee.project import discover_project_harness

    root = tmp_path / "repo"
    (root / ".fleet").mkdir(parents=True)
    harness = connection.make_harness(60)
    request = {
        "instruction": "Write every ID to output/result.json",
        "settings": {
            "observation_hosts": ["127.0.0.1"],
            "public_acceptance": {
                "checks": {
                    "schema_version": "1",
                    "goal": "Write every ID to output/result.json",
                    "criteria": [
                        {
                            "id": "all-ids",
                            "request_fragment": "every ID",
                            "description": "Every public input ID is represented",
                            "command_ref": "coverage",
                        }
                    ],
                },
                "commands": {
                    "coverage": (
                        "import json; from pathlib import Path; "
                        "assert set(json.loads(Path('output/result.json').read_text())) "
                        "== set(json.loads(Path('input/ids.json').read_text()))"
                    )
                },
            },
        },
    }
    argv = connection.configure_validation(request, harness, root)
    (root / ".fleet/project.json").write_text(json.dumps(harness))
    discovered = discover_project_harness(root)
    goal = attach_goal_checks(_work_goal("run", request["instruction"], discovered), argv[1])
    effective = harness_for_goal(discovered, goal)
    assert "coverage" in effective.verification.required
    assert "goal.acceptance.all-ids" in effective.verification.required_evaluators
    assert effective.verification.review.parent_semantic_review
    assert effective.worker.scratch_validation
    assert effective.worker.observation_hosts == ("127.0.0.1",)
    assert any(c.id == "goal.acceptance.all-ids" and c.mandatory for c in goal.completion_criteria)
    request["instruction"] = "different goal"
    with pytest.raises(ValueError, match="exact original"):
        connection.configure_validation(request, harness, root)


def test_cli_supplies_explicit_operator_settings(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"instruction": "fixture", "seconds": 60}))
    settings = tmp_path / "settings.json"
    settings.write_text('{"observation_hosts":["127.0.0.1"]}')
    monkeypatch.setattr(
        connection.sys, "argv", ["fleet-benchmark", str(request), "--settings", str(settings)]
    )

    def capture(request, *args):
        assert request["settings"]["observation_hosts"] == ["127.0.0.1"]
        return 0

    monkeypatch.setattr(connection, "run", capture)
    assert connection.main() == 0
