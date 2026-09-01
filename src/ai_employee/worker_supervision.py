"""Deterministic worker timeout policy and bounded attempt supervision facts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import SemanticReasoningClass, SemanticScope, SemanticTaskProfile
from .domain.base import Digest, Identifier, UtcTimestamp
from .domain.v2 import DigestedRecordV2

TIMEOUT_RULE_VERSION: Literal["1"] = "1"


class WorkerTimeoutRule(BaseModel):
    """One ordered, operator-configured timeout profile rule."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: Identifier
    version: Literal["1"] = TIMEOUT_RULE_VERSION
    scope: SemanticScope | Literal["any"] = "any"
    reasoning_class: SemanticReasoningClass | Literal["any"] = "any"
    min_scale: int = Field(default=1, ge=1, le=10)
    max_scale: int = Field(default=10, ge=1, le=10)
    recommended_timeout_seconds: float = Field(gt=0)
    minimum_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _valid_rule(self) -> Self:
        if self.min_scale > self.max_scale:
            raise ValueError("timeout rule minimum scale exceeds maximum scale")
        if self.minimum_timeout_seconds > self.recommended_timeout_seconds:
            raise ValueError("timeout rule minimum exceeds recommendation")
        return self

    def matches(
        self,
        scope: SemanticScope,
        reasoning_class: SemanticReasoningClass,
        scale: int,
    ) -> bool:
        return (
            (self.scope == "any" or self.scope is scope)
            and (self.reasoning_class == "any" or self.reasoning_class is reasoning_class)
            and self.min_scale <= scale <= self.max_scale
        )


def default_timeout_rules() -> tuple[WorkerTimeoutRule, ...]:
    """Conservative ordered defaults; operators may replace the entire table."""

    return (
        WorkerTimeoutRule(
            id="deep",
            reasoning_class=SemanticReasoningClass.DEEP,
            recommended_timeout_seconds=1800.0,
            minimum_timeout_seconds=1200.0,
        ),
        WorkerTimeoutRule(
            id="open-ended",
            reasoning_class=SemanticReasoningClass.OPEN_ENDED,
            recommended_timeout_seconds=1800.0,
            minimum_timeout_seconds=1200.0,
        ),
        WorkerTimeoutRule(
            id="broad",
            scope=SemanticScope.BROAD,
            recommended_timeout_seconds=1800.0,
            minimum_timeout_seconds=1200.0,
        ),
        WorkerTimeoutRule(
            id="multi-component-moderate",
            scope=SemanticScope.MULTI_COMPONENT,
            reasoning_class=SemanticReasoningClass.MODERATE,
            recommended_timeout_seconds=1200.0,
            minimum_timeout_seconds=900.0,
        ),
        WorkerTimeoutRule(
            id="large-scale",
            min_scale=8,
            recommended_timeout_seconds=1800.0,
            minimum_timeout_seconds=1200.0,
        ),
        WorkerTimeoutRule(
            id="baseline",
            recommended_timeout_seconds=600.0,
            minimum_timeout_seconds=1.0,
        ),
    )


