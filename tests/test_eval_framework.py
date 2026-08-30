from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import ai_employee.eval_framework as framework
from ai_employee import cli
from ai_employee.config import OperatorConfig, OperatorRoutingConfig, OperatorStrategyConfig
from ai_employee.domain import (
    AcceptedGraphRevision,
    CompletionCriterion,
    EvaluationDecision,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    HarnessCommand,
    HarnessEvaluator,
    HarnessVerification,
    HarnessWorker,
    Node,
    NodeKind,
    OutputContract,
    ProjectHarnessV2,
    RoutingMode,
)
from ai_employee.domain.v2 import (
    AcceptanceLedger,
    ArtifactDescriptor,
    CriterionEvidence,
    WorkerResult,
    WorkspaceSnapshot,
)
from ai_employee.eval_framework import (
    EvalEnvironmentSnapshot,
    EvalExperiment,
    EvalReport,
    EvalResult,
    EvalScenario,
    EvalStrategyBinding,
    EvalVerificationCommand,
    collect_authoritative_result,
    inspect_experiment,
    persist_exact,
    planned_trial,
    run_experiment,
)
from ai_employee.graph_composition import GraphPatchCompositionRecord
from ai_employee.graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationReplay,
)
from ai_employee.inspector import inspect_any_run
from ai_employee.serialization import canonical_digest, project_harness_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeExecutionRecord,
    TaskGraphAcceptance,
)

NOW = datetime(2026, 8, 30, tzinfo=UTC)
DIGEST = "1" * 64


def _harness() -> ProjectHarnessV2:
    return ProjectHarnessV2(
        commands={"test": HarnessCommand(argv=("pytest", "-q"))},
        evaluators=(
            HarnessEvaluator(
                id="unit-tests",
                provider_id="process.harness",
                command_ref="test",
                criterion_ids=("goal-check",),
            ),
        ),
        verification=HarnessVerification(
            required=("test",),
            required_evaluators=("unit-tests",),
        ),
    )


def _scenario(harness: ProjectHarnessV2, repository: str = "/fixture") -> EvalScenario:
    return EvalScenario(
        id="scenario-small",
        run_id="scenario-small",
        created_at=NOW,
        repository=repository,
        base_commit="a" * 40,
        goal="make the bounded change",
        verification_commands=(EvalVerificationCommand(name="test", argv=("pytest", "-q")),),
        tags=("simple",),
        clean_status_digest=canonical_digest(""),
        harness_digest=project_harness_digest(harness),
    )


def _binding(strategy_id: str = "strategy-low") -> EvalStrategyBinding:
    strategy = ExecutionStrategy(
        id=strategy_id,
        routing_mode=RoutingMode.FIXED,
        backend="codex_cli",
        model="fixture",
        effort="low",
        capabilities=("edit_intent", "process"),
    )
    return EvalStrategyBinding(
        strategy=strategy,
        strategy_digest=canonical_digest(strategy),
        strategy_set="eval-set",
    )


def _experiment(
    scenario: EvalScenario, binding: EvalStrategyBinding, *, trials: int = 1
) -> EvalExperiment:
    return EvalExperiment(
        id="eval-small",
        run_id="eval-small",
        created_at=NOW,
        scenario_id=scenario.id,
        scenario_digest=scenario.content_digest or DIGEST,
        strategies=(binding,),
        trials_per_strategy=trials,
        operator_config_digest="2" * 64,
    )


def _environment(scenario: EvalScenario, experiment: EvalExperiment) -> EvalEnvironmentSnapshot:
    return EvalEnvironmentSnapshot(
        repository=scenario.repository,
        head_commit=scenario.base_commit,
        clean_status_digest=scenario.clean_status_digest,
        harness_digest=scenario.harness_digest,
        operator_config_digest=experiment.operator_config_digest,
    )


def _failed_result(
    scenario: EvalScenario,
    experiment: EvalExperiment,
    trial: framework.EvalTrial,
    binding: EvalStrategyBinding,
) -> EvalResult:
    return EvalResult(
        id=f"result-{trial.id}",
        run_id=experiment.id,
        created_at=NOW + timedelta(seconds=1),
        trial_id=trial.id,
        trial_digest=trial.content_digest or DIGEST,
        experiment_digest=experiment.content_digest or DIGEST,
        scenario_digest=scenario.content_digest or DIGEST,
        strategy_id=binding.strategy.id,
        strategy_digest=binding.strategy_digest,
        trial_index=trial.trial_index,
        fleet_run_id=trial.fleet_run_id,
        succeeded=False,
        verified=False,
        total_seconds=1.0,
        failure_code="EXPECTED_FIXTURE_FAILURE",
    )


