"""Explicit orchestration choices and local wall-time observations, not authority."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from time import monotonic
from typing import ClassVar, Literal

from pydantic import Field

from .domain import ProjectHarnessV2, RoutingMode
from .domain.base import Digest, Identifier
from .domain.v2 import DigestedRecordV2, SchemaModelV2
from .serialization import project_harness_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore


class StageChoice(SchemaModelV2):
    schema_name: ClassVar[str] = "orchestration_stage_choice"
    stage: Identifier
    disposition: Literal["selected", "required", "omitted"]
    reason: str = Field(min_length=1, max_length=1_000)


class ExecutionProfile(DigestedRecordV2):
    schema_name: ClassVar[str] = "execution_profile"
    profile: Literal["lightweight", "adaptive"]
    routing_mode: RoutingMode
    fixed_strategy_id: Identifier | None
    minimal_sufficient_guidance: bool = True
    harness_digest: Digest
    operator_config_digest: Digest
    stages: tuple[StageChoice, ...]
    escalation: str
    risk_inference: Literal["not_inferred_from_profile"] = "not_inferred_from_profile"


class ProfileTiming(DigestedRecordV2):
    schema_name: ClassVar[str] = "execution_profile_timing"
    profile_digest: Digest
    phase: Literal["invocation_start", "before_first_worker", "invocation"]
    seconds: float = Field(ge=0, allow_inf_nan=False)


def choose_profile(
    run_id: str,
    harness: ProjectHarnessV2,
    operator_digest: str,
    mode: RoutingMode,
    strategy_id: str | None,
    *,
    minimal_sufficient: bool = True,
) -> ExecutionProfile:
    minimal = mode is RoutingMode.FIXED
    review = harness.verification.review
    if minimal and review.plan_review:
        raise ValueError("Harness requires plan review; select --profile adaptive")
    if minimal and strategy_id is None:
        raise ValueError("lightweight profile requires an explicit authorized --strategy")
    stages = []
    for stage in ("goal_assessment", "planning", "plan_review", "node_assessment"):
        stages.append(
            StageChoice(
                stage=stage,
                disposition=(
                    "omitted"
                    if minimal
                    else "required"
                    if stage == "plan_review" and review.plan_review
                    else "selected"
                ),
                reason=(
                    "Operator selected one bounded node and a fixed strategy; no risk inference."
                    if minimal
                    else "Required by Harness; lightweight cannot skip this gate."
                    if stage == "plan_review" and review.plan_review
                    else "Operator selected adaptive orchestration and its validated plan gate."
                ),
            )
        )
    for stage, required in (
        ("task_review", review.independent_task_review),
        ("parent_review", review.parent_semantic_review),
        ("artifact_review", review.required),
    ):
        stages.append(
            StageChoice(
                stage=stage,
                disposition="required" if required else "omitted",
                reason="Required by Harness." if required else "Not required by Harness.",
            )
        )
    stages.append(
        StageChoice(
            stage="verification_and_approval",
            disposition="required",
            reason="Unchanged Harness/Goal checks, freshness, budgets and promotion authority.",
        )
    )
    return ExecutionProfile(
        id="profile-" + run_id,
        run_id=run_id,
        created_at=now(),
        profile="lightweight" if minimal else "adaptive",
        routing_mode=mode,
        fixed_strategy_id=strategy_id,
        minimal_sufficient_guidance=minimal_sufficient,
        harness_digest=project_harness_digest(harness),
        operator_config_digest=operator_digest,
        stages=tuple(stages),
        escalation=(
            "Do not expand accepted scope or switch strategy. If investigation reveals missing "
            "criteria, authority or design decisions, stop with findings; use an explicit new "
            "adaptive run or supported bounded repair/replan, retaining the original outcome."
        ),
    )


class ProfileObservation:
    def __init__(self, store: SQLiteStore, profile: ExecutionProfile, started: float) -> None:
        self.store, self.profile, self.started = store, profile, started
        self.id = identifier("profile-invocation")

    def record(
        self,
        phase: Literal["invocation_start", "before_first_worker", "invocation"],
        *,
        store: SQLiteStore | None = None,
    ) -> None:
        record = ProfileTiming(
            id=self.id + "-" + phase,
            run_id=self.profile.run_id,
            created_at=now(),
            profile_digest=self.profile.content_digest or "",
            phase=phase,
            seconds=max(0.0, monotonic() - self.started),
        )
        (store or self.store).put_once("execution_profile_timing_v2", record, run_id=record.run_id)


@contextmanager
def observe_profile(
    store: SQLiteStore, profile: ExecutionProfile, *, started: float
) -> Iterator[ProfileObservation]:
    try:
        previous = store.get("execution_profile_v2", profile.id, ExecutionProfile)
    except KeyError:
        store.put_once("execution_profile_v2", profile, run_id=profile.run_id)
    else:
        ignored = {"content_digest", "created_at"}
        if previous.model_dump(exclude=ignored) != profile.model_dump(exclude=ignored):
            raise ValueError("execution profile changed since the original invocation")
        profile = previous
    observer = ProfileObservation(store, profile, started)
    observer.record("invocation_start")
    try:
        yield observer
    finally:
        observer.record("invocation")


def inspect_profile(store: SQLiteStore, run_id: str) -> dict[str, object] | None:
    from .adaptive_execution import AdaptiveExecutionDecision

    try:
        profile = store.get("execution_profile_v2", "profile-" + run_id, ExecutionProfile)
    except KeyError:
        return None
    timings = store.list_records("execution_profile_timing_v2", ProfileTiming, run_id=run_id)
    if any(item.profile_digest != profile.content_digest for item in timings):
        raise ValueError("execution timing has a stale profile binding")
    started_ids = {
        item.id.removesuffix("-invocation_start")
        for item in timings
        if item.phase == "invocation_start"
    }
    finished_ids = {
        item.id.removesuffix("-invocation") for item in timings if item.phase == "invocation"
    }
    complete = bool(started_ids) and started_ids == finished_ids
    subtotal = sum(item.seconds for item in timings if item.phase == "invocation")
    elapsed = None
    if complete:
        beginning = min(
            item.created_at - timedelta(seconds=item.seconds)
            for item in timings
            if item.phase == "invocation_start"
        )
        ending = max(item.created_at for item in timings if item.phase == "invocation")
        measured = (ending - beginning).total_seconds()
        elapsed = measured if measured >= 0 else None
    adaptive_path = None
    effective_stages = [item.model_dump(mode="json") for item in profile.stages]
    try:
        decision = store.get(
            "adaptive_execution_decision_v2", "adaptive-path-" + run_id, AdaptiveExecutionDecision
        )
    except KeyError:
        pass
    else:
        if decision.execution_profile_digest != profile.content_digest:
            raise ValueError("adaptive decision has a stale execution-profile binding")
        adaptive_path = {
            "path": decision.path,
            "reason": decision.reason,
            "decision_digest": decision.content_digest,
            "assessment_digest": decision.assessment_digest,
            "selected_strategy_id": decision.selected_strategy.id,
            "recommendation": (
                None if decision.recommendation is None else decision.recommendation.model_dump()
            ),
            "continuation": decision.continuation,
        }
        if decision.path == "direct":
            for stage in effective_stages:
                if stage["stage"] in {"planning", "plan_review", "node_assessment"}:
                    stage.update(disposition="omitted", reason=decision.reason)
    return {
        "choice": profile.model_dump(mode="json"),
        "adaptive_execution": adaptive_path,
        "effective_stages": effective_stages,
        "timings": [item.model_dump(mode="json") for item in timings],
        "active_invocation_wall_seconds": subtotal if complete else None,
        "completed_invocation_wall_seconds": subtotal,
        "timing_complete": complete,
        "elapsed_through_last_invocation_seconds": elapsed,
        "human_active_seconds": None,
        "later_rework": None,
        "measurement_scope": (
            "Active wall time excludes inter-invocation waiting; elapsed time uses recorded UTC "
            "timestamps through the latest completed invocation, including pause/resume waiting. "
            "Neither is measured human active time or proof of promotion."
        ),
    }
