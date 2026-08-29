"""Read-only Inspector projection and tiny local HTTP server."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from pydantic import RootModel

from .domain import Artifact, ContextPackage, ExecutionMetrics, Node, Run, VerificationEvidence
from .domain.base import FrozenDict
from .domain.evaluation import EvaluationEvidenceLedger
from .domain.policy_v2 import PolicyLayer
from .domain.v2 import (
    AcceptanceLedger,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    DownloadResult,
    ExecutionResult,
    InstallResult,
    NonMutatingResultAcceptance,
    PolicyDecision,
    PromotionRecord,
    WorkerAvailability,
    WorkerResult,
    WorkspaceSnapshot,
)
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationRequest,
)
from .plan_review import (
    PlanReviewAcceptanceBinding,
    PlanReviewAction,
    PlanReviewAttempt,
    PlanRevisionAttempt,
)
from .serialization import canonical_json
from .storage import SQLiteStore
from .task_orchestration import (
    GoalEvaluatorRecord,
    GraphControlFact,
    GraphRunRecord,
    NodeEvaluatorRecord,
    NodeEvidenceRecord,
    NodeExecutionRecord,
    NodeReservationRecord,
    NodeRouteRecord,
    NodeSemanticAssessmentRecord,
    RetainedNodeBinding,
    StaleNodeResultRecord,
    TaskGraphAcceptance,
    _load_plan_review_history,
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


def inspect_graph_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
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
    return {
        "schema_version": "2",
        "run_id": run.id,
        "kind": "graph_run",
        "state": run.status,
        "generation": run.generation,
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
        "nodes": [_json_model(latest_nodes[node_id]) for node_id in sorted(latest_nodes)],
        "node_history": [_json_model(item) for item in node_records],
        "claims": list(store.graph_claims(run_id)),
        "reservations": [
            _json_model(item)
            for item in store.list_records(
                "node_reservation_v2", NodeReservationRecord, run_id=run_id
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
        "worker_results": [
            _json_model(store.get("worker_result_v2", item.worker_result_id, WorkerResult))
            for item in node_records
            if item.worker_result_id is not None
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
        "composition": None if composition is None else _json_model(composition),
        "candidate_patch": None if candidate is None else _json_model(candidate),
        "parent_evaluation": None if evaluation is None else _json_model(evaluation),
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
        "approval_requests": [
            _json_model(item)
            for item in store.list_records("approval_request_v2", ApprovalRequest, run_id=run_id)
        ],
        "approvals": [
            _json_model(item)
            for item in store.list_records("approval_v2", ApprovalRecord, run_id=run_id)
        ],
        "promotions": [
            _json_model(item)
            for item in store.list_records("promotion_v2", PromotionRecord, run_id=run_id)
        ],
    }


def inspect_any_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Read any runtime generation without mutating or migrating state."""

    try:
        return inspect_graph_run(store, run_id)
    except KeyError:
        try:
            return inspect_work_run(store, run_id)
        except KeyError:
            return inspect_run(store, run_id)


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


def serve(store: SQLiteStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve a local read-only JSON/HTML Inspector until interrupted."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.removeprefix("/api/runs/")
                try:
                    body = canonical_json(inspect_any_run(store, run_id)).encode()
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = "application/json"
            elif parsed.path == "/":
                body = _INDEX.encode()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    HTTPServer((host, port), Handler).serve_forever()


_INDEX = """<!doctype html><meta charset=utf-8><title>Fleet Inspector</title>
<style>
body{font:14px system-ui;max-width:1000px;margin:2rem auto}
input{width:24rem}pre{white-space:pre-wrap}
</style>
<h1>Fleet Inspector</h1><p>Read-only local projection</p>
<input id=r placeholder="run id"><button onclick="loadRun()">Inspect</button><pre id=o></pre>
<script>async function loadRun(){let r=document.querySelector('#r').value;
let x=await fetch('/api/runs/'+encodeURIComponent(r));document.querySelector('#o').textContent=
x.ok?JSON.stringify(await x.json(),null,2):'Run not found'}</script>"""
