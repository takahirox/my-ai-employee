"""Derive scoped routing statistics from committed, verified runtime facts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .domain import (
    EvaluationDecision,
    ExecutionStrategy,
    GoalTaskKind,
    StrategyPerformance,
    TaskAssessment,
)
from .domain.base import Digest
from .domain.v2 import WorkerRequest, WorkerResult
from .routing import record_outcome
from .run_budget import check_wall_budget
from .serialization import canonical_digest
from .stage_control import StageCancellation
from .storage import SQLiteStore

if TYPE_CHECKING:
    from .task_orchestration import GraphRunRecord, NodeExecutionRecord, NodeRouteRecord


@dataclass(frozen=True)
class VerifiedRoutingHistory:
    performances: tuple[StrategyPerformance, ...] = ()
    evidence_digests: tuple[Digest, ...] = ()


def strategy_fingerprint(strategy: ExecutionStrategy) -> str:
    # Selection mode and explanatory prose do not change the executed model/configuration.
    return canonical_digest(strategy.model_dump(exclude={"routing_reasons", "routing_mode"}))


def assessment_fingerprint(assessment: TaskAssessment) -> str:
    profile = assessment.semantic_profile
    length = assessment.context_character_count
    return canonical_digest(
        {
            "complexity": assessment.complexity,
            "scale": assessment.scale,
            "risk": assessment.risk,
            "capabilities": sorted(assessment.required_capabilities),
            "semantic_profile": None
            if profile is None
            else profile.model_dump(exclude={"reasons"}),
            "context_band": None
            if length is None
            else next(bound for bound in (512, 2048, 8192, 10000) if length <= bound),
        }
    )


def load_verified_routing_history(
    store: SQLiteStore,
    *,
    run_id: str,
    strategies: Iterable[ExecutionStrategy],
    assessment: TaskAssessment,
    task_kind: GoalTaskKind,
    harness_digest: str,
    effective_policy_digest: str,
    operator_config_digest: str | None,
) -> VerifiedRoutingHistory:
    """Read at most 200 repository-local runs; never trust the old unscoped aggregate."""
    from .task_orchestration import GraphRunRecord

    if operator_config_digest is None:
        return VerifiedRoutingHistory()
    fingerprints = {item.id: strategy_fingerprint(item) for item in strategies}
    assessment_key = assessment_fingerprint(assessment)
    aggregate: dict[str, StrategyPerformance] = {}
    sources: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for historical_id in store.repository_graph_run_ids(run_id, limit=200):
        cancelled = StageCancellation().cancelled()
        check_wall_budget()
        if cancelled:
            return VerifiedRoutingHistory()
        if len(seen) >= 200:
            break
        if historical_id == run_id:
            continue
        try:
            run = store.get("graph_run_v2", historical_id, GraphRunRecord)
            if (
                run.status not in {"completed", "ready_to_promote", "failed"}
                or run.goal.task_kind is not task_kind
                or run.harness_digest != harness_digest
                or run.effective_policy_digest != effective_policy_digest
                or run.operator_config_digest != operator_config_digest
            ):
                continue
            samples = _verified_samples(store, run, fingerprints, assessment_key)
        except (KeyError, ValueError):
            # Missing, legacy or corrupt optional history must not stop a new task.
            continue
        for strategy_id, succeeded, seconds, identity, digests in samples:
            if len(seen) >= 200:
                break
            if identity in seen:
                continue
            seen.add(identity)
            aggregate[strategy_id] = record_outcome(
                aggregate.get(strategy_id),
                strategy_id=strategy_id,
                succeeded=succeeded,
                duration_seconds=seconds,
                cost=0.0,
            )
            sources.update(digests)
    return VerifiedRoutingHistory(
        tuple(aggregate[key] for key in sorted(aggregate)), tuple(sorted(sources))
    )


def _verified_samples(
    store: SQLiteStore,
    run: GraphRunRecord,
    fingerprints: dict[str, str],
    assessment_key: str,
) -> list[tuple[str, bool, float, tuple[str, str], tuple[str, ...]]]:
    from .run_ownership import RunLeaseClosureRecord
    from .task_orchestration import (
        NodeEvaluatorRecord,
        NodeEvidenceRecord,
        NodeExecutionRecord,
        NodeRouteRecord,
        TaskGraphAcceptance,
        _evaluate_node_criteria,
        _validate_retained_node,
    )

    closures = [
        item
        for item in store.list_records("run_lease_closure_v2", RunLeaseClosureRecord, run_id=run.id)
        if item.graph_run_id == run.id
        and item.execution_attempt == run.execution_attempt
        and item.accepted_graph_revision_digest == run.accepted_graph_revision_digest
        and item.generation == run.generation
        and item.terminal_graph_status == run.status
    ]
    if len(closures) != 1:
        return []
    histories = store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run.id)
    latest: dict[tuple[str, int, int], NodeExecutionRecord] = {}
    for record in histories:
        key = (record.node_id, record.generation, record.attempt)
        if key not in latest or latest[key].sequence < record.sequence:
            latest[key] = record
    routes = {
        item.content_digest: item
        for item in store.list_records("node_route_v2", NodeRouteRecord, run_id=run.id)
    }
    requests = {
        item.content_digest: item
        for item in store.list_records("worker_request_v2", WorkerRequest, run_id=run.id)
    }
    acceptances = {
        item.accepted_revision.content_digest: item.accepted_revision
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
        )
    }
    samples: list[tuple[str, bool, float, tuple[str, str], tuple[str, ...]]] = []
    for key, record in sorted(
        latest.items(), key=lambda item: (item[1].transitioned_at, item[0]), reverse=True
    ):
        cancelled = StageCancellation().cancelled()
        check_wall_budget()
        if cancelled:
            return []
        try:
            publication = record
            if record.status == "passed" and record.output_generation != record.generation:
                if record.generation != run.generation or record.output_generation is None:
                    continue
                original = latest.get((record.node_id, record.output_generation, record.attempt))
                # Same-revision resume only changes lifecycle metadata. Never infer
                # a retained success from a request digest or generation alone.
                metadata = {
                    "id",
                    "created_at",
                    "transitioned_at",
                    "generation",
                    "sequence",
                    "content_digest",
                }
                if original is None or original.model_dump(exclude=metadata) != record.model_dump(
                    exclude=metadata
                ):
                    continue
                _validate_retained_node(store, publication)
                record = original
            route = routes[record.route_digest]
            if record.worker_result_id is None:
                continue
            result = store.get("worker_result_v2", record.worker_result_id, WorkerResult)
            request = requests.get(record.worker_request_digest)
            if request is None:
                # WorkCoordinator persists the same request under its child run.
                matches = [
                    item
                    for item in store.list_records(
                        "worker_request_v2", WorkerRequest, run_id=result.run_id
                    )
                    if item.content_digest == record.worker_request_digest
                ]
                if len(matches) != 1:
                    continue
                request = matches[0]
            if (
                record.status not in {"passed", "failed"}
                or record.retained_from_revision_digest is not None
                or route.selected_strategy.id not in fingerprints
                or strategy_fingerprint(route.selected_strategy)
                != fingerprints[route.selected_strategy.id]
                or assessment_fingerprint(route.assessment) != assessment_key
                or not _route_request_match(run, record, route, request)
                or record.worker_result_id is None
                or record.evaluator_id is None
                or record.evidence_id is None
            ):
                continue
            evaluator = store.get("node_evaluator_v2", record.evaluator_id, NodeEvaluatorRecord)
            evidence = store.get("node_evidence_v2", record.evidence_id, NodeEvidenceRecord)
            if (
                result.run_id != request.run_id
                or result.request_digest != request.content_digest
                or result.content_digest != record.worker_result_digest
                or evaluator.content_digest != record.evaluator_digest
                or evidence.content_digest != record.evidence_digest
                or evaluator.worker_result_digest != result.content_digest
                or evaluator.evidence_digest != evidence.content_digest
                or any(
                    item.run_id != request.run_id
                    or item.node_id != record.node_id
                    or item.generation != record.generation
                    or item.attempt != record.attempt
                    or item.accepted_graph_revision_digest != record.accepted_graph_revision_digest
                    for item in (evaluator, evidence)
                )
            ):
                continue
            node = next(
                item
                for item in acceptances[record.accepted_graph_revision_digest].graph.nodes
                if item.id == record.node_id
            )
            succeeded = record.status == "passed" and evaluator.decision is EvaluationDecision.PASS
            if succeeded:
                if (
                    run.status not in {"completed", "ready_to_promote"}
                    or publication.generation != run.generation
                    or record.accepted_graph_revision_digest != run.accepted_graph_revision_digest
                    or any(
                        other.node_id == record.node_id
                        and (other.generation, other.attempt) > key[1:]
                        for other in latest.values()
                    )
                    or result.status != "succeeded"
                    or _evaluate_node_criteria(
                        node.completion_criteria, evidence.criteria, record.artifact_descriptors
                    )
                    is not EvaluationDecision.PASS
                    or not _verified_goal(store, run)
                ):
                    continue
                _validate_retained_node(store, record)
                if run.independent_task_review and not _review_pass(store, run, record):
                    continue
            elif (
                record.status != "failed"
                or evaluator.decision is not EvaluationDecision.FAIL
                or record.failure_code
                not in {
                    "NODE_EVALUATION_NOT_PASS",
                    "VERIFICATION_FAILED",
                    "PATCH_PREFLIGHT_FAILED",
                    "WORKER_PROTOCOL_ERROR",
                    "WORKER_EMPTY_OUTPUT",
                    "WORKER_STRUCTURED_OUTPUT_MISSING",
                }
            ):
                # Control/policy/transport interruptions and unverified review outcomes
                # do not become model-quality observations.
                continue
            started = [
                item.transitioned_at
                for item in histories
                if (item.node_id, item.generation, item.attempt)
                == (record.node_id, record.generation, record.attempt)
                and item.status == "running"
                and item.worker_request_digest == record.worker_request_digest
            ]
            if not started:
                continue
            seconds = (record.transitioned_at - min(started)).total_seconds()
            if seconds < 0:
                continue
            samples.append(
                (
                    route.selected_strategy.id,
                    succeeded,
                    seconds,
                    (request.run_id, request.content_digest or ""),
                    (
                        record.content_digest or "",
                        publication.content_digest or "",
                        closures[0].content_digest or "",
                    ),
                )
            )
        except (KeyError, ValueError, StopIteration):
            continue
    return samples


def _route_request_match(
    run: GraphRunRecord, record: NodeExecutionRecord, route: NodeRouteRecord, request: WorkerRequest
) -> bool:
    return bool(
        record.run_id == run.id
        and route.run_id == run.id
        and request.graph_run_id == run.id
        and route.node_id == request.node_id == record.node_id
        and route.generation == request.generation == record.generation
        and route.attempt == request.attempt == record.attempt
        and route.accepted_graph_revision_digest
        == request.accepted_graph_revision_digest
        == record.accepted_graph_revision_digest
        and route.harness_digest == request.harness_digest == run.harness_digest
        and route.effective_policy_digest
        == request.effective_policy_digest
        == run.effective_policy_digest
    )


def _verified_goal(store: SQLiteStore, run: GraphRunRecord) -> bool:
    from .graph_evaluation import ParentCandidateEvaluationRecord
    from .task_orchestration import GoalEvaluatorRecord, TaskGraphAcceptance

    goals = [
        item
        for item in store.list_records("goal_evaluator_v2", GoalEvaluatorRecord, run_id=run.id)
        if item.content_digest == run.goal_evaluator_digest
    ]
    if (
        len(goals) != 1
        or goals[0].run_id != run.id
        or goals[0].decision is not EvaluationDecision.PASS
        or goals[0].accepted_graph_revision_digest != run.accepted_graph_revision_digest
    ):
        return False
    acceptance = next(
        item
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
        )
        if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
    )
    writing = any(
        "edit_intent" in node.required_capabilities
        for node in acceptance.accepted_revision.graph.nodes
    )
    if not writing:
        return run.status == "completed"
    if run.status != "ready_to_promote" or run.parent_evaluation_id is None:
        return False
    parent = store.get(
        "parent_candidate_evaluation_v2", run.parent_evaluation_id, ParentCandidateEvaluationRecord
    )
    return bool(
        parent.run_id == run.id
        and parent.content_digest == run.parent_evaluation_digest
        and parent.decision is EvaluationDecision.PASS
        and parent.goal_evaluator_digest == run.goal_evaluator_digest
        and parent.accepted_graph_revision_digest == run.accepted_graph_revision_digest
        and parent.composition_record_digest == run.composition_digest
        and parent.candidate_artifact_digest == run.parent_candidate_digest
        and parent.effective_policy_digest == run.effective_policy_digest
    )


def _review_pass(store: SQLiteStore, run: GraphRunRecord, record: NodeExecutionRecord) -> bool:
    from .task_review import (
        TaskReviewAction,
        TaskReviewDecision,
        TaskReviewRequest,
        TaskReviewResult,
        decide_task_review,
    )

    requests = {
        item.content_digest: item
        for item in store.list_records("task_review_request_v2", TaskReviewRequest, run_id=run.id)
    }
    results = {
        item.content_digest: item
        for item in store.list_records("task_review_result_v2", TaskReviewResult, run_id=run.id)
    }
    for decision in store.list_records(
        "task_review_decision_v2", TaskReviewDecision, run_id=run.id
    ):
        if (
            decision.node_id != record.node_id
            or decision.generation != record.generation
            or decision.attempt != record.attempt
            or decision.action is not TaskReviewAction.PASS
        ):
            continue
        request = requests[decision.request_digest]
        result = results[decision.result_digest]
        verified = decide_task_review(
            request,
            result,
            block_severities=run.task_review_block_severities,
            decision_id=decision.id,
            run_id=run.id,
            created_at=decision.created_at,
        )
        if (
            verified == decision
            and request.worker_request_digest == record.worker_request_digest
            and request.worker_result_digest == record.worker_result_digest
            and record.evidence_digest in request.deterministic_evidence_digests
            and record.evaluator_digest in request.deterministic_evidence_digests
        ):
            return True
    return False