def test_experiment_restart_reuses_completed_result_without_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    invocations: list[str] = []

    def collect(
        _store: SQLiteStore,
        bound_scenario: EvalScenario,
        bound_experiment: EvalExperiment,
        trial: framework.EvalTrial,
        bound_binding: EvalStrategyBinding,
        *_args: object,
        **_kwargs: object,
    ) -> EvalResult:
        return _failed_result(bound_scenario, bound_experiment, trial, bound_binding)

    monkeypatch.setattr(framework, "collect_authoritative_result", collect)
    with SQLiteStore(tmp_path / "eval.db") as store:
        first = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            lambda: _environment(scenario, experiment),
            lambda _descriptor: b"",
            lambda trial, _binding: invocations.append(trial.fleet_run_id) or 0,
            clock=lambda: NOW,
        )
        second = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            lambda: _environment(scenario, experiment),
            lambda _descriptor: b"",
            lambda trial, _binding: invocations.append(trial.fleet_run_id) or 0,
            clock=lambda: NOW + timedelta(seconds=2),
        )

    assert len(invocations) == 1
    assert first.worker_invocations == 1
    assert second.worker_invocations == 0
    assert second.results == first.results
    assert second.summaries[0].verified_success_rate == 0


def test_running_orphan_is_indeterminate_and_never_automatically_retried(
    tmp_path: Path,
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    invocations: list[str] = []
    with SQLiteStore(tmp_path / "eval.db") as store:
        persist_exact(store, framework.SCENARIO_KIND, scenario)
        persist_exact(store, framework.EXPERIMENT_KIND, experiment)
        persist_exact(store, framework.TRIAL_KIND, trial)
        report = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            lambda: _environment(scenario, experiment),
            lambda _descriptor: b"",
            lambda value, _binding: invocations.append(value.fleet_run_id) or 0,
            clock=lambda: NOW + timedelta(seconds=3),
        )

    assert invocations == []
    assert report.results[0].failure_code == "EVAL_EVIDENCE_INDETERMINATE"
    assert not report.results[0].verified


def test_result_only_crash_gap_finishes_trial_without_worker(tmp_path: Path) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    result = _failed_result(scenario, experiment, trial, binding).model_copy(
        update={"id": framework._result_id(trial)}
    )
    invocations: list[str] = []
    with SQLiteStore(tmp_path / "eval.db") as store:
        persist_exact(store, framework.SCENARIO_KIND, scenario)
        persist_exact(store, framework.EXPERIMENT_KIND, experiment)
        persist_exact(store, framework.TRIAL_KIND, trial)
        persist_exact(store, framework.RESULT_KIND, result)
        report = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            lambda: _environment(scenario, experiment),
            lambda _descriptor: b"",
            lambda value, _binding: invocations.append(value.fleet_run_id) or 0,
            clock=lambda: NOW + timedelta(seconds=2),
        )

    assert invocations == []
    assert report.trials[0].state == "completed"
    assert report.results == (result,)


def test_environment_change_after_trial_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    snapshots = [
        _environment(scenario, experiment),
        _environment(scenario, experiment),
        _environment(scenario, experiment).model_copy(update={"head_commit": "b" * 40}),
    ]
    monkeypatch.setattr(
        framework,
        "collect_authoritative_result",
        lambda *_args, **_kwargs: pytest.fail("changed environment must block collection"),
    )
    with SQLiteStore(tmp_path / "eval.db") as store:
        report = run_experiment(
            store,
            scenario,
            experiment,
            harness,
            lambda: snapshots.pop(0),
            lambda _descriptor: b"",
            lambda _trial, _binding: 0,
            clock=lambda: NOW + timedelta(seconds=1),
        )

    assert report.results[0].failure_code == "EVAL_FIXTURE_CHANGED"
    assert not report.results[0].verified


