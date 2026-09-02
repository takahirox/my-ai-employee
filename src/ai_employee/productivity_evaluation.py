"""Strict, deterministic primitives for reproducible productivity evaluations."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from typing import ClassVar, Literal, Protocol, Self

from pydantic import Field, field_validator, model_validator

from .domain.base import Digest, Identifier, StableStrEnum, UtcTimestamp
from .domain.v2 import SchemaModelV2
from .serialization import canonical_digest, canonical_json, loads_model


class TaskClass(StableStrEnum):
    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    TEST = "test"
    DOCUMENTATION = "documentation"
    MAINTENANCE = "maintenance"
    OTHER = "other"


class ArmKind(StableStrEnum):
    DIRECT_AGENT = "direct_agent"
    FLEET = "fleet"
    FLEET_ABLATION = "fleet_ablation"
    OSS_ADAPTER = "oss_adapter"


class CheckDisposition(StableStrEnum):
    PASSED = "passed"
    FAILED = "failed"


class TerminalOutcome(StableStrEnum):
    ACCEPTED = "accepted"
    CHECKS_FAILED = "checks_failed"
    EXECUTION_FAILED = "execution_failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class FailureClassification(StableStrEnum):
    ASSERTION = "assertion"
    PROCESS = "process"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INFRASTRUCTURE = "infrastructure"
    POLICY = "policy"
    PROTOCOL = "protocol"


class CheckOutcome(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_check_outcome"
    check_id: Identifier
    authority: str = Field(min_length=1, max_length=1_000)
    disposition: CheckDisposition
    evidence_digest: Digest


class AcceptanceCriterion(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_acceptance_criterion"
    id: Identifier
    description: str = Field(min_length=1, max_length=4_000)
    authority: str = Field(min_length=1, max_length=1_000)


class RegressionCheck(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_regression_check"
    id: Identifier
    description: str = Field(min_length=1, max_length=4_000)
    authority: str = Field(min_length=1, max_length=1_000)


class SWEBenchProvenance(SchemaModelV2):
    schema_name: ClassVar[str] = "swe_bench_provenance"
    problem_statement: str | None = None
    hints_text: str | None = None
    patch: str | None = None
    test_patch: str | None = None
    version: str | None = None
    created_at: str | None = None
    environment_setup_commit: str | None = None


class TaskIdentity(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_task_identity"
    benchmark: Identifier
    benchmark_version: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=500)
    task_version: str = Field(min_length=1, max_length=200)
    repository: str = Field(min_length=1, max_length=4_096)
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    task_class: TaskClass
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    regression_checks: tuple[RegressionCheck, ...] = Field(min_length=1)
    swe_bench_provenance: SWEBenchProvenance | None = None

    @model_validator(mode="after")
    def _criteria_are_canonical(self) -> Self:
        for name, checks in (
            ("acceptance criteria", self.acceptance_criteria),
            ("regression checks", self.regression_checks),
        ):
            ids = tuple(item.id for item in checks)
            if ids != tuple(sorted(set(ids))):
                raise ValueError(f"{name} must be unique and sorted by id")
        if self.swe_bench_provenance is not None and self.benchmark != "swe-bench":
            raise ValueError("SWE-bench provenance is only valid for SWE-bench tasks")
        return self


class ResourceBudgetManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_resource_budget_manifest"
    wall_seconds: float = Field(gt=0)
    input_tokens: int = Field(gt=0)
    output_tokens: int = Field(gt=0)
    cost_limit: float | None = Field(default=None, ge=0)


class StoppingManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_stopping_manifest"
    maximum_attempts: int = Field(ge=1)
    maximum_retries: int = Field(ge=0)
    maximum_repairs: int = Field(ge=0)
    terminal_conditions: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_conditions(self) -> Self:
        if self.terminal_conditions != tuple(sorted(set(self.terminal_conditions))):
            raise ValueError("terminal conditions must be unique and sorted")
        return self


class PricingManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_pricing_manifest"
    currency: Identifier
    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    subscription_allocation: str = Field(min_length=1, max_length=1_000)


class EnvironmentManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_environment_manifest"
    executable: str = Field(min_length=1, max_length=4_096)
    executable_version: str = Field(min_length=1, max_length=500)
    dependency_lock_digest: Digest
    sandbox_mode: Identifier
    network_mode: Identifier
    cache_policy: Identifier
    machine_digest: Digest


class FairnessConfigManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_fairness_config_manifest"
    prompt_digest: Digest
    context_digest: Digest
    model_provider: Identifier
    model_name: str = Field(min_length=1, max_length=500)
    model_version: str = Field(min_length=1, max_length=500)
    reasoning_effort: Identifier
    tools: tuple[Identifier, ...]
    budgets: ResourceBudgetManifest
    stopping: StoppingManifest
    pricing: PricingManifest
    randomized_order: tuple[Identifier, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def _canonical_controls(self) -> Self:
        if self.tools != tuple(sorted(set(self.tools))):
            raise ValueError("tools must be unique and sorted")
        if len(self.randomized_order) != len(set(self.randomized_order)):
            raise ValueError("randomized order must contain unique arm ids")
        return self


class ArmConfigManifest(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_arm_config_manifest"
    planning: bool
    review: bool
    repair: bool
    maximum_parallelism: int = Field(ge=1)


class ArmIdentity(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_arm_identity"
    id: Identifier
    kind: ArmKind
    adapter: Identifier
    worker: Identifier
    environment: EnvironmentManifest
    fairness_config: FairnessConfigManifest
    arm_config: ArmConfigManifest
    environment_digest: Digest
    fairness_config_digest: Digest
    arm_config_digest: Digest
    seed: int = Field(ge=0)
    repetition: int = Field(ge=1)
    assignment_index: int = Field(ge=0)
    disabled_components: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _provenance_is_canonical(self) -> Self:
        if self.disabled_components != tuple(sorted(set(self.disabled_components))):
            raise ValueError("disabled components must be unique and sorted")
        if (self.kind is ArmKind.FLEET_ABLATION) != (len(self.disabled_components) == 1):
            raise ValueError("Fleet ablation arms must disable exactly one component")
        if self.assignment_index >= len(self.fairness_config.randomized_order) or (
            self.fairness_config.randomized_order[self.assignment_index] != self.id
        ):
            raise ValueError("assignment index must bind this arm to the randomized order")
        config = self.arm_config
        if self.kind is ArmKind.DIRECT_AGENT and (
            config.planning or config.review or config.repair or config.maximum_parallelism != 1
        ):
            raise ValueError("direct arms must disable every Fleet orchestration component")
        if self.kind is ArmKind.FLEET and not (config.planning and config.review and config.repair):
            raise ValueError("complete Fleet arms must enable planning, review, and repair")
        if self.kind is ArmKind.FLEET_ABLATION:
            component = self.disabled_components[0]
            expected = {
                "planning": not config.planning and config.review and config.repair,
                "review": config.planning and not config.review and config.repair,
                "repair": config.planning and config.review and not config.repair,
                "parallelism": (
                    config.planning
                    and config.review
                    and config.repair
                    and config.maximum_parallelism == 1
                ),
            }.get(component)
            if expected is not True:
                raise ValueError("ablation arm configuration does not disable its named component")
        bindings = (
            ("environment", self.environment_digest, canonical_digest(self.environment)),
            (
                "fairness config",
                self.fairness_config_digest,
                canonical_digest(self.fairness_config),
            ),
            ("arm config", self.arm_config_digest, canonical_digest(self.arm_config)),
        )
        for name, supplied, actual in bindings:
            if supplied != actual:
                raise ValueError(f"{name} digest does not bind its retained manifest")
        return self


class TrialMetrics(SchemaModelV2):
    """Metric families remain distinct instead of collapsing into one score."""

    schema_name: ClassVar[str] = "productivity_trial_metrics"
    human_active_seconds: float = Field(ge=0)
    human_interventions: int = Field(ge=0)
    time_to_accepted_seconds: float | None = Field(default=None, ge=0)
    wall_seconds: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    api_cost: float | None = Field(default=None, ge=0)
    compute_seconds: float | None = Field(default=None, ge=0)
    compute_cost: float | None = Field(default=None, ge=0)
    retries: int = Field(ge=0)
    repairs: int = Field(ge=0)
    replans: int = Field(ge=0)
    escalations: int = Field(ge=0)
    recoveries: int = Field(ge=0)
    decomposed_nodes: int = Field(ge=0)
    dependency_edges: int = Field(ge=0)
    maximum_parallelism: int = Field(ge=1)
    critical_path_seconds: float = Field(ge=0)
    context_input_tokens: int = Field(ge=0)
    context_output_tokens: int = Field(ge=0)
    unnecessary_work_items: int = Field(ge=0)
    unnecessary_work_seconds: float = Field(ge=0)

    @field_validator(
        "human_active_seconds",
        "time_to_accepted_seconds",
        "wall_seconds",
        "api_cost",
        "compute_seconds",
        "compute_cost",
        "critical_path_seconds",
        "unnecessary_work_seconds",
    )
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("trial metrics must be finite")
        return value

    @model_validator(mode="after")
    def _durations_are_coherent(self) -> Self:
        if (
            self.time_to_accepted_seconds is not None
            and self.time_to_accepted_seconds > self.wall_seconds
        ):
            raise ValueError("time to accepted cannot exceed wall time")
        if self.critical_path_seconds > self.wall_seconds:
            raise ValueError("critical path cannot exceed wall time")
        return self


class TrialResult(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_trial_result"
    id: Identifier
    task: TaskIdentity
    arm: ArmIdentity
    acceptance_outcomes: tuple[CheckOutcome, ...]
    regression_outcomes: tuple[CheckOutcome, ...]
    terminal_outcome: TerminalOutcome
    failure_classification: FailureClassification | None = None
    process_exit_code: int | None = None
    metrics: TrialMetrics

    @model_validator(mode="after")
    def _authoritative_evidence_is_complete(self) -> Self:
        if self.id != trial_id(self.task, self.arm):
            raise ValueError("trial id does not match task and arm provenance")
        self._validate_outcomes(
            self.task.acceptance_criteria, self.acceptance_outcomes, "acceptance"
        )
        self._validate_outcomes(self.task.regression_checks, self.regression_outcomes, "regression")
        if self.accepted != (self.metrics.time_to_accepted_seconds is not None):
            raise ValueError("time-to-accepted is present exactly for accepted trials")
        if self.accepted:
            if self.terminal_outcome is not TerminalOutcome.ACCEPTED:
                raise ValueError("passing checks require an accepted terminal outcome")
            if self.process_exit_code != 0 or self.failure_classification is not None:
                raise ValueError("accepted trials require exit zero and no failure classification")
        elif self.terminal_outcome is TerminalOutcome.ACCEPTED:
            raise ValueError("accepted terminal outcome requires every declared check to pass")
        if self.terminal_outcome is TerminalOutcome.CHECKS_FAILED and (
            self.process_exit_code != 0
            or self.failure_classification is not FailureClassification.ASSERTION
        ):
            raise ValueError("check failure requires exit zero and assertion classification")
        if self.terminal_outcome is TerminalOutcome.EXECUTION_FAILED and (
            self.process_exit_code in (None, 0)
            or self.failure_classification
            not in {
                FailureClassification.PROCESS,
                FailureClassification.INFRASTRUCTURE,
                FailureClassification.POLICY,
                FailureClassification.PROTOCOL,
            }
        ):
            raise ValueError("execution failure requires a nonzero exit and classification")
        expected = {
            TerminalOutcome.TIMED_OUT: FailureClassification.TIMEOUT,
            TerminalOutcome.CANCELLED: FailureClassification.CANCELLED,
        }.get(self.terminal_outcome)
        if expected is not None and self.failure_classification is not expected:
            raise ValueError("terminal outcome and failure classification are incoherent")
        budgets = self.arm.fairness_config.budgets
        stopping = self.arm.fairness_config.stopping
        if self.metrics.wall_seconds > budgets.wall_seconds:
            raise ValueError("observed wall time exceeds the retained budget")
        if self.metrics.input_tokens is None:
            raise ValueError("input-token evidence is required for a capped trial")
        if self.metrics.input_tokens > budgets.input_tokens:
            raise ValueError("observed input tokens exceed the retained budget")
        if self.metrics.output_tokens is None:
            raise ValueError("output-token evidence is required for a capped trial")
        if self.metrics.output_tokens > budgets.output_tokens:
            raise ValueError("observed output tokens exceed the retained budget")
        if budgets.cost_limit is not None:
            costs = (self.metrics.api_cost, self.metrics.compute_cost)
            if any(value is None for value in costs):
                raise ValueError("complete cost evidence is required for a cost-capped trial")
            observed_cost = sum(value for value in costs if value is not None)
            if observed_cost > budgets.cost_limit:
                raise ValueError("observed cost exceeds the retained budget")
        if 1 + self.metrics.retries > stopping.maximum_attempts:
            raise ValueError("observed attempts exceed the retained stopping rule")
        if self.metrics.retries > stopping.maximum_retries:
            raise ValueError("observed retries exceed the retained stopping rule")
        if self.metrics.repairs > stopping.maximum_repairs:
            raise ValueError("observed repairs exceed the retained stopping rule")
        if self.metrics.maximum_parallelism > self.arm.arm_config.maximum_parallelism:
            raise ValueError("observed parallelism exceeds the retained arm configuration")
        return self

    @staticmethod
    def _validate_outcomes(
        definitions: tuple[AcceptanceCriterion, ...] | tuple[RegressionCheck, ...],
        outcomes: tuple[CheckOutcome, ...],
        family: str,
    ) -> None:
        if tuple(item.check_id for item in outcomes) != tuple(
            sorted(item.check_id for item in outcomes)
        ):
            raise ValueError(f"{family} outcomes must be in canonical check-id order")
        declared = {item.id: item.authority for item in definitions}
        actual = {item.check_id: item.authority for item in outcomes}
        if len(actual) != len(outcomes) or actual != declared:
            raise ValueError(
                f"{family} outcomes must exactly cover declared checks and authorities"
            )

    @property
    def authoritative_success(self) -> bool:
        return all(item.disposition is CheckDisposition.PASSED for item in self.acceptance_outcomes)

    @property
    def regression_free(self) -> bool:
        return all(item.disposition is CheckDisposition.PASSED for item in self.regression_outcomes)

    @property
    def accepted(self) -> bool:
        return self.authoritative_success and self.regression_free


def trial_id(task: TaskIdentity, arm: ArmIdentity) -> Identifier:
    return f"productivity-trial-{canonical_digest({'task': task, 'arm': arm})[:32]}"


_COMPARABLE_FIELDS = (
    "worker",
    "environment",
    "fairness_config",
    "seed",
    "repetition",
)


def _validate_same_capability(left: TrialResult, right: TrialResult) -> None:
    if canonical_digest(left.task) != canonical_digest(right.task):
        raise ValueError("paired trials must use the exact same task baseline")
    if any(getattr(left.arm, name) != getattr(right.arm, name) for name in _COMPARABLE_FIELDS):
        raise ValueError("paired trials do not have the same capability and controls")


def validate_paired_comparability(direct: TrialResult, fleet: TrialResult) -> None:
    if direct.arm.kind is not ArmKind.DIRECT_AGENT or fleet.arm.kind is not ArmKind.FLEET:
        raise ValueError("comparison must be direct-agent versus complete Fleet")
    _validate_same_capability(direct, fleet)


class Statistics(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_statistics"
    count: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0, le=1)
    mean: float | None = None
    sample_variance: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _shape_matches_count(self) -> Self:
        values = (self.rate, self.mean, self.sample_variance)
        if self.count == 0 and any(value is not None for value in values):
            raise ValueError("empty statistics cannot contain derived values")
        if self.count > 0 and self.mean is None:
            raise ValueError("non-empty statistics require a mean")
        if (self.count >= 2) != (self.sample_variance is not None):
            raise ValueError("sample variance requires at least two observations")
        if any(value is not None and not math.isfinite(value) for value in values):
            raise ValueError("statistics must be finite")
        return self


class MetricAggregate(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_metric_aggregate"
    metric: Identifier
    statistics: Statistics


class ArmAggregate(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_arm_aggregate"
    arm_id: Identifier
    trials: int = Field(ge=1)
    metrics: tuple[MetricAggregate, ...] = Field(min_length=1)

    def metric(self, name: str) -> Statistics:
        matches = tuple(item.statistics for item in self.metrics if item.metric == name)
        if len(matches) != 1:
            raise KeyError(name)
        return matches[0]


def _values(result: TrialResult) -> dict[str, float | None]:
    values = {
        name: None if value is None else float(value)
        for name, value in result.metrics.model_dump(
            mode="python", exclude={"schema_version"}
        ).items()
    }
    values.update(
        accepted=float(result.accepted),
        authoritative_success=float(result.authoritative_success),
        regression_free=float(result.regression_free),
        terminal_failure=float(not result.accepted),
    )
    values.update(
        {
            f"outcome_{item.value}": float(result.terminal_outcome is item)
            for item in TerminalOutcome
        }
    )
    values.update(
        {
            f"failure_{item.value}": float(result.failure_classification is item)
            for item in FailureClassification
        }
    )
    return values


def _statistics(values: Iterable[float], *, rate: bool = False) -> Statistics:
    ordered = tuple(sorted(values))
    if not ordered:
        return Statistics(count=0)
    mean = math.fsum(ordered) / len(ordered)
    variance = None
    if len(ordered) > 1:
        variance = math.fsum((item - mean) ** 2 for item in ordered) / (len(ordered) - 1)
    return Statistics(
        count=len(ordered), rate=mean if rate else None, mean=mean, sample_variance=variance
    )


def _aggregates(value_sets: Mapping[str, Iterable[float]]) -> tuple[MetricAggregate, ...]:
    rates = {
        "accepted",
        "authoritative_success",
        "regression_free",
        "terminal_failure",
    }
    return tuple(
        MetricAggregate(
            metric=name,
            statistics=_statistics(
                value_sets[name], rate=name in rates or name.startswith(("outcome_", "failure_"))
            ),
        )
        for name in sorted(value_sets)
    )


def aggregate_trials(results: Iterable[TrialResult]) -> tuple[ArmAggregate, ...]:
    grouped: dict[str, list[TrialResult]] = {}
    seen: set[tuple[str, str, int, int]] = set()
    families: dict[str, str] = {}
    for result in results:
        key = (canonical_digest(result.task), result.arm.id, result.arm.seed, result.arm.repetition)
        if key in seen:
            raise ValueError("duplicate task/arm/seed/repetition trial")
        seen.add(key)
        family = canonical_digest(
            result.arm.model_dump(mode="python", exclude={"seed", "repetition"})
        )
        if result.arm.id in families and families[result.arm.id] != family:
            raise ValueError("logical arm provenance changed across repetitions")
        families[result.arm.id] = family
        grouped.setdefault(result.arm.id, []).append(result)
    output: list[ArmAggregate] = []
    for arm_id in sorted(grouped):
        trials = grouped[arm_id]
        samples = {
            name: tuple(value for trial in trials if (value := _values(trial)[name]) is not None)
            for name in sorted(_values(trials[0]))
        }
        output.append(ArmAggregate(arm_id=arm_id, trials=len(trials), metrics=_aggregates(samples)))
    return tuple(output)


class PairedComparison(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_paired_comparison"
    direct_arm_id: Identifier
    fleet_arm_id: Identifier
    pairs: int = Field(ge=1)
    fleet_minus_direct: tuple[MetricAggregate, ...]
    task_classes_where_fleet_hurts: tuple[TaskClass, ...]


def _one_arm(results: tuple[TrialResult, ...]) -> Identifier:
    ids = {item.arm.id for item in results}
    if len(ids) != 1:
        raise ValueError("input must contain exactly one logical arm")
    return next(iter(ids))


def _index(results: tuple[TrialResult, ...]) -> dict[tuple[str, int, int], TrialResult]:
    output: dict[tuple[str, int, int], TrialResult] = {}
    for result in results:
        key = (canonical_digest(result.task), result.arm.seed, result.arm.repetition)
        if key in output:
            raise ValueError("duplicate pair member")
        output[key] = result
    return output


def _paired_deltas(
    pairs: tuple[tuple[TrialResult, TrialResult], ...],
) -> tuple[MetricAggregate, ...]:
    samples: dict[str, list[float]] = {name: [] for name in sorted(_values(pairs[0][0]))}
    for left, right in pairs:
        for name in samples:
            left_value, right_value = _values(left)[name], _values(right)[name]
            if left_value is not None and right_value is not None:
                samples[name].append(right_value - left_value)
    return tuple(
        MetricAggregate(metric=name, statistics=_statistics(samples[name]))
        for name in sorted(samples)
    )


def compare_direct_to_fleet(
    direct_results: Iterable[TrialResult], fleet_results: Iterable[TrialResult]
) -> PairedComparison:
    direct, fleet = tuple(direct_results), tuple(fleet_results)
    if not direct or not fleet:
        raise ValueError("paired comparison requires both arms")
    left, right = _index(direct), _index(fleet)
    if set(left) != set(right):
        raise ValueError("paired comparison has missing or extra trials")
    pairs = tuple((left[key], right[key]) for key in sorted(left))
    for direct_trial, fleet_trial in pairs:
        validate_paired_comparability(direct_trial, fleet_trial)
    hurts: list[TaskClass] = []
    for task_class in sorted({pair[0].task.task_class for pair in pairs}):
        selected = tuple(pair for pair in pairs if pair[0].task.task_class is task_class)
        direct_rate = math.fsum(float(pair[0].accepted) for pair in selected) / len(selected)
        fleet_rate = math.fsum(float(pair[1].accepted) for pair in selected) / len(selected)
        direct_human = math.fsum(pair[0].metrics.human_active_seconds for pair in selected)
        fleet_human = math.fsum(pair[1].metrics.human_active_seconds for pair in selected)
        if fleet_rate < direct_rate or (fleet_rate == direct_rate and fleet_human > direct_human):
            hurts.append(task_class)
    return PairedComparison(
        direct_arm_id=_one_arm(direct),
        fleet_arm_id=_one_arm(fleet),
        pairs=len(pairs),
        fleet_minus_direct=_paired_deltas(pairs),
        task_classes_where_fleet_hurts=tuple(hurts),
    )


class AblationContribution(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_ablation_contribution"
    component: Identifier
    pairs: int = Field(ge=1)
    full_minus_ablation: tuple[MetricAggregate, ...]


def component_ablation_contribution(
    full_results: Iterable[TrialResult], ablation_results: Iterable[TrialResult]
) -> AblationContribution:
    full, ablated = tuple(full_results), tuple(ablation_results)
    if not full or not ablated:
        raise ValueError("ablation comparison requires both arms")
    components = {item.arm.disabled_components for item in ablated}
    if len(components) != 1 or len(next(iter(components))) != 1:
        raise ValueError("ablation must disable exactly one consistent component")
    left, right = _index(ablated), _index(full)
    if set(left) != set(right):
        raise ValueError("ablation comparison has missing or extra trials")
    pairs = tuple((left[key], right[key]) for key in sorted(left))
    for ablation, complete in pairs:
        if (
            ablation.arm.kind is not ArmKind.FLEET_ABLATION
            or complete.arm.kind is not ArmKind.FLEET
        ):
            raise ValueError("ablation comparison requires ablated and complete Fleet arms")
        _validate_same_capability(ablation, complete)
        left_config = ablation.arm.arm_config.model_dump(mode="python")
        right_config = complete.arm.arm_config.model_dump(mode="python")
        changed = tuple(
            sorted(name for name in left_config if left_config[name] != right_config[name])
        )
        component_field = {
            "planning": "planning",
            "review": "review",
            "repair": "repair",
            "parallelism": "maximum_parallelism",
        }.get(ablation.arm.disabled_components[0])
        if component_field is None or changed != (component_field,):
            raise ValueError("ablation must prove an exact one-component-only configuration delta")
    return AblationContribution(
        component=next(iter(components))[0],
        pairs=len(pairs),
        full_minus_ablation=_paired_deltas(pairs),
    )


class ResultBundle(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_result_bundle"
    format: Literal["fleet-productivity-results/2"] = "fleet-productivity-results/2"
    id: Identifier
    run_id: Identifier
    created_at: UtcTimestamp
    benchmark: Identifier
    benchmark_version: str = Field(min_length=1, max_length=200)
    results: tuple[TrialResult, ...] = Field(min_length=1)
    bundle_digest: Digest | None = None

    @model_validator(mode="after")
    def _bind_digest(self) -> Self:
        if self.id != self.run_id:
            raise ValueError("bundle id and run id must match")
        if len({item.id for item in self.results}) != len(self.results):
            raise ValueError("bundle contains duplicate trial ids")
        keys = tuple(
            (canonical_digest(item.task), item.arm.id, item.arm.seed, item.arm.repetition)
            for item in self.results
        )
        if len(keys) != len(set(keys)):
            raise ValueError("bundle contains duplicate logical task/arm/seed/repetition keys")
        if any(
            item.task.benchmark != self.benchmark
            or item.task.benchmark_version != self.benchmark_version
            for item in self.results
        ):
            raise ValueError("bundle contains a foreign task")
        task_families: dict[tuple[str, str], str] = {}
        arm_families: dict[str, str] = {}
        for item in self.results:
            task_key = (item.task.task_id, item.task.task_version)
            task_family = canonical_digest(item.task)
            if task_key in task_families and task_families[task_key] != task_family:
                raise ValueError("logical task provenance changed within bundle")
            task_families[task_key] = task_family
            arm_family = canonical_digest(
                item.arm.model_dump(mode="python", exclude={"seed", "repetition"})
            )
            if item.arm.id in arm_families and arm_families[item.arm.id] != arm_family:
                raise ValueError("logical arm provenance changed within bundle")
            arm_families[item.arm.id] = arm_family
        canonical_order = tuple(
            sorted(
                self.results,
                key=lambda item: (
                    item.task.task_id,
                    item.task.task_version,
                    item.arm.id,
                    item.arm.seed,
                    item.arm.repetition,
                    item.id,
                ),
            )
        )
        if self.results != canonical_order:
            raise ValueError("bundle results must use canonical task/arm/seed/repetition order")
        aggregate_trials(self.results)
        actual = canonical_digest(self.model_dump(mode="python", exclude={"bundle_digest"}))
        if self.bundle_digest is not None and self.bundle_digest != actual:
            raise ValueError("bundle digest does not match canonical content")
        object.__setattr__(self, "bundle_digest", actual)
        return self


def dump_result_bundle(bundle: ResultBundle) -> bytes:
    return (canonical_json(bundle) + "\n").encode()


def load_result_bundle(data: str | bytes) -> ResultBundle:
    raw = data.encode() if isinstance(data, str) else data
    try:
        bundle = loads_model(raw.decode("utf-8"), ResultBundle)
    except UnicodeDecodeError as exc:
        raise ValueError("bundle must be UTF-8 JSON") from exc
    if dump_result_bundle(bundle) != raw:
        raise ValueError("bundle is malformed or not canonical JSON")
    return bundle


class HistoryCompatibility(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_history_compatibility"
    benchmark: Identifier
    previous_version: str = Field(min_length=1)
    current_version: str = Field(min_length=1)
    task_pairs: tuple[tuple[Digest, Digest], ...] = Field(min_length=1)
    arm_pairs: tuple[tuple[Identifier, Identifier], ...] = Field(min_length=1)
    baselines_equivalent: Literal[True]
    criteria_equivalent: Literal[True]
    capabilities_equivalent: Literal[True]

    @model_validator(mode="after")
    def _mappings_are_canonical_bijections(self) -> Self:
        for name, pairs in (("task", self.task_pairs), ("arm", self.arm_pairs)):
            if pairs != tuple(sorted(pairs)):
                raise ValueError(f"{name} compatibility pairs must be canonically sorted")
            left = tuple(item[0] for item in pairs)
            right = tuple(item[1] for item in pairs)
            if len(left) != len(set(left)) or len(right) != len(set(right)):
                raise ValueError(f"{name} compatibility mapping must be a bijection")
        return self


class HistoricalComparison(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_historical_comparison"
    previous_bundle_digest: Digest
    current_bundle_digest: Digest
    compared_trials: int = Field(ge=1)
    regressions: tuple[str, ...]


def compare_history(
    previous: ResultBundle, current: ResultBundle, compatibility: HistoryCompatibility
) -> HistoricalComparison:
    if (
        previous.benchmark != current.benchmark
        or previous.benchmark != compatibility.benchmark
        or previous.benchmark_version != compatibility.previous_version
        or current.benchmark_version != compatibility.current_version
    ):
        raise ValueError("benchmark versions are not explicitly compatible")
    task_map, arm_map = dict(compatibility.task_pairs), dict(compatibility.arm_pairs)
    old_tasks = {canonical_digest(item.task) for item in previous.results}
    new_tasks = {canonical_digest(item.task) for item in current.results}
    if set(task_map) != old_tasks or set(task_map.values()) != new_tasks:
        raise ValueError("task compatibility mapping is incomplete")
    old_arms, new_arms = (
        {item.arm.id for item in previous.results},
        {item.arm.id for item in current.results},
    )
    if set(arm_map) != old_arms or set(arm_map.values()) != new_arms:
        raise ValueError("arm compatibility mapping is incomplete")
    current_index = {
        (canonical_digest(item.task), item.arm.id, item.arm.seed, item.arm.repetition): item
        for item in current.results
    }
    mapped_keys = {
        (
            task_map[canonical_digest(item.task)],
            arm_map[item.arm.id],
            item.arm.seed,
            item.arm.repetition,
        )
        for item in previous.results
    }
    if mapped_keys != set(current_index):
        raise ValueError(
            "historical mapping must produce the exact one-to-one current trial key set"
        )
    regressions: list[str] = []
    compared = 0
    for old in sorted(previous.results, key=lambda item: item.id):
        key = (
            task_map[canonical_digest(old.task)],
            arm_map[old.arm.id],
            old.arm.seed,
            old.arm.repetition,
        )
        new = current_index.get(key)
        if new is None:
            raise ValueError("mapped current trial is missing")
        if (
            old.task.repository != new.task.repository
            or old.task.baseline_commit != new.task.baseline_commit
            or old.task.task_class is not new.task.task_class
            or old.task.acceptance_criteria != new.task.acceptance_criteria
            or old.task.regression_checks != new.task.regression_checks
        ):
            raise ValueError("mapped historical tasks are not demonstrably equivalent")
        if (
            old.arm.kind is not new.arm.kind
            or old.arm.adapter != new.arm.adapter
            or old.arm.worker != new.arm.worker
            or old.arm.environment != new.arm.environment
            or old.arm.arm_config != new.arm.arm_config
            or old.arm.disabled_components != new.arm.disabled_components
        ):
            raise ValueError("mapped historical arms are not capability-equivalent")
        old_fairness = old.arm.fairness_config.model_dump(
            mode="python", exclude={"randomized_order"}
        )
        new_fairness = new.arm.fairness_config.model_dump(
            mode="python", exclude={"randomized_order"}
        )
        mapped_order = tuple(
            arm_map.get(item, item) for item in old.arm.fairness_config.randomized_order
        )
        if old_fairness != new_fairness or mapped_order != new.arm.fairness_config.randomized_order:
            raise ValueError("mapped historical fairness controls are not equivalent")
        compared += 1
        if old.accepted and not new.accepted:
            regressions.append(f"{old.task.task_id}:{old.arm.id}:accepted")
        for metric in ("human_active_seconds", "wall_seconds", "api_cost", "compute_cost"):
            old_value, new_value = _values(old)[metric], _values(new)[metric]
            if (
                metric in {"api_cost", "compute_cost"}
                and old_value is not None
                and new_value is None
            ):
                raise ValueError(f"current historical cost evidence is missing for {metric}")
            if old_value is not None and new_value is not None and new_value > old_value:
                regressions.append(f"{old.task.task_id}:{old.arm.id}:{metric}")
    if compared != len(current.results):
        raise ValueError("historical comparison has unmatched current trials")
    if previous.bundle_digest is None or current.bundle_digest is None:
        raise ValueError("historical bundles must be digested")
    return HistoricalComparison(
        previous_bundle_digest=previous.bundle_digest,
        current_bundle_digest=current.bundle_digest,
        compared_trials=compared,
        regressions=tuple(regressions),
    )


class BenchmarkAdapter(Protocol):
    profile_id: str

    def normalize_case(self, payload: Mapping[str, object]) -> TaskIdentity: ...


class GenericOSSAdapter:
    profile_id = "generic-oss"

    def __init__(self, benchmark: Identifier, benchmark_version: str) -> None:
        self.benchmark, self.benchmark_version = benchmark, benchmark_version

    def normalize_case(self, payload: Mapping[str, object]) -> TaskIdentity:
        data = {**payload, "benchmark": self.benchmark, "benchmark_version": self.benchmark_version}
        return TaskIdentity.model_validate_json(canonical_json(data), strict=True)


class SWEBenchCase(SchemaModelV2):
    schema_name: ClassVar[str] = "swe_bench_case"
    instance_id: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    problem_statement: str | None = None
    hints_text: str | None = None
    patch: str | None = None
    test_patch: str | None = None
    version: str | None = None
    created_at: str | None = None
    environment_setup_commit: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_native_fields(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            raise TypeError("SWE-bench case must be an object")
        data = dict(value)
        for native, normalized in (
            ("FAIL_TO_PASS", "fail_to_pass"),
            ("PASS_TO_PASS", "pass_to_pass"),
        ):
            if native in data and normalized in data:
                raise ValueError(f"conflicting SWE-bench fields: {native} and {normalized}")
            if native in data:
                data[normalized] = data.pop(native)
        for field in ("fail_to_pass", "pass_to_pass"):
            raw = data.get(field, ())
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{field} must be a JSON array") from exc
            if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) for item in raw):
                raise ValueError(f"{field} must be an array of strings")
            data[field] = tuple(raw)
        return data

    @model_validator(mode="after")
    def _tests_are_valid(self) -> Self:
        for name, tests in (
            ("FAIL_TO_PASS", self.fail_to_pass),
            ("PASS_TO_PASS", self.pass_to_pass),
        ):
            if not tests or any(not item for item in tests) or len(tests) != len(set(tests)):
                raise ValueError(f"SWE-bench {name} tests must be non-empty and unique")
        if set(self.fail_to_pass) & set(self.pass_to_pass):
            raise ValueError("SWE-bench acceptance and regression tests must not overlap")
        return self


class SWEBenchAdapter:
    """Offline normalizer only; it makes no network call or performance claim."""

    profile_id = "swe-bench"

    def __init__(self, dataset_version: str) -> None:
        if not dataset_version:
            raise ValueError("dataset version must be non-empty")
        self.dataset_version = dataset_version

    def normalize_case(self, payload: Mapping[str, object]) -> TaskIdentity:
        case = SWEBenchCase.model_validate_json(canonical_json(payload), strict=True)
        criteria = tuple(
            AcceptanceCriterion(
                id=f"fail-to-pass-{index:04d}",
                description=test,
                authority="swe-bench.fail_to_pass",
            )
            for index, test in enumerate(sorted(case.fail_to_pass), start=1)
        )
        regressions = tuple(
            RegressionCheck(
                id=f"pass-to-pass-{index:04d}",
                description=test,
                authority="swe-bench.pass_to_pass",
            )
            for index, test in enumerate(sorted(case.pass_to_pass), start=1)
        )
        return TaskIdentity(
            benchmark="swe-bench",
            benchmark_version=self.dataset_version,
            task_id=case.instance_id,
            task_version=self.dataset_version,
            repository=case.repo,
            baseline_commit=case.base_commit,
            task_class=TaskClass.BUG_FIX,
            acceptance_criteria=criteria,
            regression_checks=regressions,
            swe_bench_provenance=SWEBenchProvenance(
                problem_statement=case.problem_statement,
                hints_text=case.hints_text,
                patch=case.patch,
                test_patch=case.test_patch,
                version=case.version,
                created_at=case.created_at,
                environment_setup_commit=case.environment_setup_commit,
            ),
        )
