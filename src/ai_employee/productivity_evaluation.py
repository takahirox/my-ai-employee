"""Strict, deterministic primitives for reproducible productivity evaluations."""

from __future__ import annotations

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


class AcceptanceCriterion(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_acceptance_criterion"
    id: Identifier
    description: str = Field(min_length=1, max_length=4_000)
    authority: str = Field(min_length=1, max_length=1_000)


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

    @model_validator(mode="after")
    def _criteria_are_canonical(self) -> Self:
        ids = tuple(item.id for item in self.acceptance_criteria)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("acceptance criteria must be unique and sorted by id")
        return self


class ArmIdentity(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_arm_identity"
    id: Identifier
    kind: ArmKind
    adapter: Identifier
    worker: Identifier
    model_provider: Identifier
    model_name: str = Field(min_length=1, max_length=500)
    model_version: str = Field(min_length=1, max_length=500)
    tools: tuple[Identifier, ...]
    environment_digest: Digest
    fairness_config_digest: Digest
    arm_config_digest: Digest
    seed: int = Field(ge=0)
    repetition: int = Field(ge=1)
    disabled_components: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def _provenance_is_canonical(self) -> Self:
        if self.tools != tuple(sorted(set(self.tools))):
            raise ValueError("tools must be unique and sorted")
        if self.disabled_components != tuple(sorted(set(self.disabled_components))):
            raise ValueError("disabled components must be unique and sorted")
        if (self.kind is ArmKind.FLEET_ABLATION) != bool(self.disabled_components):
            raise ValueError("exactly Fleet ablation arms must disable components")
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


class TrialResult(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_trial_result"
    id: Identifier
    task: TaskIdentity
    arm: ArmIdentity
    authoritative_success: bool
    regression_free: bool
    acceptance_evidence_digests: tuple[Digest, ...] = ()
    regression_evidence_digests: tuple[Digest, ...] = ()
    process_exit_code: int | None = None
    metrics: TrialMetrics

    @model_validator(mode="after")
    def _authoritative_evidence_is_complete(self) -> Self:
        if self.id != trial_id(self.task, self.arm):
            raise ValueError("trial id does not match task and arm provenance")
        if len(self.acceptance_evidence_digests) != len(set(self.acceptance_evidence_digests)):
            raise ValueError("acceptance evidence must be unique")
        if len(self.regression_evidence_digests) != len(set(self.regression_evidence_digests)):
            raise ValueError("regression evidence must be unique")
        if self.authoritative_success and len(self.acceptance_evidence_digests) != len(
            self.task.acceptance_criteria
        ):
            raise ValueError("success requires evidence for every acceptance criterion")
        if self.regression_free and not self.regression_evidence_digests:
            raise ValueError("regression freedom requires authoritative evidence")
        if self.accepted != (self.metrics.time_to_accepted_seconds is not None):
            raise ValueError("time-to-accepted is present exactly for accepted trials")
        return self

    @property
    def accepted(self) -> bool:
        return self.authoritative_success and self.regression_free


def trial_id(task: TaskIdentity, arm: ArmIdentity) -> Identifier:
    return f"productivity-trial-{canonical_digest({'task': task, 'arm': arm})[:32]}"


_COMPARABLE_FIELDS = (
    "worker",
    "model_provider",
    "model_name",
    "model_version",
    "tools",
    "environment_digest",
    "fairness_config_digest",
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
        for name, value in result.metrics.model_dump(mode="python").items()
    }
    values.update(
        accepted=float(result.accepted),
        authoritative_success=float(result.authoritative_success),
        regression_free=float(result.regression_free),
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
    rates = {"accepted", "authoritative_success", "regression_free"}
    return tuple(
        MetricAggregate(metric=name, statistics=_statistics(value_sets[name], rate=name in rates))
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
    return AblationContribution(
        component=next(iter(components))[0],
        pairs=len(pairs),
        full_minus_ablation=_paired_deltas(pairs),
    )


class ResultBundle(SchemaModelV2):
    schema_name: ClassVar[str] = "productivity_result_bundle"
    format: Literal["fleet-productivity-results/1"] = "fleet-productivity-results/1"
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
        bindings = tuple(
            (canonical_digest(item.task), canonical_digest(item.arm)) for item in self.results
        )
        if len(bindings) != len(set(bindings)):
            raise ValueError("bundle contains duplicate trials")
        if any(
            item.task.benchmark != self.benchmark
            or item.task.benchmark_version != self.benchmark_version
            for item in self.results
        ):
            raise ValueError("bundle contains a foreign task")
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
        compared += 1
        if old.accepted and not new.accepted:
            regressions.append(f"{old.task.task_id}:{old.arm.id}:accepted")
        for metric in ("human_active_seconds", "wall_seconds", "api_cost", "compute_cost"):
            old_value, new_value = _values(old)[metric], _values(new)[metric]
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

    @model_validator(mode="after")
    def _tests_are_valid(self) -> Self:
        tests = (*self.fail_to_pass, *self.pass_to_pass)
        if not tests or any(not item for item in tests) or len(tests) != len(set(tests)):
            raise ValueError("SWE-bench tests must be non-empty and unique")
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
        criteria = [
            AcceptanceCriterion(
                id=f"{kind}-{index:04d}",
                description=test,
                authority=f"swe-bench.{kind.replace('-', '_')}",
            )
            for kind, tests in (
                ("fail-to-pass", sorted(case.fail_to_pass)),
                ("pass-to-pass", sorted(case.pass_to_pass)),
            )
            for index, test in enumerate(tests, start=1)
        ]
        return TaskIdentity(
            benchmark="swe-bench",
            benchmark_version=self.dataset_version,
            task_id=case.instance_id,
            task_version=self.dataset_version,
            repository=case.repo,
            baseline_commit=case.base_commit,
            task_class=TaskClass.BUG_FIX,
            acceptance_criteria=tuple(criteria),
        )