def test_put_once_rejects_same_scenario_identity_with_different_content(
    tmp_path: Path,
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    conflicting = scenario.model_copy(update={"goal": "different goal", "content_digest": None})
    with SQLiteStore(tmp_path / "eval.db") as store:
        persist_exact(store, framework.SCENARIO_KIND, scenario)
        with pytest.raises(ValueError, match="conflicting persisted"):
            persist_exact(store, framework.SCENARIO_KIND, conflicting)


def _stored_graph_run(
    store: SQLiteStore,
    scenario: EvalScenario,
    experiment: EvalExperiment,
    binding: EvalStrategyBinding,
    trial: framework.EvalTrial,
    *,
    ready: bool,
    store_node: bool = True,
) -> tuple[GraphRunRecord, ParentCandidateEvaluationRecord, AcceptanceLedger, bytes]:
    criterion = CompletionCriterion(
        id="goal-check",
        description="exact candidate passes tests",
        verification_requirement_ids=("test",),
        required_artifact_ids=("workspace_patch",),
    )
    node = Node(
        id="node-one",
        kind=NodeKind.FUNCTION,
        name="bounded node",
        objective=scenario.goal,
        output_contract=OutputContract(id="node-output"),
        required_capabilities=("edit_intent", "process"),
        completion_criteria=(criterion,),
    )
    graph = Graph(
        id="graph-eval",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
    )
    accepted = AcceptedGraphRevision(revision_number=1, graph=graph)
    graph_acceptance = TaskGraphAcceptance(
        id="graph-acceptance-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        accepted_revision=accepted,
        effective_policy_digest="3" * 64,
        harness_digest=scenario.harness_digest,
    )
    store.put(
        "task_graph_acceptance_v2",
        graph_acceptance,
        run_id=trial.fleet_run_id,
    )
    worker = WorkerResult(
        id="worker-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        request_digest="4" * 64,
        status="succeeded",
        duration_seconds=2.5,
        usage={"input_tokens": 10, "output_tokens": 5, "cost": 0.25},
    )
    store.put("worker_result_v2", worker, run_id=trial.fleet_run_id)
    if store_node:
        store.put(
            "node_execution_v2",
            NodeExecutionRecord(
                id="node-execution-eval",
                run_id=trial.fleet_run_id,
                created_at=NOW,
                node_id=node.id,
                accepted_graph_revision_digest=accepted.content_digest or DIGEST,
                generation=0,
                attempt=1,
                sequence=1,
                status="passed" if ready else "failed",
                worker_result_id=worker.id,
                worker_result_digest=worker.content_digest,
                failure_code=None if ready else "VERIFICATION_FAILED",
            ),
            run_id=trial.fleet_run_id,
        )
    patch = b"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n"
    workspace = WorkspaceSnapshot(
        id="workspace-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        repository_identity="5" * 64,
        original_worktree=scenario.repository,
        head_commit=scenario.base_commit,
        base_tree="b" * 40,
        dirty_state_digest="6" * 64,
        isolated_worktree="/fixture-isolated",
        worktree_metadata={"owner": "fleet"},
    )
    descriptor = ArtifactDescriptor(
        id="candidate-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        artifact_digest=canonical_digest(patch.decode()),
        media_type="text/x-diff",
        size_bytes=len(patch),
        logical_kind="workspace_patch",
        producer_action_id=workspace.id,
        source={"base_tree": workspace.base_tree},
        store_locator="sha256/candidate",
    )
    store.put("artifact_descriptor_v2", descriptor, run_id=trial.fleet_run_id)
    composition = GraphPatchCompositionRecord(
        id="composition-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        request_digest="7" * 64,
        accepted_graph_revision_digest=accepted.content_digest or DIGEST,
        base_commit=scenario.base_commit,
        base_tree=workspace.base_tree,
        composition_workspace=workspace,
        candidate_patch=descriptor,
        status="succeeded",
    )
    store.put("graph_patch_composition_v2", composition, run_id=trial.fleet_run_id)
    evaluation = ParentCandidateEvaluationRecord(
        id="parent-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        request_digest="8" * 64,
        accepted_graph_revision_digest=accepted.content_digest or DIGEST,
        composition_record_digest=composition.content_digest or DIGEST,
        composition_workspace_digest=workspace.content_digest or DIGEST,
        candidate_digest="9" * 64,
        candidate_descriptor_digest=descriptor.content_digest or DIGEST,
        candidate_artifact_digest=descriptor.artifact_digest,
        effective_policy_digest="3" * 64,
        goal_evaluator_digest="0" * 64,
        decision=EvaluationDecision.PASS,
        status="ready_to_promote",
    )
    store.put(
        "parent_candidate_evaluation_v2",
        evaluation,
        run_id=trial.fleet_run_id,
    )
    ledger = AcceptanceLedger(
        id="acceptance-eval",
        run_id=trial.fleet_run_id,
        created_at=NOW,
        criteria=(
            CriterionEvidence(
                criterion_id=criterion.id,
                disposition="satisfied",
                evidence_refs=("a" * 64,),
            ),
        ),
    )
    store.put("acceptance_ledger_v2", ledger, run_id=trial.fleet_run_id)
    run = GraphRunRecord(
        id=trial.fleet_run_id,
        goal_id="goal-eval",
        goal=Goal(
            id="goal-eval",
            statement=scenario.goal,
            completion_criteria=(criterion,),
        ),
        execution_policy=ExecutionPolicy(),
        accepted_graph_revision_digest=accepted.content_digest or DIGEST,
        harness_digest=scenario.harness_digest,
        effective_policy_digest="3" * 64,
        available_capabilities=("edit_intent", "process"),
        execution_strategies=(binding.strategy,),
        routing_mode=RoutingMode.FIXED,
        fixed_strategy_id=binding.strategy.id,
        allowed_strategy_ids=(binding.strategy.id,),
        allowed_backends=(binding.strategy.backend,),
        local_backend_allowed=False,
        status="ready_to_promote" if ready else "failed",
        max_concurrency=1,
        max_claims=1,
        repository=scenario.repository,
        base_commit=scenario.base_commit,
        operator_config_digest=experiment.operator_config_digest,
        strategy_set=binding.strategy_set,
        failure_code=None if ready else "VERIFICATION_FAILED",
        composition_id=composition.id if ready else None,
        composition_digest=composition.content_digest if ready else None,
        parent_candidate_artifact_id=descriptor.id if ready else None,
        parent_candidate_digest=descriptor.artifact_digest if ready else None,
        parent_evaluation_id=evaluation.id if ready else None,
        parent_evaluation_digest=evaluation.content_digest if ready else None,
    )
    store.put("graph_run_v2", run, run_id=run.id)
    return run, evaluation, ledger, patch


def test_cli_zero_is_not_success_without_authoritative_parent_pass(tmp_path: Path) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    with SQLiteStore(tmp_path / "eval.db") as store:
        _stored_graph_run(store, scenario, experiment, binding, trial, ready=False)
        result = collect_authoritative_result(
            store,
            scenario,
            experiment,
            trial,
            binding,
            harness,
            lambda _descriptor: b"",
            invocation_exit_code=0,
            finished_at=NOW + timedelta(seconds=4),
        )

    assert result.invocation_exit_code == 0
    assert not result.succeeded
    assert not result.verified
    assert result.failure_code == "VERIFICATION_FAILED"
    assert result.input_tokens == 10
    assert result.cost == 0.25


def test_terminal_failure_without_node_evidence_preserves_run_failure_code(
    tmp_path: Path,
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    with SQLiteStore(tmp_path / "eval.db") as store:
        _stored_graph_run(
            store,
            scenario,
            experiment,
            binding,
            trial,
            ready=False,
            store_node=False,
        )
        result = collect_authoritative_result(
            store,
            scenario,
            experiment,
            trial,
            binding,
            harness,
            lambda _descriptor: b"",
            invocation_exit_code=6,
            finished_at=NOW + timedelta(seconds=1),
        )

    assert result.failure_code == "VERIFICATION_FAILED"
    assert result.worker_seconds is None
    assert result.input_tokens is None


def test_exact_parent_pass_collects_metrics_and_inspector_is_worker_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    with SQLiteStore(tmp_path / "eval.db") as store:
        run, evaluation, ledger, patch = _stored_graph_run(
            store, scenario, experiment, binding, trial, ready=True
        )
        monkeypatch.setattr(
            framework,
            "validate_exact_parent_evidence_store",
            lambda *_args, **_kwargs: ParentCandidateEvaluationReplay(
                record=evaluation,
                acceptance_ledger=ledger,
                evaluation_ledgers=(),
            ),
        )
        result = collect_authoritative_result(
            store,
            scenario,
            experiment,
            trial,
            binding,
            harness,
            lambda descriptor: patch if descriptor.id == "candidate-eval" else b"",
            invocation_exit_code=0,
            finished_at=NOW + timedelta(seconds=4),
        )
        persist_exact(store, framework.SCENARIO_KIND, scenario)
        persist_exact(store, framework.EXPERIMENT_KIND, experiment)
        persist_exact(store, framework.TRIAL_KIND, trial)
        persist_exact(store, framework.RESULT_KIND, result)
        framework._finish_trial(store, trial, result)
        report = inspect_experiment(store, experiment.id)
        inspected = inspect_any_run(store, experiment.id)
        store.put(
            "graph_run_v2",
            run.model_copy(
                update={
                    "generation": run.generation + 1,
                    "status": "failed",
                    "failure_code": "LATE_STALE_MUTATION",
                }
            ),
            run_id=run.id,
            revision=2,
        )
        with pytest.raises(ValueError, match="graph run is stale"):
            inspect_experiment(store, experiment.id)

    assert result.verified
    assert result.succeeded
    assert result.worker_seconds == 2.5
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.cost == 0.25
    assert result.changed_files == 0
    assert result.patch_lines == 2
    assert result.patch_bytes == len(patch)
    assert report.worker_invocations == 0
    assert inspected["worker_invocations"] == 0
    assert "old" not in str(inspected)


def test_foreign_strategy_binding_is_rejected_before_evidence_replay(
    tmp_path: Path,
) -> None:
    harness = _harness()
    scenario = _scenario(harness)
    binding = _binding()
    experiment = _experiment(scenario, binding)
    trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
    with SQLiteStore(tmp_path / "eval.db") as store:
        _stored_graph_run(store, scenario, experiment, binding, trial, ready=True)
        with pytest.raises(ValueError, match="foreign or stale"):
            collect_authoritative_result(
                store,
                scenario,
                experiment,
                trial,
                _binding("strategy-foreign"),
                harness,
                lambda _descriptor: b"",
                invocation_exit_code=0,
                finished_at=NOW + timedelta(seconds=1),
            )


def test_eval_cli_injects_deterministic_trial_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "fixture"
    repository.mkdir()
    harness = _harness().model_copy(
        update={
            "worker": HarnessWorker(
                allowed=("codex_cli",),
                allowed_strategy_ids=("strategy-low",),
            )
        }
    )
    definition = framework.EvalScenarioDefinition(
        id="scenario-cli",
        repository_fixture=str(repository),
        base_commit="a" * 40,
        goal="make the bounded change",
        verification_commands=(EvalVerificationCommand(name="test", argv=("pytest", "-q")),),
    )
    operator = OperatorConfig(
        routing=OperatorRoutingConfig(
            strategies=(
                OperatorStrategyConfig(
                    id="strategy-low",
                    backend="codex_cli",
                    model="fixture",
                    effort="low",
                    capabilities=("edit_intent", "process"),
                ),
            ),
            default_strategy_set="eval-set",
            strategy_sets={"eval-set": ("strategy-low",)},
        )
    )
    monkeypatch.setattr(cli, "load_scenario_definition", lambda _path: definition)
    monkeypatch.setattr(cli, "discover_project_harness", lambda _path: harness)
    monkeypatch.setattr(cli, "load_operator_config", lambda _path: operator)
    monkeypatch.setattr(cli, "now", lambda: NOW)

    def git(*args: object, **_kwargs: object) -> SimpleNamespace:
        command = args[0]
        assert isinstance(command, tuple)
        if "status" in command:
            return SimpleNamespace(stdout="")
        if command[-1] == "--git-dir":
            return SimpleNamespace(stdout=".git\n")
        return SimpleNamespace(stdout=f"{'a' * 40}\n")

    monkeypatch.setattr(cli.subprocess, "run", git)
    seen_run_ids: list[str] = []
    monkeypatch.setattr(
        cli,
        "_work",
        lambda namespace: seen_run_ids.append(namespace.run_id) or 0,
    )

    def evaluate(
        _store: SQLiteStore,
        scenario: EvalScenario,
        experiment: EvalExperiment,
        _harness: ProjectHarnessV2,
        _environment: object,
        _artifact_reader: object,
        executor: object,
        **_kwargs: object,
    ) -> EvalReport:
        binding = experiment.strategies[0]
        trial = planned_trial(experiment, scenario, binding, 1, created_at=NOW)
        assert callable(executor)
        assert executor(trial, binding) == 0
        return EvalReport(
            experiment=experiment,
            scenario=scenario,
            trials=(trial,),
            results=(),
            summaries=(),
            worker_invocations=1,
        )

    monkeypatch.setattr(cli, "run_experiment", evaluate)
    args = SimpleNamespace(
        scenario=str(tmp_path / "scenario.yaml"),
        operator_config=None,
        strategy=["strategy-low"],
        trials=1,
        eval_id=None,
        db=str(tmp_path / "eval.db"),
        json=True,
    )
    assert cli._eval(args) == 0

    assert len(seen_run_ids) == 1
    assert seen_run_ids[0].startswith("evalrun-")
    assert '"worker_invocations":1' in capsys.readouterr().out