class WorkerSupervisionPolicy(BaseModel):
    """Versioned operator policy; ordering is part of its persisted digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    rule_version: Literal["1"] = TIMEOUT_RULE_VERSION
    rules: tuple[WorkerTimeoutRule, ...] = Field(default_factory=default_timeout_rules)
    heartbeat_interval_seconds: float = Field(default=30.0, gt=0)
    no_progress_threshold_seconds: float = Field(default=300.0, gt=0)
    max_heartbeat_records: int = Field(default=240, ge=2, le=10_000)

    @model_validator(mode="after")
    def _valid_policy(self) -> Self:
        if not self.rules or len({item.id for item in self.rules}) != len(self.rules):
            raise ValueError("timeout policy requires uniquely identified rules")
        if any(item.version != self.rule_version for item in self.rules):
            raise ValueError("timeout rule version does not match policy version")
        if self.heartbeat_interval_seconds > self.no_progress_threshold_seconds:
            raise ValueError("heartbeat interval cannot exceed no-progress threshold")
        return self

    def select(
        self,
        scope: SemanticScope,
        reasoning_class: SemanticReasoningClass,
        scale: int,
    ) -> WorkerTimeoutRule:
        matches = tuple(
            item for item in self.rules if item.matches(scope, reasoning_class, scale)
        )
        if not matches:
            raise ValueError("operator timeout table has no applicable rule")
        return matches[0]


class WorkerTimeoutProfileRecord(DigestedRecordV2):
    """All exact inputs and outputs of one versioned timeout-policy decision."""

    schema_name = "worker_timeout_profile_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    rule_version: Literal["1"] = TIMEOUT_RULE_VERSION
    operator_config_digest: Digest
    rule_id: Identifier
    scope: SemanticScope
    reasoning_class: SemanticReasoningClass
    scale: int = Field(ge=1, le=10)
    recommended_timeout_seconds: float = Field(gt=0)
    profile_minimum_seconds: float = Field(gt=0)
    accepted_node_timeout_seconds: float = Field(ge=0)
    adapter_timeout_seconds: float = Field(ge=0)
    policy_timeout_seconds: float = Field(ge=0)
    remaining_run_timeout_seconds: float = Field(ge=0)
    effective_timeout_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _strict_minimum_and_recommendation_are_exact(self) -> Self:
        if self.recommended_timeout_seconds < self.profile_minimum_seconds:
            raise ValueError("recommended timeout cannot be below the profile minimum")
        expected = min(
            self.accepted_node_timeout_seconds,
            self.adapter_timeout_seconds,
            self.policy_timeout_seconds,
            self.remaining_run_timeout_seconds,
        )
        if self.effective_timeout_seconds != expected:
            raise ValueError("effective timeout must be the strict authority minimum")
        return self


class WorkerBudgetPreflightRecord(DigestedRecordV2):
    """Fail-closed proof that an inadequate accepted attempt was never started."""

    schema_name = "worker_budget_preflight_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    timeout_profile_digest: Digest
    status: Literal["denied"] = "denied"
    failure_code: Literal["WORKER_BUDGET_INADEQUATE"] = "WORKER_BUDGET_INADEQUATE"
    denied_authorities: tuple[
        Literal["accepted_node", "adapter", "policy", "remaining_run"], ...
    ] = Field(min_length=1)
    attempt_started: Literal[False] = False
    implicit_extension_applied: Literal[False] = False


class WorkerAttemptObservation(BaseModel):
    """A sanitized point-in-time observation; silence is not a stuck-model claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    process_status: Literal[
        "starting", "running", "succeeded", "failed", "cancelled", "indeterminate"
    ]
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    last_mediated_action_digest: Digest | None = None
    last_artifact_digest: Digest | None = None
    last_diff_digest: Digest | None = None

    def progress_key(self) -> tuple[object, ...]:
        return (
            self.process_status,
            self.stdout_bytes,
            self.stderr_bytes,
            self.last_mediated_action_digest,
            self.last_artifact_digest,
            self.last_diff_digest,
        )


class WorkerAttemptHeartbeatRecord(DigestedRecordV2):
    """Bounded worker-attempt supervisor heartbeat, separate from run ownership."""

    schema_name = "worker_attempt_heartbeat_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    sequence: int = Field(ge=0)
    timeout_profile_digest: Digest
    observed_at: UtcTimestamp
    elapsed_seconds: float = Field(ge=0)
    remaining_seconds: float = Field(ge=0)
    process_status: Literal[
        "starting", "running", "succeeded", "failed", "cancelled", "indeterminate"
    ]
    stdout_bytes: int = Field(ge=0)
    stderr_bytes: int = Field(ge=0)
    last_mediated_action_digest: Digest | None = None
    last_artifact_digest: Digest | None = None
    last_diff_digest: Digest | None = None
    last_observable_progress_elapsed_seconds: float = Field(ge=0)
    observable_progress: bool
    liveness_observed: Literal[True] = True
    no_observable_progress: bool
    silence_diagnostic: bool
    model_stuck: Literal[False] = False
    early_cancel_authorized: Literal[False] = False
    hard_timeout_reached: bool


