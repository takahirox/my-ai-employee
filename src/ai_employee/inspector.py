"""Read-only Inspector projection and tiny local HTTP server."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from pydantic import RootModel

from .doctor import doctor_from_projection
from .domain import (
    Artifact,
    ContextPackage,
    ExecutionMetrics,
    Goal,
    Node,
    Run,
    VerificationEvidence,
)
from .domain.base import FrozenDict, ensure_utc
from .domain.browser import BrowserObservation
from .domain.evaluation import EvaluationEvidenceLedger, EvaluationResult, ObservationManifest
from .domain.policy_v2 import PolicyLayer
from .domain.v2 import (
    AcceptanceLedger,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    DownloadResult,
    ExecutionResult,
    InstallResult,
    NodeVerificationBinding,
    NonMutatingResultAcceptance,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    WorkerAvailability,
    WorkerBoundaryDiagnostic,
    WorkerContextManifest,
    WorkerResult,
    WorkspaceSnapshot,
)
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationRequest,
)
from .incident_runtime import INCIDENT_RUN_RECORD_KIND, IncidentRunRecord
from .inspector_ui import INDEX as _INDEX
from .parent_review import (
    ParentSemanticRepairRequest,
    ParentSemanticReviewDecision,
    ParentSemanticReviewRequest,
    ParentSemanticReviewResult,
    StaleParentSemanticReviewResult,
)
from .plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAction,
    PlanReviewAttempt,
    PlanReviewFailureEvidence,
    PlanRevisionAttempt,
)
from .promotion_approval import PromotionPolicyDecision
from .run_ownership import (
    OwnerFenceViolationRecord,
    RunExecutionOwnerRecord,
    RunLeaseClosureRecord,
    RunLeaseHeartbeatRecord,
    RunOrphanRecoveryRecord,
    RunOwnerConflictRecord,
)
from .serialization import canonical_digest, canonical_json
from .storage import SQLiteStore
from .task_orchestration import (
    DiagnosticPersistenceFailureRecord,
    GoalEvaluatorRecord,
    GraphControlFact,
    GraphRunRecord,
    LoopTransitionRecord,
    NodeControlPropagationRecord,
    NodeEvaluatorRecord,
    NodeEvidenceRecord,
    NodeExecutionRecord,
    NodeReservationRecord,
    NodeRouteRecord,
    NodeSemanticAssessmentRecord,
    NodeWatchdogRecord,
    PreAcceptanceGoalRecord,
    RetainedNodeBinding,
    StaleNodeResultRecord,
    TaskGraphAcceptance,
    WorkerTimeoutAuthorityRecord,
    _load_plan_review_history,
)
from .task_planning import ProposedGraph
from .task_review import (
    StaleTaskReviewResult,
    TaskReviewDecision,
    TaskReviewRequest,
    TaskReviewResult,
)
from .worker_supervision import (
    TimeoutRecoveryRecord,
    WorkerAttemptHeartbeatRecord,
    WorkerBudgetPreflightRecord,
    WorkerTimeoutProfileRecord,
)


class _ActionResultRecord(RootModel[ExecutionResult | DownloadResult | InstallResult]):
    """Decode the structurally distinct result variants stored under one record kind."""


def inspect_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Project all v0.1 inspection facts without exposing mutation methods."""

    run = store.get("run", run_id, Run)
    nodes = store.list_records("node", Node, run_id=run_id)
    latest_nodes: dict[str, Node] = {}
    for node in nodes:
        latest_nodes[node.id] = node
    artifacts = store.list_records("artifact", Artifact, run_id=run_id)
    evidence = store.list_records("evidence", VerificationEvidence, run_id=run_id)
    metrics = store.list_records("metrics", ExecutionMetrics, run_id=run_id)
    contexts = store.list_records("context", ContextPackage, run_id=run_id)
    requirements = run.goal.completion_criteria
    requirement_ids = {
        requirement_id
        for criterion in requirements
        for requirement_id in criterion.verification_requirement_ids
    }
    # The full typed requirements may live in an EvidencePack; this projection
    # still exposes references and persisted evidence when absent.
    events = store.events(run_id)
    results = [event.payload for event in events if event.event_type == "node.result"]
    routing = [item.custom for item in metrics if item.custom is not None]
    return {
        "run_id": run.id,
        "goal": _json_model(run.goal),
        "state": run.state.value,
        "generation": run.generation,
        "graph": {
            "revision": run.accepted_graph.revision_number,
            "digest": run.accepted_graph.content_digest,
            "stable": True,
            "candidate": None,
            "nodes": [
                {
                    "id": node.id,
                    "kind": node.kind.value,
                    "state": latest_nodes.get(node.id, node).state.value,
                }
                for node in run.accepted_graph.graph.nodes
            ],
            "edges": [_json_model(item) for item in run.accepted_graph.graph.edges],
            "entry_task_ids": list(run.accepted_graph.graph.entry_node_ids),
            "terminal_task_ids": list(run.accepted_graph.graph.terminal_node_ids),
        },
        "transitions": [_json_model(item) for item in run.transitions],
        "node_transitions": [
            _json_model(item) for node in latest_nodes.values() for item in node.transitions
        ],
        "gates": [
            item
            for item in results
            if isinstance(item, FrozenDict)
            and item.get("node_id")
            in {node.id for node in run.accepted_graph.graph.nodes if node.kind.value == "gate"}
        ],
        "artifacts": [_json_model(item) for item in artifacts],
        "contracts": [_json_model(node.output_contract) for node in run.accepted_graph.graph.nodes],
        "evidence": [_json_model(item) for item in evidence],
        "evidence_requirement_refs": sorted(requirement_ids),
        "review_decision": None,
        "context_provenance": [_json_model(item) for item in contexts],
        "routing_reasons": routing,
        "metrics": [_json_model(item) for item in metrics],
        "events": [_json_model(item) for item in events],
    }


