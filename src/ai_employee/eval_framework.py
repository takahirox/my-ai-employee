"""Small, deterministic experiments over authoritative Fleet run records."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import ClassVar, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .domain import ExecutionStrategy, ProjectHarnessV2, RoutingMode
from .domain.base import Digest, Identifier, UtcTimestamp
from .domain.browser import BrowserObservation
from .domain.v2 import (
    AcceptanceLedger,
    ApprovalRequest,
    ArtifactDescriptor,
    DigestedRecordV2,
    ExecutionResult,
    SchemaModelV2,
    WorkerRequest,
    WorkerResult,
)
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import ParentCandidateEvaluationRecord
from .promotion_approval import validate_exact_parent_evidence_store
from .serialization import canonical_digest, loads_yaml_model, project_harness_digest
from .storage import SQLiteStore
from .task_orchestration import GraphRunRecord, NodeExecutionRecord, TaskGraphAcceptance

MAX_TRIALS_PER_STRATEGY = 20
MAX_STRATEGIES = 8
MAX_TOTAL_TRIALS = 40

SCENARIO_KIND = "strategy_eval_scenario_v2"
EXPERIMENT_KIND = "strategy_eval_experiment_v2"
TRIAL_KIND = "strategy_eval_trial_v2"
RESULT_KIND = "strategy_eval_result_v2"


class EvalVerificationCommand(SchemaModelV2):
    """One exact required Harness command named by a scenario."""

    schema_name: ClassVar[str] = "eval_verification_command"
    name: Identifier
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(default=".", min_length=1, max_length=1_000)
    inherit_environment: tuple[Identifier, ...] = ()

    @field_validator("argv")
    @classmethod
    def _argv_is_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("verification argv entries must be non-empty and NUL-free")
        return value


class EvalScenarioDefinition(SchemaModelV2):
    """Strict operator input; the resolved fixture is persisted separately."""

    schema_name: ClassVar[str] = "eval_scenario_definition"
    id: Identifier
    repository_fixture: str = Field(min_length=1, max_length=4_096)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    goal: str = Field(min_length=1, max_length=20_000)
    verification_commands: tuple[EvalVerificationCommand, ...] = ()
    tags: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _ordered_values_are_unique(self) -> EvalScenarioDefinition:
        command_names = tuple(item.name for item in self.verification_commands)
        if len(command_names) != len(set(command_names)):
            raise ValueError("scenario verification command names must be unique")
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("scenario tags must be unique")
        return self


class EvalEnvironmentSnapshot(SchemaModelV2):
    """Body-free identity checked before and after every trial."""

    schema_name: ClassVar[str] = "eval_environment_snapshot"
    repository: str = Field(min_length=1, max_length=4_096)
    head_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    clean_status_digest: Digest
    harness_digest: Digest
    operator_config_digest: Digest


class EvalScenario(DigestedRecordV2):
    """Resolved, immutable scenario bound to the clean fixture and Harness."""

    schema_name: ClassVar[str] = "eval_scenario"
    repository: str = Field(min_length=1, max_length=4_096)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    goal: str = Field(min_length=1, max_length=20_000)
    verification_commands: tuple[EvalVerificationCommand, ...] = ()
    tags: tuple[Identifier, ...] = ()
    clean_status_digest: Digest
    harness_digest: Digest
    harness: ProjectHarnessV2

    @model_validator(mode="after")
    def _scenario_values_are_unique(self) -> EvalScenario:
        if self.id != self.run_id:
            raise ValueError("evaluation scenario identity must equal its run identity")
        names = tuple(item.name for item in self.verification_commands)
        if len(names) != len(set(names)) or len(self.tags) != len(set(self.tags)):
            raise ValueError("persisted scenario command names and tags must be unique")
        if project_harness_digest(self.harness) != self.harness_digest:
            raise ValueError("persisted scenario Harness digest is stale")
        return self


class EvalStrategyBinding(SchemaModelV2):
    """Exact operator strategy admitted to one fixed-routing experiment."""

    schema_name: ClassVar[str] = "eval_strategy_binding"
    strategy: ExecutionStrategy
    strategy_digest: Digest
    strategy_set: Identifier | None = None

    @model_validator(mode="after")
    def _strategy_is_exact(self) -> EvalStrategyBinding:
        if self.strategy.routing_mode is not RoutingMode.FIXED:
            raise ValueError("evaluation strategies must use fixed routing")
        if canonical_digest(self.strategy) != self.strategy_digest:
            raise ValueError("evaluation strategy digest is stale")
        return self


class EvalExperiment(DigestedRecordV2):
    """Immutable plan whose deterministic IDs make restart behavior unambiguous."""

    schema_name: ClassVar[str] = "eval_experiment"
    scenario_id: Identifier
    scenario_digest: Digest
    strategies: tuple[EvalStrategyBinding, ...] = Field(min_length=1, max_length=MAX_STRATEGIES)
    trials_per_strategy: int = Field(ge=1, le=MAX_TRIALS_PER_STRATEGY)
    operator_config_digest: Digest

    @model_validator(mode="after")
    def _plan_is_bounded(self) -> EvalExperiment:
        if self.id != self.run_id:
            raise ValueError("evaluation experiment identity must equal its run identity")
        ids = tuple(item.strategy.id for item in self.strategies)
        if len(ids) != len(set(ids)):
            raise ValueError("experiment strategy IDs must be unique")
        if len(ids) * self.trials_per_strategy > MAX_TOTAL_TRIALS:
            raise ValueError("experiment exceeds the bounded total trial limit")
        return self


class EvalTrial(DigestedRecordV2):
    """One exact Scenario x Strategy x Trial execution attempt."""

    schema_name: ClassVar[str] = "eval_trial"
    experiment_digest: Digest
    scenario_digest: Digest
    strategy_id: Identifier
    strategy_digest: Digest
    strategy_set: Identifier | None = None
    trial_index: int = Field(ge=1, le=MAX_TRIALS_PER_STRATEGY)
    fleet_run_id: Identifier
    repository: str = Field(min_length=1, max_length=4_096)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    state: Literal["running", "completed"]
    generation: int = Field(default=0, ge=0)
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None = None
    result_id: Identifier | None = None
    result_digest: Digest | None = None

    @model_validator(mode="after")
    def _state_is_complete(self) -> EvalTrial:
        finished = self.state == "completed"
        if finished != all(
            value is not None for value in (self.finished_at, self.result_id, self.result_digest)
        ):
            raise ValueError("completed trial requires exactly one result binding")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("trial cannot finish before it started")
        expected_generation = 1 if finished else 0
        if self.generation != expected_generation:
            raise ValueError("evaluation trial permits only running(0) then completed(1)")
        return self


class EvalResult(DigestedRecordV2):
    """Fail-closed metrics derived only from one bound authoritative Fleet run."""

    schema_name: ClassVar[str] = "eval_result"
    trial_id: Identifier
    trial_digest: Digest
    experiment_digest: Digest
    scenario_digest: Digest
    strategy_id: Identifier
    strategy_digest: Digest
    trial_index: int = Field(ge=1, le=MAX_TRIALS_PER_STRATEGY)
    fleet_run_id: Identifier
    graph_run_digest: Digest | None = None
    accepted_graph_revision_digest: Digest | None = None
    harness_digest: Digest | None = None
    effective_policy_digest: Digest | None = None
    parent_evaluation_digest: Digest | None = None
    acceptance_ledger_digest: Digest | None = None
    worker_result_digests: tuple[Digest, ...] = ()
    verification_result_digests: tuple[Digest, ...] = ()
    candidate_artifact_digest: Digest | None = None
    succeeded: bool
    verified: bool
    invocation_exit_code: int | None = None
    total_seconds: float = Field(ge=0)
    worker_seconds: float | None = Field(default=None, ge=0)
    verification_seconds: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)
    attempts: int = Field(default=0, ge=0)
    approvals_required: int = Field(default=0, ge=0)
    changed_files: int | None = Field(default=None, ge=0)
    patch_lines: int | None = Field(default=None, ge=0)
    patch_bytes: int | None = Field(default=None, ge=0)
    failure_code: str | None = Field(default=None, max_length=500)

    @field_validator("total_seconds", "worker_seconds", "verification_seconds", "cost")
    @classmethod
    def _metrics_are_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("evaluation metrics must be finite")
        return value

    @model_validator(mode="after")
    def _success_requires_verified_evidence(self) -> EvalResult:
        if self.verified and not self.succeeded:
            raise ValueError("verified trial must have a successful authoritative run")
        if self.verified and self.failure_code is not None:
            raise ValueError("verified trial cannot carry a failure code")
        if not self.verified and not self.failure_code:
            raise ValueError("unverified trial requires a stable failure code")
        verified_bindings = (
            self.graph_run_digest,
            self.accepted_graph_revision_digest,
            self.harness_digest,
            self.effective_policy_digest,
            self.parent_evaluation_digest,
            self.acceptance_ledger_digest,
            self.candidate_artifact_digest,
        )
        if self.verified and any(value is None for value in verified_bindings):
            raise ValueError("verified trial requires complete evidence bindings")
        if len(self.worker_result_digests) != len(set(self.worker_result_digests)) or len(
            self.verification_result_digests
        ) != len(set(self.verification_result_digests)):
            raise ValueError("evaluation result evidence digests must be unique")
        return self


class EvalStrategySummary(SchemaModelV2):
    schema_name: ClassVar[str] = "eval_strategy_summary"
    strategy_id: Identifier
    planned_trials: int = Field(ge=1)
    completed_trials: int = Field(ge=0)
    verified_successes: int = Field(ge=0)
    verified_success_rate: float = Field(ge=0, le=1)
    median_total_seconds: float | None = Field(default=None, ge=0)
    median_worker_seconds: float | None = Field(default=None, ge=0)
    median_verification_seconds: float | None = Field(default=None, ge=0)
    total_input_tokens: int | None = Field(default=None, ge=0)
    total_output_tokens: int | None = Field(default=None, ge=0)
    total_cost: float | None = Field(default=None, ge=0)


class EvalReport(SchemaModelV2):
    schema_name: ClassVar[str] = "eval_report"
    experiment: EvalExperiment
    scenario: EvalScenario
    trials: tuple[EvalTrial, ...]
    results: tuple[EvalResult, ...]
    summaries: tuple[EvalStrategySummary, ...]
    worker_invocations: int = Field(ge=0)


class TrialExecutor(Protocol):
    def __call__(self, trial: EvalTrial, binding: EvalStrategyBinding) -> int: ...


EnvironmentReader = Callable[[], EvalEnvironmentSnapshot]
ArtifactReader = Callable[[ArtifactDescriptor], bytes]


def load_scenario_definition(path: str | Path) -> EvalScenarioDefinition:
    return loads_yaml_model(Path(path).read_text(encoding="utf-8"), EvalScenarioDefinition)


def resolve_scenario(
    definition: EvalScenarioDefinition,
    scenario_path: str | Path,
    environment: EvalEnvironmentSnapshot,
    harness: ProjectHarnessV2,
    *,
    created_at: datetime,
) -> EvalScenario:
    source = Path(scenario_path).resolve()
    requested = Path(definition.repository_fixture).expanduser()
    repository = (
        (source.parent / requested).resolve()
        if not requested.is_absolute()
        else requested.resolve()
    )
    expected_commands = tuple(
        EvalVerificationCommand(
            name=name,
            argv=harness.commands[name].argv,
            cwd=harness.commands[name].cwd,
            inherit_environment=harness.commands[name].inherit_environment,
        )
        for name in harness.verification.required
    )
    if (
        str(repository) != environment.repository
        or definition.base_commit != environment.head_commit
        or environment.harness_digest != project_harness_digest(harness)
        or definition.verification_commands != expected_commands
    ):
        raise ValueError(
            "scenario does not exactly match the clean fixture and required Harness commands"
        )
    return EvalScenario(
        id=definition.id,
        run_id=definition.id,
        created_at=created_at,
        repository=str(repository),
        base_commit=definition.base_commit,
        goal=definition.goal,
        verification_commands=definition.verification_commands,
        tags=definition.tags,
        clean_status_digest=environment.clean_status_digest,
        harness_digest=environment.harness_digest,
        harness=harness,
    )


def deterministic_experiment_id(
    scenario: EvalScenario, bindings: tuple[EvalStrategyBinding, ...], trials: int
) -> Identifier:
    digest = canonical_digest(
        {
            "scenario": scenario.content_digest,
            "strategies": tuple(item.strategy_digest for item in bindings),
            "trials": trials,
        }
    )
    return f"eval-{digest[:32]}"


def planned_trial(
    experiment: EvalExperiment,
    scenario: EvalScenario,
    binding: EvalStrategyBinding,
    trial_index: int,
    *,
    created_at: datetime,
) -> EvalTrial:
    identity = canonical_digest(
        {
            "experiment_id": experiment.id,
            "experiment": experiment.content_digest,
            "strategy": binding.strategy_digest,
            "trial_index": trial_index,
        }
    )
    return EvalTrial(
        id=f"eval-trial-{identity[:28]}",
        run_id=experiment.id,
        created_at=created_at,
        experiment_digest=_required(experiment.content_digest),
        scenario_digest=_required(scenario.content_digest),
        strategy_id=binding.strategy.id,
        strategy_digest=binding.strategy_digest,
        strategy_set=binding.strategy_set,
        trial_index=trial_index,
        fleet_run_id=f"evalrun-{identity[:28]}",
        repository=scenario.repository,
        base_commit=scenario.base_commit,
        state="running",
        started_at=created_at,
    )


def persist_exact(
    store: SQLiteStore,
    kind: str,
    record: DigestedRecordV2,
    *,
    revision: int = 1,
) -> DigestedRecordV2:
    """Insert once; an occupied identity must contain the same immutable fact."""

    if store.put_once(kind, record, run_id=record.run_id, revision=revision):
        return record
    existing = store.get(kind, record.id, type(record), revision=revision)
    if (
        existing.run_id != record.run_id
        or existing.content_digest != record.content_digest
        or existing.id != record.id
    ):
        raise ValueError(f"conflicting persisted {kind} identity")
    return existing


def run_experiment(
    store: SQLiteStore,
    scenario: EvalScenario,
    experiment: EvalExperiment,
    harness: ProjectHarnessV2,
    environment_reader: EnvironmentReader,
    artifact_reader: ArtifactReader,
    executor: TrialExecutor,
    *,
    clock: Callable[[], datetime],
) -> EvalReport:
    """Run or recover a bounded experiment without retrying orphaned workers."""

    persisted_scenario = persist_exact(store, SCENARIO_KIND, scenario)
    persisted_experiment = persist_exact(store, EXPERIMENT_KIND, experiment)
    assert isinstance(persisted_scenario, EvalScenario)
    assert isinstance(persisted_experiment, EvalExperiment)
    _validate_environment(environment_reader(), scenario, experiment)
    worker_invocations = 0
    for binding in experiment.strategies:
        for trial_index in range(1, experiment.trials_per_strategy + 1):
            template = planned_trial(experiment, scenario, binding, trial_index, created_at=clock())
            try:
                existing = store.get(TRIAL_KIND, template.id, EvalTrial)
            except KeyError:
                trial = persist_exact(store, TRIAL_KIND, template)
                assert isinstance(trial, EvalTrial)
                _validate_environment(environment_reader(), scenario, experiment)
                invocation_exit_code: int | None
                try:
                    store.get("graph_run_v2", trial.fleet_run_id, GraphRunRecord)
                except KeyError:
                    worker_invocations += 1
                    try:
                        invocation_exit_code = executor(trial, binding)
                    except Exception:
                        invocation_exit_code = None
                else:
                    invocation_exit_code = None
                finished_at = clock()
                environment_failure = False
                try:
                    _validate_environment(environment_reader(), scenario, experiment)
                except ValueError:
                    environment_failure = True
                result = _recover_or_fail_result(
                    store,
                    scenario,
                    experiment,
                    trial,
                    binding,
                    harness,
                    artifact_reader,
                    invocation_exit_code=invocation_exit_code,
                    finished_at=finished_at,
                    failure_override=("EVAL_FIXTURE_CHANGED" if environment_failure else None),
                )
            else:
                trial = existing
                _validate_trial_plan(trial, template)
                if trial.state == "completed":
                    _load_exact_result(store, trial, experiment, scenario, binding)
                    continue
                result = _recover_or_fail_result(
                    store,
                    scenario,
                    experiment,
                    trial,
                    binding,
                    harness,
                    artifact_reader,
                    invocation_exit_code=None,
                    finished_at=clock(),
                    failure_override=None,
                )
            _finish_trial(store, trial, result)
    return inspect_experiment(store, experiment.id, worker_invocations=worker_invocations)


def inspect_experiment(
    store: SQLiteStore, experiment_id: str, *, worker_invocations: int = 0
) -> EvalReport:
    experiment = store.get(EXPERIMENT_KIND, experiment_id, EvalExperiment)
    scenario = store.get(SCENARIO_KIND, experiment.scenario_id, EvalScenario)
    if (
        experiment.id != experiment_id
        or experiment.run_id != experiment.id
        or scenario.id != experiment.scenario_id
        or scenario.run_id != scenario.id
        or scenario.content_digest != experiment.scenario_digest
    ):
        raise ValueError("experiment scenario binding is stale")
    all_trials = store.list_records(TRIAL_KIND, EvalTrial, run_id=experiment.id)
    latest: dict[str, EvalTrial] = {}
    for trial in all_trials:
        prior = latest.get(trial.id)
        if prior is None or trial.generation > prior.generation:
            latest[trial.id] = trial
        elif trial.generation == prior.generation:
            raise ValueError("evaluation trial generation is ambiguous")
    trials = tuple(sorted(latest.values(), key=lambda item: (item.strategy_id, item.trial_index)))
    seen_pairs: set[tuple[str, int]] = set()
    for trial in trials:
        binding = _binding(experiment, trial.strategy_id)
        expected = planned_trial(
            experiment,
            scenario,
            binding,
            trial.trial_index,
            created_at=trial.started_at,
        )
        _validate_trial_plan(trial, expected)
        pair = (trial.strategy_id, trial.trial_index)
        if pair in seen_pairs:
            raise ValueError("evaluation trial plan is ambiguous")
        seen_pairs.add(pair)
    results = tuple(
        _load_exact_result(
            store,
            trial,
            experiment,
            scenario,
            _binding(experiment, trial.strategy_id),
        )
        for trial in trials
        if trial.state == "completed"
    )
    return EvalReport(
        experiment=experiment,
        scenario=scenario,
        trials=trials,
        results=results,
        summaries=_summaries(experiment, results),
        worker_invocations=worker_invocations,
    )


def collect_authoritative_result(
    store: SQLiteStore,
    scenario: EvalScenario,
    experiment: EvalExperiment,
    trial: EvalTrial,
    binding: EvalStrategyBinding,
    harness: ProjectHarnessV2,
    artifact_reader: ArtifactReader,
    *,
    invocation_exit_code: int | None,
    finished_at: datetime,
) -> EvalResult:
    """Strictly aggregate one current graph run or reject stale/foreign evidence."""

    run = store.get("graph_run_v2", trial.fleet_run_id, GraphRunRecord)
    strategy_matches = tuple(
        item for item in run.execution_strategies if item.id == binding.strategy.id
    )
    if (
        run.id != trial.fleet_run_id
        or run.repository != scenario.repository
        or run.base_commit != scenario.base_commit
        or run.goal.statement != scenario.goal
        or run.routing_mode is not RoutingMode.FIXED
        or run.fixed_strategy_id != binding.strategy.id
        or run.strategy_set != binding.strategy_set
        or run.operator_config_digest != experiment.operator_config_digest
        or run.harness_digest != scenario.harness_digest
        or len(strategy_matches) != 1
        or canonical_digest(strategy_matches[0]) != binding.strategy_digest
    ):
        raise ValueError("Fleet run is foreign or stale for this evaluation trial")
    acceptances = tuple(
        item
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
        )
        if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
    )
    if len(acceptances) != 1:
        raise ValueError("current accepted graph revision is missing or ambiguous")
    acceptance = acceptances[0]
    if acceptance.run_id != run.id or acceptance.harness_digest != scenario.harness_digest:
        raise ValueError("accepted graph belongs to another trial or Harness")

    attempts, all_worker_results, worker_evidence_complete = _all_authority_bound_worker_results(
        store, run
    )
    approvals = len(store.list_records("approval_request_v2", ApprovalRequest, run_id=run.id))
    total_seconds = max(0.0, (finished_at - trial.started_at).total_seconds())

    if run.status != "ready_to_promote" or run.failure_code is not None:
        worker_seconds = (
            sum(item.duration_seconds for item in all_worker_results)
            if all_worker_results and worker_evidence_complete
            else None
        )
        input_tokens = (
            _complete_usage(all_worker_results, "input_tokens", integer=True)
            if worker_evidence_complete
            else None
        )
        output_tokens = (
            _complete_usage(all_worker_results, "output_tokens", integer=True)
            if worker_evidence_complete
            else None
        )
        cost = (
            _complete_usage(all_worker_results, "cost", integer=False)
            if worker_evidence_complete
            else None
        )
        return _result(
            experiment,
            scenario,
            trial,
            run,
            invocation_exit_code,
            total_seconds,
            worker_seconds,
            None,
            input_tokens,
            output_tokens,
            cost,
            attempts,
            approvals,
            all_worker_results,
            failure_code=run.failure_code or f"EVAL_RUN_{run.status.upper()}",
            finished_at=finished_at,
        )
    current_nodes = _current_nodes(store, run, acceptance)
    _bound_worker_results(store, run, current_nodes)
    worker_seconds = (
        sum(item.duration_seconds for item in all_worker_results)
        if all_worker_results and worker_evidence_complete
        else None
    )
    input_tokens = (
        _complete_usage(all_worker_results, "input_tokens", integer=True)
        if worker_evidence_complete
        else None
    )
    output_tokens = (
        _complete_usage(all_worker_results, "output_tokens", integer=True)
        if worker_evidence_complete
        else None
    )
    cost = (
        _complete_usage(all_worker_results, "cost", integer=False)
        if worker_evidence_complete
        else None
    )
    if (
        run.parent_evaluation_id is None
        or run.parent_evaluation_digest is None
        or run.composition_id is None
        or run.composition_digest is None
    ):
        raise ValueError("successful graph run lacks exact parent evidence")
    evaluation = store.get(
        "parent_candidate_evaluation_v2",
        run.parent_evaluation_id,
        ParentCandidateEvaluationRecord,
    )
    if evaluation.content_digest != run.parent_evaluation_digest:
        raise ValueError("parent evaluation pointer is stale")
    replay = validate_exact_parent_evidence_store(
        store,
        run,
        acceptance.accepted_revision,
        evaluation,
        harness,
    )
    composition = store.get(
        "graph_patch_composition_v2", run.composition_id, GraphPatchCompositionRecord
    )
    if (
        composition.run_id != run.id
        or composition.content_digest != run.composition_digest
        or composition.base_commit != scenario.base_commit
        or composition.candidate_patch is None
        or composition.candidate_patch.artifact_digest != evaluation.candidate_artifact_digest
    ):
        raise ValueError("parent composition binding is stale")
    patch = artifact_reader(composition.candidate_patch)
    if len(patch) != composition.candidate_patch.size_bytes:
        raise ValueError("candidate patch size is stale")
    changed_paths = {path for item in composition.ordered_inputs for path in item.paths}
    patch_lines = sum(
        1
        for line in patch.splitlines()
        if (line.startswith(b"+") and not line.startswith(b"+++"))
        or (line.startswith(b"-") and not line.startswith(b"---"))
    )
    verification_seconds = _verification_seconds(
        store, run.id, evaluation.verification_result_digests
    )
    return EvalResult(
        id=_result_id(trial),
        run_id=experiment.id,
        created_at=finished_at,
        trial_id=trial.id,
        trial_digest=_required(trial.content_digest),
        experiment_digest=_required(experiment.content_digest),
        scenario_digest=_required(scenario.content_digest),
        strategy_id=binding.strategy.id,
        strategy_digest=binding.strategy_digest,
        trial_index=trial.trial_index,
        fleet_run_id=trial.fleet_run_id,
        graph_run_digest=canonical_digest(run),
        accepted_graph_revision_digest=run.accepted_graph_revision_digest,
        harness_digest=run.harness_digest,
        effective_policy_digest=run.effective_policy_digest,
        parent_evaluation_digest=_required(evaluation.content_digest),
        acceptance_ledger_digest=_required(replay.acceptance_ledger.content_digest),
        worker_result_digests=tuple(_required(item.content_digest) for item in all_worker_results),
        verification_result_digests=evaluation.verification_result_digests,
        candidate_artifact_digest=composition.candidate_patch.artifact_digest,
        succeeded=True,
        verified=True,
        invocation_exit_code=invocation_exit_code,
        total_seconds=total_seconds,
        worker_seconds=worker_seconds,
        verification_seconds=verification_seconds,
        input_tokens=None if input_tokens is None else int(input_tokens),
        output_tokens=None if output_tokens is None else int(output_tokens),
        cost=cost,
        attempts=attempts,
        approvals_required=approvals,
        changed_files=len(changed_paths),
        patch_lines=patch_lines,
        patch_bytes=composition.candidate_patch.size_bytes,
    )


def _recover_or_fail_result(
    store: SQLiteStore,
    scenario: EvalScenario,
    experiment: EvalExperiment,
    trial: EvalTrial,
    binding: EvalStrategyBinding,
    harness: ProjectHarnessV2,
    artifact_reader: ArtifactReader,
    *,
    invocation_exit_code: int | None,
    finished_at: datetime,
    failure_override: str | None,
) -> EvalResult:
    try:
        existing = store.get(RESULT_KIND, _result_id(trial), EvalResult)
    except KeyError:
        existing = None
    if existing is not None:
        _validate_result_binding(existing, trial, experiment, scenario, binding)
        _validate_saved_result_evidence(store, existing, scenario)
        return existing
    if failure_override is None:
        try:
            result = collect_authoritative_result(
                store,
                scenario,
                experiment,
                trial,
                binding,
                harness,
                artifact_reader,
                invocation_exit_code=invocation_exit_code,
                finished_at=finished_at,
            )
        except (KeyError, OSError, TypeError, ValueError):
            failure_override = "EVAL_EVIDENCE_INDETERMINATE"
        else:
            persisted = persist_exact(store, RESULT_KIND, result)
            assert isinstance(persisted, EvalResult)
            return persisted
    result = EvalResult(
        id=_result_id(trial),
        run_id=experiment.id,
        created_at=finished_at,
        trial_id=trial.id,
        trial_digest=_required(trial.content_digest),
        experiment_digest=_required(experiment.content_digest),
        scenario_digest=_required(scenario.content_digest),
        strategy_id=binding.strategy.id,
        strategy_digest=binding.strategy_digest,
        trial_index=trial.trial_index,
        fleet_run_id=trial.fleet_run_id,
        succeeded=False,
        verified=False,
        invocation_exit_code=invocation_exit_code,
        total_seconds=max(0.0, (finished_at - trial.started_at).total_seconds()),
        failure_code=failure_override,
    )
    persisted = persist_exact(store, RESULT_KIND, result)
    assert isinstance(persisted, EvalResult)
    return persisted


def _finish_trial(store: SQLiteStore, trial: EvalTrial, result: EvalResult) -> EvalTrial:
    completed = EvalTrial.model_validate(
        {
            **trial.model_dump(mode="python"),
            "state": "completed",
            "generation": trial.generation + 1,
            "finished_at": result.created_at,
            "result_id": result.id,
            "result_digest": result.content_digest,
            "content_digest": None,
        },
        strict=True,
    )
    persisted = persist_exact(store, TRIAL_KIND, completed, revision=completed.generation + 1)
    assert isinstance(persisted, EvalTrial)
    return persisted


def _load_exact_result(
    store: SQLiteStore,
    trial: EvalTrial,
    experiment: EvalExperiment,
    scenario: EvalScenario,
    binding: EvalStrategyBinding,
) -> EvalResult:
    if trial.result_id is None or trial.result_digest is None:
        raise ValueError("completed trial is missing its result pointer")
    candidates = tuple(
        item
        for item in store.list_records(RESULT_KIND, EvalResult, run_id=experiment.id)
        if item.id == trial.result_id
    )
    if len(candidates) != 1:
        raise ValueError("completed trial result is missing or ambiguous")
    result = candidates[0]
    _validate_result_binding(result, trial, experiment, scenario, binding, completed=True)
    _validate_saved_result_evidence(store, result, scenario)
    if result.content_digest != trial.result_digest:
        raise ValueError("completed trial result digest is stale")
    return result


def _validate_result_binding(
    result: EvalResult,
    trial: EvalTrial,
    experiment: EvalExperiment,
    scenario: EvalScenario,
    binding: EvalStrategyBinding,
    *,
    completed: bool = False,
) -> None:
    expected_trial_digest = trial.content_digest
    if completed:
        running = EvalTrial.model_validate(
            {
                **trial.model_dump(mode="python"),
                "state": "running",
                "generation": trial.generation - 1,
                "finished_at": None,
                "result_id": None,
                "result_digest": None,
                "content_digest": None,
            },
            strict=True,
        )
        expected_trial_digest = running.content_digest
    if (
        result.id != _result_id(trial)
        or result.run_id != experiment.id
        or result.trial_id != trial.id
        or result.trial_digest != expected_trial_digest
        or result.experiment_digest != experiment.content_digest
        or result.scenario_digest != scenario.content_digest
        or result.strategy_id != binding.strategy.id
        or result.strategy_digest != binding.strategy_digest
        or result.trial_index != trial.trial_index
        or result.fleet_run_id != trial.fleet_run_id
    ):
        raise ValueError("evaluation result belongs to another planned trial")


def _validate_saved_result_evidence(
    store: SQLiteStore, result: EvalResult, scenario: EvalScenario
) -> None:
    """Keep historical metrics fail-closed if their authoritative records disappear."""

    if result.graph_run_digest is None:
        if (
            result.worker_result_digests
            or result.verification_result_digests
            or result.parent_evaluation_digest is not None
            or result.acceptance_ledger_digest is not None
            or result.candidate_artifact_digest is not None
            or result.accepted_graph_revision_digest is not None
            or result.harness_digest is not None
            or result.effective_policy_digest is not None
        ):
            raise ValueError("evaluation result evidence lacks its graph run")
        return
    run = store.get("graph_run_v2", result.fleet_run_id, GraphRunRecord)
    if (
        run.id != result.fleet_run_id
        or canonical_digest(run) != result.graph_run_digest
        or run.accepted_graph_revision_digest != result.accepted_graph_revision_digest
        or run.harness_digest != result.harness_digest
        or run.effective_policy_digest != result.effective_policy_digest
        or run.harness_digest != scenario.harness_digest
    ):
        raise ValueError("evaluation result graph run is stale")
    if result.verification_result_digests:
        _verification_seconds(store, run.id, result.verification_result_digests)
    attempts, all_workers, worker_evidence_complete = _all_authority_bound_worker_results(
        store, run
    )
    if (
        tuple(_required(item.content_digest) for item in all_workers)
        != result.worker_result_digests
    ):
        raise ValueError("saved evaluation worker authority is stale")
    expected_worker_seconds = (
        sum(item.duration_seconds for item in all_workers)
        if all_workers and worker_evidence_complete
        else None
    )
    expected_input_tokens = (
        _complete_usage(all_workers, "input_tokens", integer=True)
        if worker_evidence_complete
        else None
    )
    expected_output_tokens = (
        _complete_usage(all_workers, "output_tokens", integer=True)
        if worker_evidence_complete
        else None
    )
    expected_cost = (
        _complete_usage(all_workers, "cost", integer=False) if worker_evidence_complete else None
    )
    if (
        result.attempts != attempts
        or result.worker_seconds != expected_worker_seconds
        or result.input_tokens != expected_input_tokens
        or result.output_tokens != expected_output_tokens
        or result.cost != expected_cost
    ):
        raise ValueError("saved evaluation worker metrics are stale")
    if not result.verified:
        return
    if (
        result.parent_evaluation_digest is None
        or result.acceptance_ledger_digest is None
        or result.candidate_artifact_digest is None
    ):
        raise ValueError("verified evaluation result is missing evidence")
    evaluations = tuple(
        item
        for item in store.list_records(
            "parent_candidate_evaluation_v2",
            ParentCandidateEvaluationRecord,
            run_id=run.id,
        )
        if item.content_digest == result.parent_evaluation_digest
    )
    if len(evaluations) != 1 or evaluations[0].run_id != run.id:
        raise ValueError("verified parent evaluation is missing or ambiguous")
    evaluation = evaluations[0]
    acceptances = tuple(
        item
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
        )
        if item.accepted_revision.content_digest == result.accepted_graph_revision_digest
    )
    if len(acceptances) != 1 or acceptances[0].run_id != run.id:
        raise ValueError("verified accepted graph is missing or ambiguous")
    ledgers = tuple(
        item
        for item in store.list_records("acceptance_ledger_v2", AcceptanceLedger, run_id=run.id)
        if item.content_digest == result.acceptance_ledger_digest
    )
    if len(ledgers) != 1 or ledgers[0].run_id != run.id:
        raise ValueError("verified AcceptanceLedger is missing or ambiguous")
    candidates = tuple(
        item
        for item in store.list_records("artifact_descriptor_v2", ArtifactDescriptor, run_id=run.id)
        if item.artifact_digest == result.candidate_artifact_digest
        and item.logical_kind == "workspace_patch"
    )
    if len(candidates) != 1 or candidates[0].run_id != run.id:
        raise ValueError("verified candidate artifact is missing or ambiguous")
    replay = validate_exact_parent_evidence_store(
        store,
        run,
        acceptances[0].accepted_revision,
        evaluation,
        scenario.harness,
    )
    current_nodes = _current_nodes(store, run, acceptances[0])
    _bound_worker_results(store, run, current_nodes)
    if (
        _required(replay.acceptance_ledger.content_digest) != result.acceptance_ledger_digest
        or tuple(_required(item.content_digest) for item in all_workers)
        != result.worker_result_digests
        or evaluation.verification_result_digests != result.verification_result_digests
        or evaluation.candidate_artifact_digest != result.candidate_artifact_digest
    ):
        raise ValueError("saved verified evaluation authority is stale")


def _validate_trial_plan(trial: EvalTrial, template: EvalTrial) -> None:
    comparable = (
        "id",
        "run_id",
        "experiment_digest",
        "scenario_digest",
        "strategy_id",
        "strategy_digest",
        "strategy_set",
        "trial_index",
        "fleet_run_id",
        "repository",
        "base_commit",
    )
    if any(getattr(trial, name) != getattr(template, name) for name in comparable):
        raise ValueError("persisted trial conflicts with the deterministic experiment plan")


def _validate_environment(
    snapshot: EvalEnvironmentSnapshot,
    scenario: EvalScenario,
    experiment: EvalExperiment,
) -> None:
    if (
        snapshot.repository != scenario.repository
        or snapshot.head_commit != scenario.base_commit
        or snapshot.clean_status_digest != scenario.clean_status_digest
        or snapshot.harness_digest != scenario.harness_digest
        or snapshot.operator_config_digest != experiment.operator_config_digest
    ):
        raise ValueError("evaluation fixture, Harness, or operator configuration changed")


def _current_nodes(
    store: SQLiteStore, run: GraphRunRecord, acceptance: TaskGraphAcceptance
) -> tuple[NodeExecutionRecord, ...]:
    expected = {item.id for item in acceptance.accepted_revision.graph.nodes}
    latest: dict[str, NodeExecutionRecord] = {}
    for item in store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run.id):
        if item.accepted_graph_revision_digest != run.accepted_graph_revision_digest:
            continue
        prior = latest.get(item.node_id)
        if prior is None or (item.generation, item.attempt, item.sequence) > (
            prior.generation,
            prior.attempt,
            prior.sequence,
        ):
            latest[item.node_id] = item
        elif (item.generation, item.attempt, item.sequence) == (
            prior.generation,
            prior.attempt,
            prior.sequence,
        ) and item.content_digest != prior.content_digest:
            raise ValueError("current graph node evidence is ambiguous")
    if set(latest) != expected:
        raise ValueError("current graph node evidence is incomplete or foreign")
    return tuple(latest[node_id] for node_id in sorted(latest))


def _bound_worker_results(
    store: SQLiteStore, run: GraphRunRecord, nodes: tuple[NodeExecutionRecord, ...]
) -> tuple[WorkerResult, ...]:
    results: list[WorkerResult] = []
    seen: set[str] = set()
    for node in nodes:
        if node.worker_result_id is None or node.worker_result_digest is None:
            raise ValueError("current node lacks its exact worker result")
        if node.worker_result_id in seen:
            raise ValueError("worker result is ambiguously shared across nodes")
        seen.add(node.worker_result_id)
        try:
            result = store.get("worker_result_v2", node.worker_result_id, WorkerResult)
        except KeyError:
            raise ValueError("node worker result is missing or ambiguous") from None
        if result.content_digest != node.worker_result_digest:
            raise ValueError("node worker result is stale or foreign")
        results.append(result)
    return tuple(results)


def _all_authority_bound_worker_results(
    store: SQLiteStore, run: GraphRunRecord
) -> tuple[int, tuple[WorkerResult, ...], bool]:
    """Return all consumed node-attempt results, including retries and old revisions."""

    acceptances = tuple(
        sorted(
            store.list_records("task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id),
            key=lambda item: item.accepted_revision.revision_number,
        )
    )
    if tuple(item.accepted_revision.revision_number for item in acceptances) != tuple(
        range(1, len(acceptances) + 1)
    ):
        raise ValueError("accepted graph revision history is missing or ambiguous")
    for index, acceptance in enumerate(acceptances):
        if index and (
            acceptance.previous_revision_digest
            != acceptances[index - 1].accepted_revision.content_digest
        ):
            raise ValueError("accepted graph revision ancestry is stale")
    if (
        not acceptances
        or acceptances[-1].accepted_revision.content_digest != run.accepted_graph_revision_digest
    ):
        raise ValueError("current accepted graph revision is missing")

    acceptance_by_digest: dict[Digest, TaskGraphAcceptance] = {}
    for acceptance in acceptances:
        digest = _required(acceptance.accepted_revision.content_digest)
        if digest in acceptance_by_digest:
            raise ValueError("accepted graph revision is ambiguous")
        if (
            acceptance.run_id != run.id
            or acceptance.harness_digest != run.harness_digest
            or acceptance.effective_policy_digest != run.effective_policy_digest
        ):
            raise ValueError("accepted graph revision is foreign to its Fleet run")
        acceptance_by_digest[digest] = acceptance
    attempt_records: dict[tuple[Digest, str, int, int], list[NodeExecutionRecord]] = {}
    for record in store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run.id):
        bound_acceptance = acceptance_by_digest.get(record.accepted_graph_revision_digest)
        if (
            record.run_id != run.id
            or bound_acceptance is None
            or record.node_id
            not in {item.id for item in bound_acceptance.accepted_revision.graph.nodes}
        ):
            raise ValueError("node execution is not bound to an accepted graph revision")
        key = (
            record.accepted_graph_revision_digest,
            record.node_id,
            record.generation,
            record.attempt,
        )
        attempt_records.setdefault(key, []).append(record)

    # Successful scheduler results are stored in the node-specific WorkerRequest scope,
    # not the parent GraphRun scope. Resolve exact IDs across scopes, then validate the
    # deterministic request chain below.
    stored_workers = store.list_records("worker_result_v2", WorkerResult)
    by_id: dict[str, list[WorkerResult]] = {}
    for worker in stored_workers:
        by_id.setdefault(worker.id, []).append(worker)
    # Inner WorkCoordinator execution moves the same request identity to the
    # node-specific run scope, so request lookup must also be scope-independent.
    stored_requests = store.list_records("worker_request_v2", WorkerRequest)
    requests_by_digest: dict[Digest, list[WorkerRequest]] = {}
    for request in stored_requests:
        requests_by_digest.setdefault(_required(request.content_digest), []).append(request)
    consumed: dict[Digest, WorkerResult] = {}
    worker_evidence_complete = True
    for key in sorted(attempt_records):
        request_digests = {
            record.worker_request_digest
            for record in attempt_records[key]
            if record.worker_request_digest is not None
        }
        if len(request_digests) > 1:
            raise ValueError("node attempt has ambiguous worker request bindings")
        bindings: set[tuple[str, Digest]] = set()
        for record in attempt_records[key]:
            worker_id = record.worker_result_id
            worker_digest = record.worker_result_digest
            if (worker_id is None) != (worker_digest is None):
                raise ValueError("node attempt has an incomplete worker result binding")
            if worker_id is not None and worker_digest is not None:
                bindings.add((worker_id, worker_digest))
        if len(bindings) > 1:
            raise ValueError("node attempt has ambiguous worker result bindings")
        retained = tuple(
            record
            for record in attempt_records[key]
            if record.retained_from_revision_digest is not None
        )
        if retained:
            if len(retained) != len(attempt_records[key]) or len(request_digests) != 1:
                raise ValueError("retained node attempt binding is ambiguous")
            request_digest = next(iter(request_digests))
            if len(bindings) != 1:
                raise ValueError("retained node lacks its immutable worker result")
            retained_worker_id, retained_worker_digest = next(iter(bindings))
            source_digest = retained[0].retained_from_revision_digest
            if source_digest is None or any(
                record.retained_from_revision_digest != source_digest for record in retained
            ):
                raise ValueError("retained node ancestry is stale")
            source_exists = any(
                source_key[0] == source_digest
                and source_key[1] == key[1]
                and any(
                    record.worker_request_digest == request_digest
                    and record.worker_result_id == retained_worker_id
                    and record.worker_result_digest == retained_worker_digest
                    for record in source_records
                )
                for source_key, source_records in attempt_records.items()
            )
            if not source_exists:
                raise ValueError("retained node source attempt is missing")
            continue
        if not request_digests:
            if bindings:
                raise ValueError("node attempt result lacks its worker request binding")
            continue
        request_digest = next(iter(request_digests))
        request_candidates = requests_by_digest.get(request_digest, ())
        if len(request_candidates) != 1:
            raise ValueError("node attempt worker request is missing or ambiguous")
        request = request_candidates[0]
        _, node_id, generation, attempt = key
        worker_run_digest = canonical_digest(
            {
                "graph_run_id": run.id,
                "node_id": node_id,
                "generation": generation,
                "attempt": attempt,
            }
        )
        expected_worker_run_id = f"node-{worker_run_digest[:32]}"
        if (
            request.graph_run_id != run.id
            or request.run_id != expected_worker_run_id
            or request.node_id != node_id
            or request.accepted_graph_revision_digest != key[0]
            or request.generation != generation
            or request.attempt != attempt
            or request.harness_digest != run.harness_digest
            or request.effective_policy_digest != run.effective_policy_digest
        ):
            raise ValueError("node attempt worker request is stale or foreign")
        if not bindings:
            worker_evidence_complete = False
            continue
        worker_id, digest = next(iter(bindings))
        candidates = tuple(
            item for item in by_id.get(worker_id, ()) if item.content_digest == digest
        )
        if (
            len(candidates) != 1
            or candidates[0].run_id != request.run_id
            or candidates[0].request_digest != request_digest
        ):
            raise ValueError("node attempt worker result is missing, stale, or ambiguous")
        # Replanned retained nodes point back to an already-consumed immutable result.
        # Digest de-duplication prevents charging the same worker invocation twice.
        consumed.setdefault(digest, candidates[0])
    actual_attempts = sum(
        1
        for records in attempt_records.values()
        if any(
            record.retained_from_revision_digest is None
            and (record.worker_request_digest is not None or record.worker_result_id is not None)
            for record in records
        )
    )
    return (
        actual_attempts,
        tuple(consumed[digest] for digest in sorted(consumed)),
        worker_evidence_complete,
    )


def _complete_usage(
    results: tuple[WorkerResult, ...], key: str, *, integer: bool
) -> int | float | None:
    if not results:
        return None
    values: list[int | float] = []
    for result in results:
        usage = result.usage
        if not isinstance(usage, Mapping):
            return None
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(float(value)) or value < 0 or (integer and not isinstance(value, int)):
            return None
        values.append(value)
    total = sum(values)
    return int(total) if integer else float(total)


def _verification_seconds(store: SQLiteStore, run_id: str, digests: tuple[Digest, ...]) -> float:
    durations: list[float] = []
    process = store.list_records("verification_result_v2", ExecutionResult, run_id=run_id)
    browser = store.list_records("browser_observation_v2", BrowserObservation, run_id=run_id)
    for digest in digests:
        matches = [item for item in (*process, *browser) if item.content_digest == digest]
        if len(matches) != 1 or matches[0].run_id != run_id:
            raise ValueError("verification runtime result is missing, foreign, or ambiguous")
        runtime = matches[0]
        if not isinstance(runtime, (ExecutionResult, BrowserObservation)):
            raise TypeError("verification runtime result has an unsupported type")
        durations.append(runtime.duration_seconds)
    return sum(durations)


def _result(
    experiment: EvalExperiment,
    scenario: EvalScenario,
    trial: EvalTrial,
    run: GraphRunRecord,
    invocation_exit_code: int | None,
    total_seconds: float,
    worker_seconds: float | None,
    verification_seconds: float | None,
    input_tokens: int | float | None,
    output_tokens: int | float | None,
    cost: int | float | None,
    attempts: int,
    approvals: int,
    worker_results: tuple[WorkerResult, ...],
    *,
    failure_code: str,
    finished_at: datetime,
) -> EvalResult:
    return EvalResult(
        id=_result_id(trial),
        run_id=experiment.id,
        created_at=finished_at,
        trial_id=trial.id,
        trial_digest=_required(trial.content_digest),
        experiment_digest=_required(experiment.content_digest),
        scenario_digest=_required(scenario.content_digest),
        strategy_id=trial.strategy_id,
        strategy_digest=trial.strategy_digest,
        trial_index=trial.trial_index,
        fleet_run_id=trial.fleet_run_id,
        graph_run_digest=canonical_digest(run),
        accepted_graph_revision_digest=run.accepted_graph_revision_digest,
        harness_digest=run.harness_digest,
        effective_policy_digest=run.effective_policy_digest,
        worker_result_digests=tuple(_required(item.content_digest) for item in worker_results),
        succeeded=False,
        verified=False,
        invocation_exit_code=invocation_exit_code,
        total_seconds=total_seconds,
        worker_seconds=worker_seconds,
        verification_seconds=verification_seconds,
        input_tokens=None if input_tokens is None else int(input_tokens),
        output_tokens=None if output_tokens is None else int(output_tokens),
        cost=None if cost is None else float(cost),
        attempts=attempts,
        approvals_required=approvals,
        failure_code=failure_code,
    )


def _summaries(
    experiment: EvalExperiment, results: tuple[EvalResult, ...]
) -> tuple[EvalStrategySummary, ...]:
    summaries: list[EvalStrategySummary] = []
    for binding in experiment.strategies:
        selected = tuple(item for item in results if item.strategy_id == binding.strategy.id)
        verified = sum(item.verified for item in selected)
        summaries.append(
            EvalStrategySummary(
                strategy_id=binding.strategy.id,
                planned_trials=experiment.trials_per_strategy,
                completed_trials=len(selected),
                verified_successes=verified,
                verified_success_rate=verified / experiment.trials_per_strategy,
                median_total_seconds=_median(item.total_seconds for item in selected),
                median_worker_seconds=_complete_median(item.worker_seconds for item in selected),
                median_verification_seconds=_complete_median(
                    item.verification_seconds for item in selected
                ),
                total_input_tokens=_complete_sum_int(item.input_tokens for item in selected),
                total_output_tokens=_complete_sum_int(item.output_tokens for item in selected),
                total_cost=_complete_sum_float(item.cost for item in selected),
            )
        )
    return tuple(summaries)


def _median(values: Iterable[float]) -> float | None:
    materialized: tuple[float, ...] = tuple(values)
    return None if not materialized else float(median(materialized))


def _complete_median(values: Iterable[float | None]) -> float | None:
    materialized: tuple[float | None, ...] = tuple(values)
    if not materialized or any(item is None for item in materialized):
        return None
    complete = tuple(item for item in materialized if item is not None)
    return float(median(complete))


def _complete_sum_int(values: Iterable[int | None]) -> int | None:
    materialized: tuple[int | None, ...] = tuple(values)
    if not materialized or any(item is None for item in materialized):
        return None
    return sum(item for item in materialized if item is not None)


def _complete_sum_float(values: Iterable[float | None]) -> float | None:
    materialized: tuple[float | None, ...] = tuple(values)
    if not materialized or any(item is None for item in materialized):
        return None
    return float(sum(item for item in materialized if item is not None))


def _binding(experiment: EvalExperiment, strategy_id: str) -> EvalStrategyBinding:
    matches = tuple(item for item in experiment.strategies if item.strategy.id == strategy_id)
    if len(matches) != 1:
        raise ValueError("trial strategy binding is missing or ambiguous")
    return matches[0]


def _result_id(trial: EvalTrial) -> Identifier:
    return f"eval-result-{canonical_digest({'trial': trial.id})[:28]}"


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("authoritative evaluation record is missing its digest")
    return value