class TimeoutRecoveryRecord(DigestedRecordV2):
    """A timeout recovery decision that cannot silently change execution identity."""

    schema_name = "timeout_recovery_record"
    graph_run_id: Identifier
    node_id: Identifier
    child_run_id: Identifier
    accepted_graph_revision_digest: Digest
    timeout_profile_digest: Digest
    source_generation: int = Field(ge=0)
    source_attempt: int = Field(ge=0)
    action: Literal["same_strategy_retry", "replan_required", "denied"]
    routing_mode: Literal["fixed", "policy", "adaptive"]
    source_strategy_id: Identifier
    source_model: str = Field(min_length=1, max_length=200)
    source_backend: str = Field(min_length=1, max_length=200)
    retry_strategy_id: Identifier | None = None
    retry_model: str | None = Field(default=None, min_length=1, max_length=200)
    retry_backend: str | None = Field(default=None, min_length=1, max_length=200)
    retry_within_policy: bool
    retry_within_counters: bool
    retry_within_resource_budgets: bool
    normal_acceptance_required: bool
    alternate_fallback_authorized: Literal[False] = False

    @model_validator(mode="after")
    def _recovery_never_falls_back(self) -> Self:
        retry = (self.retry_strategy_id, self.retry_model, self.retry_backend)
        source = (self.source_strategy_id, self.source_model, self.source_backend)
        if self.action == "same_strategy_retry":
            if retry != source:
                raise ValueError("timeout retry must preserve strategy, model, and backend")
            if not (
                self.retry_within_policy
                and self.retry_within_counters
                and self.retry_within_resource_budgets
            ):
                raise ValueError("same-strategy retry requires every existing authority")
            if self.normal_acceptance_required:
                raise ValueError("same accepted task retry does not create a new acceptance")
        elif any(item is not None for item in retry):
            raise ValueError("replan or denial cannot carry an alternate fallback")
        if self.action == "replan_required" and not self.normal_acceptance_required:
            raise ValueError("replan/decomposition requires normal acceptance")
        return self


def select_node_timeout(
    *,
    id: Identifier,
    run_id: Identifier,
    created_at: datetime,
    graph_run_id: Identifier,
    node_id: Identifier,
    child_run_id: Identifier,
    accepted_graph_revision_digest: Digest,
    generation: int,
    attempt: int,
    operator_config_digest: Digest,
    rule: WorkerTimeoutRule,
    profile: SemanticTaskProfile | None,
    scale: int,
    accepted_node_timeout_seconds: float,
    adapter_timeout_seconds: float,
    policy_timeout_seconds: float,
    remaining_run_timeout_seconds: float,
) -> WorkerTimeoutProfileRecord:
    scope = SemanticScope.BOUNDED if profile is None else profile.scope
    reasoning = (
        SemanticReasoningClass.MECHANICAL if profile is None else profile.reasoning_class
    )
    if not rule.matches(scope, reasoning, scale):
        raise ValueError("selected timeout rule does not match persisted task inputs")
    effective = min(
        accepted_node_timeout_seconds,
        adapter_timeout_seconds,
        policy_timeout_seconds,
        remaining_run_timeout_seconds,
    )
    return WorkerTimeoutProfileRecord(
        id=id,
        run_id=run_id,
        created_at=created_at,
        graph_run_id=graph_run_id,
        node_id=node_id,
        child_run_id=child_run_id,
        accepted_graph_revision_digest=accepted_graph_revision_digest,
        generation=generation,
        attempt=attempt,
        operator_config_digest=operator_config_digest,
        rule_id=rule.id,
        scope=scope,
        reasoning_class=reasoning,
        scale=scale,
        recommended_timeout_seconds=rule.recommended_timeout_seconds,
        profile_minimum_seconds=rule.minimum_timeout_seconds,
        accepted_node_timeout_seconds=accepted_node_timeout_seconds,
        adapter_timeout_seconds=adapter_timeout_seconds,
        policy_timeout_seconds=policy_timeout_seconds,
        remaining_run_timeout_seconds=remaining_run_timeout_seconds,
        effective_timeout_seconds=effective,
    )