def inspect_work_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Project v0.2 work evidence without reading artifact bodies or secrets."""

    run = store.get_work_run(run_id)
    artifacts = store.list_records("artifact_descriptor_v2", ArtifactDescriptor, run_id=run_id)
    patch = next((item for item in artifacts if item.id == run.patch_artifact_id), None)
    graph_acceptances = store.list_records(
        "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
    )
    return {
        "schema_version": "2",
        "run_id": run.id,
        "kind": "work_run",
        "state": run.status,
        "generation": run.generation,
        "run": _json_model(run),
        "graph": (None if not graph_acceptances else _json_model(graph_acceptances[-1])),
        "routing": {
            "strategy_set": run.strategy_set,
            "assessment_strategy": (
                None if run.assessment_strategy is None else _json_model(run.assessment_strategy)
            ),
            "assessment": (
                None if run.task_assessment is None else _json_model(run.task_assessment)
            ),
            "selected_strategy": (
                None if run.selected_strategy is None else _json_model(run.selected_strategy)
            ),
        },
        "events": [_json_model(item) for item in store.work_events(run_id)],
        "policy": {
            "effective_digest": run.effective_policy_digest,
            "layers": [
                _json_model(item)
                for item in store.list_records("policy_layer_v2", PolicyLayer, run_id=run_id)
            ],
            "decisions": [
                _json_model(item)
                for item in store.list_records("policy_decision_v2", PolicyDecision, run_id=run_id)
            ],
        },
        "approvals": [
            _json_model(item)
            for item in store.list_records("approval_v2", ApprovalRecord, run_id=run_id)
        ],
        "promotion_policy_decisions": [
            _json_model(item)
            for item in store.list_records(
                "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run_id
            )
        ],
        "worker": {
            "availability": [
                _json_model(item)
                for item in store.list_records(
                    "worker_availability_v2", WorkerAvailability, run_id=run_id
                )
            ],
            "results": [
                _json_model(item)
                for item in store.list_records("worker_result_v2", WorkerResult, run_id=run_id)
            ],
            "boundary_diagnostics": [
                _json_model(item)
                for item in store.list_records(
                    "worker_boundary_diagnostic_v2",
                    WorkerBoundaryDiagnostic,
                    run_id=run_id,
                )
            ],
            "typed_result_acceptances": [
                _json_model(item)
                for item in store.list_records(
                    "non_mutating_result_acceptance_v2",
                    NonMutatingResultAcceptance,
                    run_id=run_id,
                )
            ],
        },
        "workspace": [
            _json_model(item)
            for item in store.list_records("workspace_v2", WorkspaceSnapshot, run_id=run_id)
        ],
        "actions": [
            _json_model(item.root)
            for item in store.list_records("action_result_v2", _ActionResultRecord, run_id=run_id)
        ],
        "verification": [
            _json_model(item)
            for item in store.list_records("verification_result_v2", ExecutionResult, run_id=run_id)
        ],
        "verification_requests": [
            _json_model(item)
            for item in store.list_records("verification_request_v2", ProcessRequest, run_id=run_id)
        ],
        "verification_bindings": [
            _json_model(item)
            for item in store.list_records(
                "node_verification_binding_v2", NodeVerificationBinding, run_id=run_id
            )
        ],
        "artifacts": [_json_model(item) for item in artifacts],
        "patch": None if patch is None else _json_model(patch),
        "acceptance": [
            _json_model(item)
            for item in store.list_records("acceptance_ledger_v2", AcceptanceLedger, run_id=run_id)
        ],
        "review": {"digest": run.review_digest},
        "promotions": [
            _json_model(item)
            for item in store.list_records("promotion_v2", PromotionRecord, run_id=run_id)
        ],
    }


_TERMINAL_NODE_STATES = frozenset({"passed", "failed", "blocked", "cancelled"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _node_execution_projection(
    record: NodeExecutionRecord,
    history: tuple[NodeExecutionRecord, ...],
    node: Node | None,
    route: NodeRouteRecord | None,
    reservation: NodeReservationRecord | None,
    observed_at: datetime,
) -> dict[str, Any]:
    # Running duration is reconstructed only from authoritative persisted transitions.
    attempt_history = sorted(
        (
            item
            for item in history
            if item.node_id == record.node_id
            and item.accepted_graph_revision_digest == record.accepted_graph_revision_digest
            and item.generation == record.generation
            and item.attempt == record.attempt
        ),
        key=lambda item: (item.sequence, item.transitioned_at, item.id),
    )
    running = next((item for item in attempt_history if item.status == "running"), None)
    terminal = next(
        (item for item in attempt_history if item.status in _TERMINAL_NODE_STATES),
        None,
    )
    running_started_at = None if running is None else running.transitioned_at
    finished_at = (
        None
        if record.status not in _TERMINAL_NODE_STATES or terminal is None
        else terminal.transitioned_at
    )
    wall_seconds: float | None = None
    if reservation is not None and isinstance(reservation.requested, Mapping):
        persisted_wall_seconds = reservation.requested.get("wall_seconds")
        if isinstance(persisted_wall_seconds, (int, float)) and not isinstance(
            persisted_wall_seconds, bool
        ):
            wall_seconds = float(persisted_wall_seconds)
    if wall_seconds is None and node is not None:
        wall_seconds = node.resource_budget.wall_seconds
    deadline_at = (
        None
        if running_started_at is None or wall_seconds is None
        else running_started_at + timedelta(seconds=wall_seconds)
    )
    elapsed_end = finished_at or observed_at
    elapsed_seconds = (
        None
        if running_started_at is None
        else max(0.0, (elapsed_end - running_started_at).total_seconds())
    )
    overdue = record.status == "running" and deadline_at is not None and observed_at >= deadline_at
    projection = _json_model(record)
    projection.update(
        {
            "operational_status": "overdue" if overdue else record.status,
            "running_started_at": _timestamp(running_started_at),
            "last_persisted_activity_at": _timestamp(record.transitioned_at),
            "finished_at": _timestamp(finished_at),
            "elapsed_seconds": elapsed_seconds,
            "wall_time_budget_seconds": wall_seconds,
            "deadline_at": _timestamp(deadline_at),
            "overdue": overdue,
            "selected_strategy_id": None if route is None else route.selected_strategy.id,
            "verification_count": len(record.verification_result_digests),
        }
    )
    return projection


def _run_ownership_projection(
    store: SQLiteStore,
    run: GraphRunRecord,
    latest_nodes: Mapping[str, NodeExecutionRecord],
    observed_at: datetime,
) -> dict[str, Any]:
    owners = store.list_records("run_execution_owner_v2", RunExecutionOwnerRecord, run_id=run.id)
    heartbeats = store.list_records(
        "run_lease_heartbeat_v2", RunLeaseHeartbeatRecord, run_id=run.id
    )
    closures = store.list_records("run_lease_closure_v2", RunLeaseClosureRecord, run_id=run.id)
    conflicts = store.list_records("run_owner_conflict_v2", RunOwnerConflictRecord, run_id=run.id)
    fence_violations = store.list_records(
        "owner_fence_violation_v2", OwnerFenceViolationRecord, run_id=run.id
    )
    recoveries = store.list_records(
        "run_orphan_recovery_v2", RunOrphanRecoveryRecord, run_id=run.id
    )
    current = store.current_run_owner(run.id)
    owner_by_id = {item.id: item for item in owners}
    current_owner = None if current is None else owner_by_id.get(str(current["owner_record_id"]))
    binding_matches = bool(
        current is not None
        and current_owner is not None
        and current["owner_record_digest"] == current_owner.content_digest
        and current["graph_revision_digest"] == run.accepted_graph_revision_digest
        and cast(int, current["generation"]) == run.generation
        and cast(int, current["execution_attempt"]) == run.execution_attempt
        and current["owner_instance_id"] == current_owner.owner_instance_id
    )
    expired = bool(current is not None and observed_at >= ensure_utc(current["expires_at"]))
    parent_nonterminal = run.status == "running"
    live = bool(
        parent_nonterminal
        and current is not None
        and current["status"] == "active"
        and binding_matches
        and not expired
    )
    terminal_child_ids = tuple(
        sorted(
            {
                item.work_run_id
                for item in latest_nodes.values()
                if item.work_run_id is not None and item.status in _TERMINAL_NODE_STATES
            }
        )
    )
    child_parent_incident = bool(parent_nonterminal and terminal_child_ids)
    liveness_state: str
    diagnostic_code: str | None
    if run.status in {"planned", "paused", "ready_to_promote"}:
        liveness_state = run.status
        diagnostic_code = None
    elif not parent_nonterminal:
        liveness_state = "terminal"
        diagnostic_code = None
    elif current is None:
        liveness_state = "orphaned"
        diagnostic_code = "RUN_OWNER_ABSENT"
    elif current["status"] == "recovered":
        liveness_state = "interrupted"
        diagnostic_code = None
    elif current["status"] != "active":
        liveness_state = "interrupted"
        diagnostic_code = "PARENT_TERMINALIZATION_MISSING" if child_parent_incident else None
    elif not binding_matches:
        liveness_state = "orphaned"
        diagnostic_code = "RUN_OWNER_CONFLICT"
    elif expired:
        liveness_state = "orphaned"
        diagnostic_code = "RUN_LEASE_EXPIRED"
    elif child_parent_incident:
        liveness_state = "parent_terminalization_missing"
        diagnostic_code = "CHILD_TERMINAL_PARENT_NONTERMINAL"
    else:
        liveness_state = "live"
        diagnostic_code = None
    return {
        "state": liveness_state,
        "is_active": live and not child_parent_incident,
        "diagnostic_code": diagnostic_code,
        "last_authoritative_graph_state": run.status,
        "graph_revision_digest": run.accepted_graph_revision_digest,
        "generation": run.generation,
        "execution_attempt": run.execution_attempt,
        "owner_instance_id": None if current is None else current["owner_instance_id"],
        "owner_record_id": None if current is None else current["owner_record_id"],
        "owner_record_digest": None if current is None else current["owner_record_digest"],
        "last_heartbeat": (
            None if current is None else _timestamp(ensure_utc(current["last_heartbeat_at"]))
        ),
        "lease_expiry": (
            None if current is None else _timestamp(ensure_utc(current["expires_at"]))
        ),
        "observed_at": _timestamp(observed_at) if parent_nonterminal else None,
        "terminal_child_run_ids": list(terminal_child_ids),
        "owners": [_json_model(item) for item in owners],
        "heartbeats": [_json_model(item) for item in heartbeats],
        "closures": [_json_model(item) for item in closures],
        "conflicts": [_json_model(item) for item in conflicts],
        "fence_violations": [_json_model(item) for item in fence_violations],
        "recoveries": [_json_model(item) for item in recoveries],
    }


def inspect_graph_run(
    store: SQLiteStore,
    run_id: str,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Project exact graph handoff records without opening artifact bodies."""

    run = store.get("graph_run_v2", run_id, GraphRunRecord)
    acceptances = store.list_records("task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id)
    acceptances = tuple(
        sorted(acceptances, key=lambda item: item.accepted_revision.revision_number)
    )
    acceptance = next(
        (
            item
            for item in reversed(acceptances)
            if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
        ),
        None,
    )
    review_attempts = tuple(
        sorted(
            store.list_records("plan_review_attempt_v2", PlanReviewAttempt, run_id=run_id),
            key=lambda item: item.review_round,
        )
    )
    review_failures = store.list_records(
        "plan_review_failure_evidence_v2", PlanReviewFailureEvidence, run_id=run_id
    )
    revision_attempts = store.list_records(
        "plan_revision_attempt_v2", PlanRevisionAttempt, run_id=run_id
    )
    review_bindings = store.list_records(
        "plan_review_acceptance_binding_v2",
        PlanReviewAcceptanceBinding,
        run_id=run_id,
    )
    if not review_attempts and not revision_attempts and not review_bindings:
        review_status = "not_configured"
    elif len(review_bindings) == 1:
        try:
            _load_plan_review_history(store, run, acceptances)
        except ValueError:
            review_status = "failed"
        else:
            review_status = "revised" if revision_attempts else "accepted"
    elif (
        review_attempts
        and review_attempts[-1].outcome == "completed"
        and review_attempts[-1].action is PlanReviewAction.REJECT
    ):
        review_status = "blocked"
    else:
        review_status = "failed"
    node_records = store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run_id)
    latest_nodes: dict[str, NodeExecutionRecord] = {}
    for node_record in node_records:
        previous = latest_nodes.get(node_record.node_id)
        if previous is None or (
            node_record.generation,
            node_record.attempt,
            node_record.sequence,
            node_record.created_at,
        ) > (
            previous.generation,
            previous.attempt,
            previous.sequence,
            previous.created_at,
        ):
            latest_nodes[node_record.node_id] = node_record
    routes = store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)
    reservations = store.list_records("node_reservation_v2", NodeReservationRecord, run_id=run_id)
    observed_at = ensure_utc(clock())
    run_ownership = _run_ownership_projection(store, run, latest_nodes, observed_at)
    graph_nodes = (
        {}
        if acceptance is None
        else {item.id: item for item in acceptance.accepted_revision.graph.nodes}
    )
    route_by_attempt = {
        (
            item.node_id,
            item.accepted_graph_revision_digest,
            item.generation,
            item.attempt,
        ): item
        for item in sorted(routes, key=lambda item: (item.created_at, item.id))
    }
    reservation_by_attempt = {
        (
            item.node_id,
            item.accepted_graph_revision_digest,
            item.generation,
            item.attempt,
        ): item
        for item in sorted(reservations, key=lambda item: (item.created_at, item.id))
    }
    node_projections: list[dict[str, Any]] = []
    for node_id in sorted(latest_nodes):
        record = latest_nodes[node_id]
        binding = (
            record.node_id,
            record.accepted_graph_revision_digest,
            record.generation,
            record.attempt,
        )
        node_projections.append(
            _node_execution_projection(
                record,
                node_records,
                graph_nodes.get(node_id),
                route_by_attempt.get(binding),
                reservation_by_attempt.get(binding),
                observed_at,
            )
        )
    composition = (
        None
        if run.composition_id is None
        else store.get(
            "graph_patch_composition_v2", run.composition_id, GraphPatchCompositionRecord
        )
    )
    evaluation = (
        None
        if run.parent_evaluation_id is None
        else store.get(
            "parent_candidate_evaluation_v2",
            run.parent_evaluation_id,
            ParentCandidateEvaluationRecord,
        )
    )
    candidate = (
        None
        if run.parent_candidate_artifact_id is None
        else store.get(
            "artifact_descriptor_v2", run.parent_candidate_artifact_id, ArtifactDescriptor
        )
    )
    timeout_authorities = store.list_records(
        "worker_timeout_authority_v2", WorkerTimeoutAuthorityRecord, run_id=run_id
    )
    timeout_profiles = store.list_records(
        "worker_timeout_profile_v2", WorkerTimeoutProfileRecord, run_id=run_id
    )
    timeout_preflights = store.list_records(
        "worker_budget_preflight_v2", WorkerBudgetPreflightRecord, run_id=run_id
    )
    attempt_heartbeats = store.list_records(
        "worker_attempt_heartbeat_v2", WorkerAttemptHeartbeatRecord, run_id=run_id
    )
    timeout_recoveries = store.list_records(
        "timeout_recovery_v2", TimeoutRecoveryRecord, run_id=run_id
    )
    watchdogs = store.list_records("node_watchdog_v2", NodeWatchdogRecord, run_id=run_id)
    control_propagations = store.list_records(
        "node_control_propagation_v2", NodeControlPropagationRecord, run_id=run_id
    )
    child_run_ids = tuple(
        dict.fromkeys(
            (
                *(item.child_run_id for item in timeout_profiles),
                *(item.child_run_id for item in timeout_authorities),
            )
        )
    )
    child_worker_outcomes = {
        "results": [
            _json_model(item)
            for child_run_id in child_run_ids
            for item in store.list_records("worker_result_v2", WorkerResult, run_id=child_run_id)
        ],
        "process_results": [
            _json_model(item.root)
            for child_run_id in child_run_ids
            for item in store.list_records(
                "action_result_v2", _ActionResultRecord, run_id=child_run_id
            )
        ],
        "diagnostics": [
            _json_model(item)
            for child_run_id in child_run_ids
            for item in store.list_records(
                "worker_boundary_diagnostic_v2",
                WorkerBoundaryDiagnostic,
                run_id=child_run_id,
            )
        ],
    }
    return {
        "schema_version": "2",
        "run_id": run.id,
        "kind": "graph_run",
        "state": run.status,
        "generation": run.generation,
        "execution_attempt": run.execution_attempt,
        "run_ownership": run_ownership,
        "replan_count": run.replan_count,
        "run": _json_model(run),
        "planner_routing": (
            None if run.planner_routing is None else _json_model(run.planner_routing)
        ),
        "graph_acceptance": None if acceptance is None else _json_model(acceptance),
        "graph_revisions": [_json_model(item) for item in acceptances],
        "plan_review": {
            "status": review_status,
            "attempts": [_json_model(item) for item in review_attempts],
            "failure_evidence": [_json_model(item) for item in review_failures],
            "revision_attempts": [_json_model(item) for item in revision_attempts],
            "acceptance_binding": (
                None if len(review_bindings) != 1 else _json_model(review_bindings[0])
            ),
        },
        "retained_node_bindings": [
            _json_model(item)
            for item in sorted(
                store.list_records("retained_node_binding_v2", RetainedNodeBinding, run_id=run_id),
                key=lambda item: (item.generation, item.node_id),
            )
        ],
        "artifact_descriptors": [
            _json_model(item)
            for record in latest_nodes.values()
            for item in record.artifact_descriptors
        ],
        "nodes": node_projections,
        "node_history": [_json_model(item) for item in node_records],
        "claims": list(store.graph_claims(run_id)),
        "reservations": [_json_model(item) for item in reservations],
        "worker_timeout_authorities": [_json_model(item) for item in timeout_authorities],
        "worker_timeout_profiles": [_json_model(item) for item in timeout_profiles],
        "worker_budget_preflights": [_json_model(item) for item in timeout_preflights],
        "worker_attempt_heartbeats": [_json_model(item) for item in attempt_heartbeats],
        "timeout_recoveries": [_json_model(item) for item in timeout_recoveries],
        "node_watchdogs": [_json_model(item) for item in watchdogs],
        "node_control_propagations": [_json_model(item) for item in control_propagations],
        "child_worker_outcomes": child_worker_outcomes,
        "incident_reporting": [
            record.model_dump(
                mode="json",
                include={
                    "state",
                    "internal_incident_code",
                    "error_code",
                    "fingerprint",
                    "report_digest",
                    "preview_digest",
                    "expiry",
                    "issue_number",
                    "public_url",
                    "public_report_digest",
                    "authorization_mode",
                    "authorization_digest",
                    "authorized_at",
                    "published_at",
                },
            )
            for record in sorted(
                store.list_records(
                    INCIDENT_RUN_RECORD_KIND,
                    IncidentRunRecord,
                    run_id=run_id,
                ),
                key=lambda item: (item.created_at, item.id),
            )[:20]
        ],
        "diagnostic_persistence_failures": [
            _json_model(item)
            for item in store.list_records(
                "diagnostic_persistence_failure_v2",
                DiagnosticPersistenceFailureRecord,
                run_id=run_id,
            )
        ],
        "node_semantic_assessments": [
            _json_model(item)
            for item in store.list_records(
                "node_semantic_assessment_v2",
                NodeSemanticAssessmentRecord,
                run_id=run_id,
            )
        ],
        "routes": [_json_model(item) for item in routes],
        "worker_context_manifests": [
            _json_model(item)
            for item in store.list_records(
                "worker_context_manifest_v2", WorkerContextManifest, run_id=run_id
            )
        ],
        "worker_results": [
            _json_model(store.get("worker_result_v2", item.worker_result_id, WorkerResult))
            for item in node_records
            if item.worker_result_id is not None
        ],
        "worker_boundary_diagnostics": [
            _json_model(item)
            for item in store.list_records(
                "worker_boundary_diagnostic_v2",
                WorkerBoundaryDiagnostic,
                run_id=run_id,
            )
        ],
        "typed_result_acceptances": [
            _json_model(item)
            for item in {
                record.result_acceptance_id: store.get(
                    "non_mutating_result_acceptance_v2",
                    record.result_acceptance_id,
                    NonMutatingResultAcceptance,
                )
                for record in node_records
                if record.result_acceptance_id is not None
            }.values()
        ],
        "node_evidence": [
            _json_model(store.get("node_evidence_v2", item.evidence_id, NodeEvidenceRecord))
            for item in node_records
            if item.evidence_id is not None
        ],
        "node_evaluator_decisions": [
            _json_model(store.get("node_evaluator_v2", item.evaluator_id, NodeEvaluatorRecord))
            for item in node_records
            if item.evaluator_id is not None
        ],
        "controls": [
            _json_model(item)
            for item in store.list_records("graph_control_fact_v2", GraphControlFact, run_id=run_id)
        ],
        "stale_results": [
            _json_model(item)
            for item in store.list_records(
                "stale_node_result_v2", StaleNodeResultRecord, run_id=run_id
            )
        ],
        "loop_transitions": [
            _json_model(item)
            for item in sorted(
                store.list_records("loop_transition_v2", LoopTransitionRecord, run_id=run_id),
                key=lambda item: (item.generation, item.created_at, item.id),
            )
        ],
        "task_reviews": {
            "requests": [
                _json_model(item)
                for item in store.list_records(
                    "task_review_request_v2", TaskReviewRequest, run_id=run_id
                )
            ],
            "results": [
                _json_model(item)
                for item in store.list_records(
                    "task_review_result_v2", TaskReviewResult, run_id=run_id
                )
            ],
            "decisions": [
                _json_model(item)
                for item in store.list_records(
                    "task_review_decision_v2", TaskReviewDecision, run_id=run_id
                )
            ],
            "stale_results": [
                _json_model(item)
                for item in store.list_records(
                    "stale_task_review_result_v2", StaleTaskReviewResult, run_id=run_id
                )
            ],
        },
        "composition": None if composition is None else _json_model(composition),
        "candidate_patch": None if candidate is None else _json_model(candidate),
        "parent_evaluation": None if evaluation is None else _json_model(evaluation),
        "parent_semantic_review": {
            "requests": [
                _json_model(item)
                for item in store.list_records(
                    "parent_semantic_review_request_v2",
                    ParentSemanticReviewRequest,
                    run_id=run_id,
                )
            ],
            "results": [
                _json_model(item)
                for item in store.list_records(
                    "parent_semantic_review_result_v2",
                    ParentSemanticReviewResult,
                    run_id=run_id,
                )
            ],
            "decisions": [
                _json_model(item)
                for item in store.list_records(
                    "parent_semantic_review_decision_v2",
                    ParentSemanticReviewDecision,
                    run_id=run_id,
                )
            ],
            "repair_requests": [
                _json_model(item)
                for item in store.list_records(
                    "parent_semantic_repair_request_v2",
                    ParentSemanticRepairRequest,
                    run_id=run_id,
                )
            ],
            "stale_results": [
                _json_model(item)
                for item in store.list_records(
                    "stale_parent_semantic_review_result_v2",
                    StaleParentSemanticReviewResult,
                    run_id=run_id,
                )
            ],
        },
        "parent_evaluation_requests": [
            _json_model(item)
            for item in store.list_records(
                "parent_candidate_evaluation_request_v2",
                ParentCandidateEvaluationRequest,
                run_id=run_id,
            )
        ],
        "parent_goal_evaluations": [
            _json_model(item)
            for item in store.list_records("goal_evaluator_v2", GoalEvaluatorRecord, run_id=run_id)
        ],
        "parent_evidence": [
            _json_model(item)
            for item in store.list_records(
                "evaluation_evidence_ledger_v2",
                EvaluationEvidenceLedger,
                run_id=run_id,
            )
        ],
        "parent_acceptance": [
            _json_model(item)
            for item in store.list_records("acceptance_ledger_v2", AcceptanceLedger, run_id=run_id)
        ],
        "parent_evaluation_results": [
            _json_model(item)
            for item in store.list_records("evaluation_result_v2", EvaluationResult, run_id=run_id)
        ],
        "parent_observation_manifests": [
            _json_model(item)
            for item in store.list_records(
                "observation_manifest_v2", ObservationManifest, run_id=run_id
            )
        ],
        "parent_browser_observations": [
            _json_model(item)
            for item in store.list_records(
                "browser_observation_v2", BrowserObservation, run_id=run_id
            )
        ],
        "approval_requests": [
            _json_model(item)
            for item in store.list_records("approval_request_v2", ApprovalRequest, run_id=run_id)
        ],
        "approvals": [
            _json_model(item)
            for item in store.list_records("approval_v2", ApprovalRecord, run_id=run_id)
        ],
        "promotion_policy_decisions": [
            _json_model(item)
            for item in store.list_records(
                "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run_id
            )
        ],
        "promotions": [
            _json_model(item)
            for item in store.list_records("promotion_v2", PromotionRecord, run_id=run_id)
        ],
    }