def inadequate_authorities(
    profile: WorkerTimeoutProfileRecord,
) -> tuple[Literal["accepted_node", "adapter", "policy", "remaining_run"], ...]:
    minimum = profile.profile_minimum_seconds
    values = (
        ("accepted_node", profile.accepted_node_timeout_seconds),
        ("adapter", profile.adapter_timeout_seconds),
        ("policy", profile.policy_timeout_seconds),
        ("remaining_run", profile.remaining_run_timeout_seconds),
    )
    return tuple(
        cast(
            Literal["accepted_node", "adapter", "policy", "remaining_run"],
            name,
        )
        for name, value in values
        if value < minimum
    )


class WorkerAttemptSupervisor:
    """State machine for periodic diagnostics; only the exact hard deadline is terminal."""

    def __init__(
        self,
        profile: WorkerTimeoutProfileRecord,
        *,
        heartbeat_interval_seconds: float = 30.0,
        no_progress_threshold_seconds: float = 300.0,
        max_heartbeat_records: int = 240,
    ) -> None:
        if (
            heartbeat_interval_seconds <= 0
            or no_progress_threshold_seconds <= 0
            or max_heartbeat_records < 2
        ):
            raise ValueError("supervisor intervals must be positive")
        self.profile = profile
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.no_progress_threshold_seconds = no_progress_threshold_seconds
        self.max_heartbeat_records = max_heartbeat_records
        self._sequence = 0
        self._last_emitted: float | None = None
        self._last_progress = 0.0
        self._progress_key: tuple[object, ...] | None = None

    def sample(
        self,
        observation: WorkerAttemptObservation,
        *,
        elapsed_seconds: float,
        observed_at: datetime,
        force: bool = False,
    ) -> WorkerAttemptHeartbeatRecord | None:
        if elapsed_seconds < 0:
            raise ValueError("elapsed time cannot be negative")
        progress = self._progress_key is None or observation.progress_key() != self._progress_key
        if progress:
            self._progress_key = observation.progress_key()
            self._last_progress = elapsed_seconds
        hard_timeout = elapsed_seconds >= self.profile.effective_timeout_seconds
        if not force and self._sequence >= self.max_heartbeat_records:
            return None
        if (
            not force
            and not hard_timeout
            and self._last_emitted is not None
            and elapsed_seconds - self._last_emitted < self.heartbeat_interval_seconds
        ):
            return None
        no_progress = elapsed_seconds - self._last_progress >= self.no_progress_threshold_seconds
        remaining = max(0.0, self.profile.effective_timeout_seconds - elapsed_seconds)
        record = WorkerAttemptHeartbeatRecord(
            id=f"heartbeat-{self.profile.node_id}-{self.profile.generation}-"
            f"{self.profile.attempt}-{self._sequence}",
            run_id=self.profile.run_id,
            created_at=observed_at,
            graph_run_id=self.profile.graph_run_id,
            node_id=self.profile.node_id,
            child_run_id=self.profile.child_run_id,
            accepted_graph_revision_digest=self.profile.accepted_graph_revision_digest,
            generation=self.profile.generation,
            attempt=self.profile.attempt,
            sequence=self._sequence,
            timeout_profile_digest=self.profile.content_digest or "",
            observed_at=observed_at,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=remaining,
            process_status=observation.process_status,
            stdout_bytes=observation.stdout_bytes,
            stderr_bytes=observation.stderr_bytes,
            last_mediated_action_digest=observation.last_mediated_action_digest,
            last_artifact_digest=observation.last_artifact_digest,
            last_diff_digest=observation.last_diff_digest,
            last_observable_progress_elapsed_seconds=self._last_progress,
            observable_progress=progress,
            no_observable_progress=no_progress,
            silence_diagnostic=no_progress,
            hard_timeout_reached=hard_timeout,
        )
        self._sequence += 1
        self._last_emitted = elapsed_seconds
        return record


def timeout_recovery_action(
    *,
    retry_within_policy: bool,
    retry_within_counters: bool,
    retry_within_resource_budgets: bool,
    replan_authorized: bool,
) -> Literal["same_strategy_retry", "replan_required", "denied"]:
    if retry_within_policy and retry_within_counters and retry_within_resource_budgets:
        return "same_strategy_retry"
    return "replan_required" if replan_authorized else "denied"