def inspect_failed_plan_review(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Project a pre-acceptance plan-review failure without reading artifact bodies."""

    attempts = tuple(
        sorted(
            store.list_records("plan_review_attempt_v2", PlanReviewAttempt, run_id=run_id),
            key=lambda item: item.review_round,
        )
    )
    revisions = tuple(
        sorted(
            store.list_records("plan_revision_attempt_v2", PlanRevisionAttempt, run_id=run_id),
            key=lambda item: (item.created_at, item.id),
        )
    )
    if not attempts:
        raise KeyError(("plan_review_attempt_v2", run_id))
    if any(item.goal_digest != attempts[0].goal_digest for item in attempts):
        raise ValueError("pre-acceptance plan-review Goal digests disagree")
    if revisions and revisions[-1].status == "failed":
        stable_code = "GRAPH_PLANNER_FAILED"
        review_status = "failed"
    elif attempts[-1].outcome == "failed":
        stable_code = "PLAN_REVIEW_FAILED"
        review_status = "failed"
    elif attempts[-1].outcome == "completed" and attempts[-1].action is PlanReviewAction.REJECT:
        stable_code = "PLAN_REVIEW_BLOCKED"
        review_status = "blocked"
    else:
        raise KeyError(("plan_review_attempt_v2", run_id))
    failures = store.list_records(
        "plan_review_failure_evidence_v2", PlanReviewFailureEvidence, run_id=run_id
    )
    try:
        goal_record = store.get("pre_acceptance_goal_v2", run_id, PreAcceptanceGoalRecord)
        if goal_record.goal_digest != attempts[0].goal_digest:
            raise ValueError("pre-acceptance Goal does not match its review attempt")
        goal_projection = _json_model(goal_record.goal)
    except KeyError:
        legacy_goals = store.list_records("goal_v2", Goal, run_id=run_id)
        matching_legacy = [
            item
            for item in legacy_goals
            if item.id == attempts[0].goal_id and canonical_digest(item) == attempts[0].goal_digest
        ]
        # Older databases may not contain a safe run-scoped Goal. Keep them
        # inspectable but never guess or reuse a colliding record from another run.
        goal_projection = (
            _json_model(matching_legacy[0])
            if len(matching_legacy) == 1
            else {
                "id": attempts[0].goal_id,
                "statement": None,
                "unavailable_reason": "goal_not_persisted_by_older_runtime",
            }
        )
    proposals = store.list_records("proposed_graph_v2", ProposedGraph, run_id=run_id)
    if not proposals:
        raise KeyError(("proposed_graph_v2", run_id))
    proposal = next(
        (
            item
            for item in reversed(proposals)
            if item.content_digest == attempts[-1].proposed_graph_digest
        ),
        None,
    )
    if proposal is None:
        raise KeyError(("proposed_graph_v2", attempts[-1].proposed_graph_digest))
    return {
        "schema_version": "2",
        "run_id": run_id,
        "kind": "graph_run",
        "state": "failed",
        "failure_code": stable_code,
        "generation": 0,
        "goal": goal_projection,
        "proposed_graph": _json_model(proposal),
        "graph_acceptance": None,
        "graph_revisions": [],
        "plan_review": {
            "status": review_status,
            "attempts": [_json_model(item) for item in attempts],
            "failure_evidence": [_json_model(item) for item in failures],
            "revision_attempts": [_json_model(item) for item in revisions],
        },
    }


def inspect_any_run(
    store: SQLiteStore,
    run_id: str,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Read any runtime generation without mutating or migrating state."""

    if store.is_standalone_work_run(run_id):
        raise KeyError(run_id)
    try:
        from .eval_framework import inspect_experiment

        projection = _json_model(inspect_experiment(store, run_id))
    except KeyError:
        pass
    else:
        return _attach_repository_context(store, run_id, projection)
    try:
        projection = inspect_graph_run(store, run_id, clock=clock)
    except KeyError:
        pass
    else:
        return _attach_repository_context(store, run_id, projection)
    for inspector in (inspect_failed_plan_review, inspect_work_run, inspect_run):
        try:
            projection = inspector(store, run_id)
        except KeyError:
            continue
        return _attach_repository_context(store, run_id, projection)
    raise KeyError(run_id)


_TERMINAL_RUN_STATES = frozenset(
    {
        "cancelled",
        "completed",
        "failed",
        "rejected",
        "succeeded",
    }
)
_ACTIVE_TASK_STATES = frozenset({"active", "claimed", "routed", "running"})
_ATTENTION_TASK_STATES = frozenset({"blocked", "failed", "overdue"})
_ATTENTION_RUN_STATES = frozenset(
    {"failed", "paused", "planned", "ready_to_promote", "waiting_approval"}
)
_ATTENTION_LOOP_ACTIONS = frozenset({"ESCALATE", "REPAIR", "REPLAN", "RETRY"})


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _goal_statement(projection: dict[str, Any], run: dict[str, Any]) -> str | None:
    goal = projection.get("goal", run.get("goal"))
    if isinstance(goal, str):
        return goal
    if isinstance(goal, dict) and isinstance(goal.get("statement"), str):
        statement = goal["statement"]
        return str(statement)
    return None


def _accepted_graph(projection: dict[str, Any]) -> dict[str, Any]:
    graph = projection.get("graph_acceptance") or projection.get("graph")
    graph = _as_dict(graph)
    accepted_revision = _as_dict(graph.get("accepted_revision"))
    if accepted_revision:
        return _as_dict(accepted_revision.get("graph"))
    nested = _as_dict(graph.get("graph"))
    return nested or graph


def _latest_node_facts(projection: dict[str, Any]) -> list[dict[str, Any]]:
    # Graph-run inspection already projects exactly one authoritative latest
    # record per node. Do not reconstruct latest state from UUID-sorted history.
    latest = _as_dicts(projection.get("nodes"))
    if latest:
        return latest
    graph_nodes = _as_dicts(_as_dict(projection.get("graph")).get("nodes"))
    return graph_nodes


def _latest_persisted_activity_at(projection: dict[str, Any]) -> str | None:
    """Return the newest timestamp carried by a projected persisted record."""

    candidates: list[datetime] = []
    pending: list[object] = [projection]
    timestamp_fields = {"created_at", "transitioned_at", "last_persisted_activity_at"}
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            is_node_fact = isinstance(value.get("node_id"), str) and (
                "status" in value or "operational_status" in value
            )
            for key, item in value.items():
                is_authoritative_timestamp = key in timestamp_fields and not (
                    is_node_fact and key == "created_at"
                )
                if is_authoritative_timestamp and isinstance(item, str) and item.endswith("Z"):
                    with suppress(ValueError):
                        candidates.append(datetime.fromisoformat(f"{item[:-1]}+00:00"))
                elif isinstance(item, (dict, list)):
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    if not candidates:
        return None
    return _timestamp(max(candidates))


def _attention_facts(
    projection: dict[str, Any], run: dict[str, Any], status: str, latest_nodes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    attention: list[dict[str, Any]] = []
    for record in latest_nodes:
        task_status = record.get("operational_status") or record.get("status", record.get("state"))
        if task_status in _ATTENTION_TASK_STATES:
            attention.append(
                {
                    "kind": "task",
                    "task_id": record.get("node_id", record.get("id")),
                    "condition": task_status,
                }
            )
    failure_code = projection.get("failure_code") or run.get("failure_code")
    if failure_code:
        attention.append({"kind": "run", "condition": failure_code})
    elif status in _ATTENTION_RUN_STATES:
        attention.append({"kind": "run", "condition": status})

    approvals = _as_dicts(projection.get("approvals"))
    if any(item.get("decision") == "pending" for item in approvals):
        attention.append({"kind": "approval", "condition": "approval_required"})
    plan_review = _as_dict(projection.get("plan_review"))
    if plan_review.get("status") == "blocked":
        attention.append({"kind": "plan_review", "condition": "blocked"})

    transitions = _as_dicts(projection.get("loop_transitions"))
    if transitions:
        action = transitions[-1].get("action")
        if action in _ATTENTION_LOOP_ACTIONS:
            attention.append({"kind": "loop", "condition": str(action).lower()})

    controls = _as_dicts(projection.get("controls"))
    if controls and controls[-1].get("action") in {"cancel", "pause"}:
        attention.append({"kind": "control", "condition": controls[-1]["action"]})

    unique: list[dict[str, Any]] = []
    seen: set[tuple[object, ...]] = set()
    for item in attention:
        key = (item.get("kind"), item.get("task_id"), item.get("condition"))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def inspect_fleet_runs(
    store: SQLiteStore,
    repository_id: str | None = None,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Project a live, read-only summary of top-level persisted Fleet runs."""

    active: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    child_work_run_ids = {
        item.work_run_id
        for item in store.list_records("node_execution_v2", NodeExecutionRecord)
        if item.work_run_id is not None
    }
    for context in store.list_run_repositories(repository_id):
        run_id = context["run_id"]
        if not isinstance(run_id, str):
            continue
        if run_id in child_work_run_ids:
            continue
        try:
            projection = inspect_any_run(store, run_id, clock=clock)
        except KeyError:
            # Old event/checkpoint-only IDs remain discoverable without inventing facts.
            item: dict[str, Any] = {
                **context,
                "goal": None,
                "status": "not_recorded",
                "generation": None,
                "progress": {"completed": 0, "total": 0},
                "active_task": None,
                "active_tasks": [],
                "phase": "Persisted v0.1 records",
                "last_updated_at": None,
                "requires_attention": False,
                "attention": [],
                "attention_count": None,
                "attention_available": False,
            }
            history.append(item)
            continue

        status = str(projection.get("state") or "not_recorded")
        ownership = _as_dict(projection.get("run_ownership"))
        graph_run_is_active = bool(ownership.get("is_active"))
        displayed_status = (
            str(ownership.get("state") or status)
            if projection.get("kind") == "graph_run" and status not in _TERMINAL_RUN_STATES
            else status
        )
        run = _as_dict(projection.get("run"))
        goal_statement = _goal_statement(projection, run)
        graph = _accepted_graph(projection)
        graph_nodes = _as_dicts(graph.get("nodes"))
        node_ids: list[str] = [
            str(node["id"]) for node in graph_nodes if isinstance(node.get("id"), str)
        ]
        node_labels: dict[str, str] = {
            str(node["id"]): str(node.get("name") or node.get("objective") or node["id"])
            for node in graph_nodes
            if isinstance(node.get("id"), str)
        }
        latest_node_records = _latest_node_facts(projection)
        if not node_ids:
            node_ids = [
                str(record.get("node_id", record.get("id")))
                for record in latest_node_records
                if record.get("node_id", record.get("id")) is not None
            ]
        latest_nodes = {
            str(record.get("node_id", record.get("id"))): record
            for record in latest_node_records
            if record.get("node_id", record.get("id")) is not None
        }
        completed = len(
            [
                node_id
                for node_id in node_ids
                if latest_nodes.get(node_id, {}).get(
                    "status", latest_nodes.get(node_id, {}).get("state")
                )
                in {"completed", "passed", "succeeded"}
            ]
        )
        active_tasks = [
            {
                "id": node_id,
                "label": node_labels.get(node_id, node_id),
                "status": record.get("status", record.get("state")),
            }
            for node_id, record in latest_nodes.items()
            if record.get("status", record.get("state")) in _ACTIVE_TASK_STATES
        ]
        attention = _as_dicts(projection.get("attention"))
        active_task = active_tasks[0]["label"] if active_tasks else None
        loop_attention = next(
            (item["condition"] for item in reversed(attention) if item["kind"] == "loop"), None
        )
        item = {
            **context,
            "goal": goal_statement,
            "status": displayed_status,
            "generation": projection.get("generation"),
            "progress": {"completed": completed, "total": len(node_ids)},
            "active_task": active_task,
            "active_tasks": active_tasks,
            "phase": (
                str(loop_attention).title()
                if loop_attention is not None
                else f"Task: {active_task}"
                if active_task is not None
                else status.replace("_", " ").title()
            ),
            "last_updated_at": _latest_persisted_activity_at(projection),
            "requires_attention": bool(attention),
            "attention": attention,
            "attention_count": projection.get("attention_count", len(attention)),
            "attention_available": projection.get("attention_available", True),
        }
        if projection.get("kind") == "graph_run":
            ownership_summary = {
                key: ownership.get(key)
                for key in (
                    "state",
                    "is_active",
                    "diagnostic_code",
                    "last_authoritative_graph_state",
                    "graph_revision_digest",
                    "generation",
                    "execution_attempt",
                    "owner_instance_id",
                    "owner_record_id",
                    "owner_record_digest",
                    "last_heartbeat",
                    "lease_expiry",
                    "terminal_child_run_ids",
                )
            }
            item.update(
                {
                    "authoritative_status": status,
                    "execution_attempt": projection.get("execution_attempt"),
                    "liveness": ownership_summary,
                    "last_heartbeat": ownership.get("last_heartbeat"),
                    "lease_expiry": ownership.get("lease_expiry"),
                    "owner_instance_id": ownership.get("owner_instance_id"),
                    "diagnostic_code": ownership.get("diagnostic_code"),
                }
            )
            (active if graph_run_is_active else history).append(item)
        else:
            (history if status in _TERMINAL_RUN_STATES else active).append(item)
    active.sort(key=lambda item: (not item["requires_attention"], str(item["run_id"])))
    history.sort(key=lambda item: str(item["run_id"]), reverse=True)
    return {"active": active, "history": history}


def _attach_repository_context(
    store: SQLiteStore, run_id: str, projection: dict[str, Any]
) -> dict[str, Any]:
    run = _as_dict(projection.get("run"))
    status = str(projection.get("state") or "not_recorded")
    attention = _attention_facts(projection, run, status, _latest_node_facts(projection))
    projection["attention"] = attention
    projection["attention_count"] = len(attention)
    projection["attention_available"] = True
    projection["doctor"] = doctor_from_projection(projection)
    repository = store.repository_for_run(run_id)
    if repository is not None:
        projection["repository_context"] = repository
    return projection


def _json_model(value: object) -> dict[str, Any]:
    data = json.loads(canonical_json(value))
    if not isinstance(data, dict):
        raise TypeError("Inspector model projection must be an object")
    return data


def compare_runs(store: SQLiteStore, left_id: str, right_id: str) -> dict[str, Any]:
    left = inspect_run(store, left_id)
    right = inspect_run(store, right_id)
    return {
        "left": {"run_id": left_id, "state": left["state"], "metrics": left["metrics"]},
        "right": {"run_id": right_id, "state": right["state"], "metrics": right["metrics"]},
        "same_graph_digest": left["graph"]["digest"] == right["graph"]["digest"],
    }


_SSE_HEARTBEAT_SECONDS = 15.0
_SSE_POLL_SECONDS = 0.1
_SSE_COALESCE_SECONDS = 0.25
_SSE_MAX_CLIENTS = 32
_SSE_MAX_EVENT_BYTES = 256


def _open_read_only_store(path: str) -> SQLiteStore:
    """Open the Inspector database without schema setup or mutation privileges."""

    if path == ":memory:":
        raise ValueError("the concurrent Inspector requires a file-backed SQLite database")
    reader = SQLiteStore.__new__(SQLiteStore)
    reader.path = str(Path(path).expanduser())
    uri = f"{Path(reader.path).resolve().as_uri()}?mode=ro"
    reader._connection = sqlite3.connect(uri, uri=True, timeout=10.0)
    try:
        reader._connection.row_factory = sqlite3.Row
        reader._connection.execute("PRAGMA query_only = ON")
        reader._connection.execute("PRAGMA busy_timeout = 10000")
    except BaseException:
        reader._connection.close()
        raise
    return reader


class _FreshnessMonitor:
    """Coalesce SQLite commits into bounded, content-free freshness markers."""

    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._sequence = 0
        self._clients = 0
        self._closed = False
        self._failed = False
        self._thread = threading.Thread(
            target=self._run,
            name="fleet-inspector-freshness",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=2.0)

    @property
    def sequence(self) -> int:
        with self._condition:
            return self._sequence

    @property
    def running(self) -> bool:
        with self._condition:
            return not self._closed and not self._failed

    @property
    def client_count(self) -> int:
        with self._condition:
            return self._clients

    @property
    def thread_alive(self) -> bool:
        return self._thread.is_alive()

    def acquire_client(self) -> bool:
        with self._condition:
            if self._closed or self._failed or self._clients >= _SSE_MAX_CLIENTS:
                return False
            self._clients += 1
            return True

    def release_client(self) -> None:
        with self._condition:
            self._clients -= 1

    def wait(self, after: int, timeout: float) -> int | None:
        with self._condition:
            changed = self._condition.wait_for(
                lambda: self._sequence != after or self._closed or self._failed,
                timeout=timeout,
            )
            if not changed or self._sequence == after:
                return None
            return self._sequence

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            with _open_read_only_store(self._database_path) as reader:
                row = reader._connection.execute("PRAGMA data_version").fetchone()
                if row is None:
                    raise RuntimeError("SQLite did not return data_version")
                version = int(row[0])
                self._ready.set()
                while not self._stop.wait(_SSE_POLL_SECONDS):
                    row = reader._connection.execute("PRAGMA data_version").fetchone()
                    if row is None:
                        raise RuntimeError("SQLite did not return data_version")
                    observed = int(row[0])
                    if observed == version:
                        continue
                    version = observed
                    deadline = monotonic() + _SSE_COALESCE_SECONDS
                    while monotonic() < deadline and not self._stop.wait(_SSE_POLL_SECONDS):
                        row = reader._connection.execute("PRAGMA data_version").fetchone()
                        if row is None:
                            raise RuntimeError("SQLite did not return data_version")
                        version = int(row[0])
                    with self._condition:
                        if self._closed:
                            return
                        self._sequence += 1
                        self._condition.notify_all()
        except (OSError, sqlite3.Error, RuntimeError):
            with self._condition:
                self._failed = True
                self._condition.notify_all()
        finally:
            self._ready.set()


class _InspectorServer(ThreadingHTTPServer):
    """Threaded read-only server with one bounded database monitor."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], database_path: str) -> None:
        self.monitor: _FreshnessMonitor | None = None
        super().__init__(address, _InspectorHandler)
        self.database_path = database_path
        self.monitor = _FreshnessMonitor(database_path)

    def server_close(self) -> None:
        if self.monitor is not None:
            self.monitor.close()
        super().server_close()


class _InspectorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            self._serve_events()
            return
        if parsed.path == "/":
            self._send_body(_INDEX.encode(), "text/html; charset=utf-8")
            return

        server = cast(_InspectorServer, self.server)
        try:
            with _open_read_only_store(server.database_path) as store:
                if parsed.path == "/api/overview":
                    repository_id = parse_qs(parsed.query).get("repository_id", [None])[0]
                    body = canonical_json(inspect_fleet_runs(store, repository_id)).encode()
                elif parsed.path == "/api/runs":
                    repository_id = parse_qs(parsed.query).get("repository_id", [None])[0]
                    body = canonical_json(
                        {"runs": store.list_run_repositories(repository_id)}
                    ).encode()
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/explanation"):
                    run_id = parsed.path.removeprefix("/api/runs/").removesuffix("/explanation")
                    try:
                        from .run_explanation import explain_any_run

                        explanation = explain_any_run(store, run_id)
                        repository = store.repository_for_run(run_id)
                        if repository is not None:
                            explanation["repository_context"] = repository
                        body = canonical_json(explanation).encode()
                    except KeyError:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                elif parsed.path.startswith("/api/runs/"):
                    run_id = parsed.path.removeprefix("/api/runs/")
                    try:
                        body = canonical_json(inspect_any_run(store, run_id)).encode()
                    except KeyError:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
        except (OSError, sqlite3.Error):
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_body(body, "application/json")

    def _send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        server = cast(_InspectorServer, self.server)
        monitor = server.monitor
        if monitor is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if not monitor.acquire_client():
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(b"retry: 1000\n\n")
            self.wfile.flush()
            sequence = monitor.sequence
            while monitor.running:
                next_sequence = monitor.wait(sequence, _SSE_HEARTBEAT_SECONDS)
                if not monitor.running:
                    break
                if next_sequence is None:
                    event = b": heartbeat\n\n"
                else:
                    sequence = next_sequence
                    event = _freshness_event()
                self.wfile.write(event)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            monitor.release_client()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _freshness_event() -> bytes:
    payload = canonical_json({"type": "freshness"}).encode()
    event = b"event: freshness\ndata: " + payload + b"\n\n"
    if len(event) > _SSE_MAX_EVENT_BYTES:
        raise ValueError("Inspector freshness event exceeded its fixed bound")
    return event


def serve(store: SQLiteStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve a local read-only JSON/HTML Inspector until interrupted."""

    server = _InspectorServer((host, port), store.path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
