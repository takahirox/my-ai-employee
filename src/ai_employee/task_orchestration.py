"""Graph-first task orchestration with proposed-graph execution safety fences."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import ClassVar, Literal, Protocol, Self

from pydantic import ConfigDict, Field, model_validator
from pydantic.main import BaseModel

from .domain import (
    AcceptedGraphRevision,
    Budget,
    CompletionCriterion,
    EvaluationDecision,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
    TaskAssessment,
)
from .domain.base import Digest, Identifier, freeze_json
from .domain.v2 import CriterionEvidence, DigestedRecordV2, WorkerRequest, WorkerResult
from .graph import accept_task_graph
from .graph_composition import NodePatchArtifact
from .routing import RoutingError, assess_task, select_strategy
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_planning import ProposedGraph

NodeExecutionStatus = Literal["pending", "routed", "running", "passed", "failed", "blocked"]
GraphExecutionStatus = Literal["planned", "running", "completed", "ready_to_promote", "failed"]


class TaskGraphAcceptance(DigestedRecordV2):
    schema_name: ClassVar[str] = "task_graph_acceptance"
    accepted_revision: AcceptedGraphRevision
    effective_policy_digest: Digest
    harness_digest: Digest
    previous_revision_digest: Digest | None = None
    proposed_graph_digest: Digest | None = None

    @model_validator(mode="after")
    def _first_slice_has_no_revision_parent(self) -> Self:
        if self.accepted_revision.revision_number != 1 or self.previous_revision_digest is not None:
            raise ValueError("the initial orchestration slice does not support re-planning")
        return self


class NodeRouteRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_route_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    assessment: TaskAssessment
    eligible_strategy_ids: tuple[Identifier, ...] = Field(min_length=1)
    selected_strategy: ExecutionStrategy
    effective_policy_digest: Digest
    harness_digest: Digest


class NodeEvidenceRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_evidence_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    criteria: tuple[CriterionEvidence, ...]


class NodeEvaluatorRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_evaluator_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    worker_result_digest: Digest
    evidence_digest: Digest
    decision: EvaluationDecision


class GoalEvaluatorRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "goal_evaluator_record"
    goal_id: Identifier
    accepted_graph_revision_digest: Digest
    evidence_digests: tuple[Digest, ...]
    decision: EvaluationDecision


class NodeExecutionRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "node_execution_record"
    node_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    sequence: int = Field(ge=0)
    status: NodeExecutionStatus
    route_digest: Digest | None = None
    worker_request_digest: Digest | None = None
    worker_result_digest: Digest | None = None
    evidence_digest: Digest | None = None
    evaluator_digest: Digest | None = None
    evaluator_decision: EvaluationDecision | None = None
    failure_code: str | None = None
    workspace_id: Identifier | None = None
    workspace_digest: Digest | None = None
    work_run_id: Identifier | None = None
    patch_artifact_id: Identifier | None = None
    patch_descriptor_digest: Digest | None = None
    patch_digest: Digest | None = None
    acceptance_ledger_digest: Digest | None = None
    verification_result_digests: tuple[Digest, ...] = ()


class GraphRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: Identifier
    goal_id: Identifier
    accepted_graph_revision_digest: Digest
    status: GraphExecutionStatus
    max_concurrency: int = Field(ge=1)
    max_claims: int = Field(ge=1)
    generation: int = Field(default=0, ge=0)
    goal_evaluator_digest: Digest | None = None
    failure_code: str | None = None
    composition_id: Identifier | None = None
    composition_digest: Digest | None = None
    parent_candidate_artifact_id: Identifier | None = None
    parent_candidate_digest: Digest | None = None
    parent_evaluation_id: Identifier | None = None
    parent_evaluation_digest: Digest | None = None


class NodeExecutionResult(BaseModel):
    """Typed facts returned by a node worker boundary, before scheduler evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    worker_result: WorkerResult
    criterion_evidence: tuple[CriterionEvidence, ...]
    workspace_id: Identifier | None = None
    node_patch: NodePatchArtifact | None = None

    @model_validator(mode="after")
    def _criterion_evidence_is_unique(self) -> Self:
        ids = tuple(item.criterion_id for item in self.criterion_evidence)
        if len(ids) != len(set(ids)):
            raise ValueError("criterion evidence must be unique per node result")
        if self.node_patch is not None and self.workspace_id != self.node_patch.workspace.id:
            raise ValueError("node patch and execution workspace must match")
        return self


class NodeRunner(Protocol):
    def __call__(
        self,
        node: Node,
        request: WorkerRequest,
        strategy: ExecutionStrategy,
    ) -> NodeExecutionResult: ...


class GraphReplay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    run: GraphRunRecord
    acceptance: TaskGraphAcceptance
    nodes: tuple[NodeExecutionRecord, ...]
    claims: tuple[Identifier, ...]
    route_count: int
    worker_result_count: int
    evidence_count: int
    evaluator_count: int
    worker_invocations: Literal[0] = 0
    verification_invocations: Literal[0] = 0
    composition_invocations: Literal[0] = 0
    promotion_invocations: Literal[0] = 0


def one_node_graph(
    goal: Goal,
    *,
    graph_id: Identifier,
    node_id: Identifier,
    required_capabilities: tuple[Identifier, ...] = (),
    max_wall_seconds: float = 3600.0,
) -> Graph:
    """Represent the compatibility path as the degenerate accepted task DAG."""

    criteria = goal.completion_criteria or (
        CompletionCriterion(
            id=f"criterion-{node_id}",
            description="the node-bound worker result is accepted",
        ),
    )
    node = Node(
        id=node_id,
        kind=NodeKind.FUNCTION,
        name="Single task",
        objective=goal.statement,
        output_contract=OutputContract(id=f"contract-{node_id}"),
        required_capabilities=required_capabilities,
        completion_criteria=criteria,
    )
    return Graph(
        id=graph_id,
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(
            max_attempts=1,
            max_nodes=1,
            max_wall_seconds=max_wall_seconds,
        ),
    )


class TaskOrchestrator:
    """Accept task DAGs and fence uncomposed planner output from execution."""

    def __init__(
        self,
        store: SQLiteStore,
        runner: NodeRunner,
        strategies: Iterable[ExecutionStrategy],
        *,
        max_concurrency: int = 2,
        routing_mode: RoutingMode = RoutingMode.ADAPTIVE,
        allowed_strategy_ids: Iterable[str] = (),
        allowed_backends: Iterable[str] = (),
        local_backend_allowed: bool = False,
        bounded_graph_execution: bool = False,
        defer_parent_evaluation: bool = False,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.store = store
        self.runner = runner
        self.strategies = tuple(sorted(strategies, key=lambda item: item.id))
        if not self.strategies:
            raise ValueError("at least one explicitly configured strategy is required")
        self.max_concurrency = max_concurrency
        self.routing_mode = routing_mode
        self.allowed_strategy_ids = tuple(allowed_strategy_ids) or tuple(
            item.id for item in self.strategies
        )
        self.allowed_backends = tuple(allowed_backends) or tuple(
            dict.fromkeys(item.backend for item in self.strategies)
        )
        self.local_backend_allowed = local_backend_allowed
        if bounded_graph_execution and not defer_parent_evaluation:
            raise ValueError("bounded graph execution must defer unavailable parent evaluation")
        self.bounded_graph_execution = bounded_graph_execution
        self.defer_parent_evaluation = defer_parent_evaluation

    def run(
        self,
        goal: Goal,
        proposed_graph: Graph | ProposedGraph,
        policy: ExecutionPolicy,
        *,
        harness_digest: Digest,
        effective_policy_digest: Digest,
        run_id: Identifier,
        available_capabilities: Iterable[str],
        plan_only: bool = False,
    ) -> GraphRunRecord:
        if isinstance(proposed_graph, ProposedGraph):
            proposal: ProposedGraph | None = proposed_graph
            candidate: Graph = proposed_graph.graph
        else:
            proposal = None
            candidate = proposed_graph
        configured_planners = {item.id: item for item in self.strategies}
        if proposal is not None and (
            proposal.run_id != run_id
            or proposal.goal_id != goal.id
            or proposal.goal_digest != canonical_digest(goal)
            or configured_planners.get(proposal.planner_strategy.id) != proposal.planner_strategy
            or proposal.planner_strategy.backend in {"ollama", "ollama_cli"}
            or proposal.effective_policy_digest != effective_policy_digest
            or proposal.harness_digest != harness_digest
        ):
            raise ValueError("ProposedGraph provenance does not match this run")
        accepted = accept_task_graph(
            candidate,
            policy,
            available_capabilities=available_capabilities,
        )
        if proposal is not None:
            self.store.put("proposed_graph_v2", proposal, run_id=run_id)
        acceptance = TaskGraphAcceptance(
            id=identifier("graph-acceptance"),
            run_id=run_id,
            created_at=now(),
            accepted_revision=accepted,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
            proposed_graph_digest=(None if proposal is None else proposal.content_digest),
        )
        graph_digest = _required_digest(accepted.content_digest)
        self.store.save_graph(run_id, accepted)
        self.store.put("task_graph_acceptance_v2", acceptance, run_id=run_id)
        max_claims = min(candidate.budget.max_attempts, policy.max_attempts)
        graph_run = GraphRunRecord(
            id=run_id,
            goal_id=goal.id,
            accepted_graph_revision_digest=graph_digest,
            status="planned" if plan_only else "running",
            max_concurrency=self.max_concurrency,
            max_claims=max_claims,
        )
        self._save_run(graph_run)

        nodes = {node.id: node for node in candidate.nodes}
        predecessors: dict[str, tuple[str, ...]] = {}
        inbound: dict[str, list[str]] = defaultdict(list)
        for edge in candidate.edges:
            inbound[edge.target_id].append(edge.source_id)
        for node_id in nodes:
            predecessors[node_id] = tuple(sorted(inbound[node_id]))

        records = {
            node.id: NodeExecutionRecord(
                id=identifier("node-execution"),
                run_id=run_id,
                created_at=now(),
                node_id=node.id,
                accepted_graph_revision_digest=graph_digest,
                generation=node.generation,
                attempt=node.attempt,
                sequence=0,
                status="pending",
            )
            for node in candidate.nodes
        }
        for record in records.values():
            self._save_node(record)
        if plan_only:
            return graph_run
        if proposal is not None and not self.bounded_graph_execution:
            graph_run = graph_run.model_copy(
                update={
                    "status": "failed",
                    "generation": graph_run.generation + 1,
                    "failure_code": "GRAPH_EXECUTION_UNAVAILABLE",
                }
            )
            self._save_run(graph_run)
            return graph_run

        evidence_by_node: dict[str, NodeEvidenceRecord] = {}
        active: dict[Future[NodeExecutionResult], tuple[str, WorkerRequest]] = {}
        with ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix=f"fleet-{run_id[:24]}",
        ) as pool:
            while active or any(item.status == "pending" for item in records.values()):
                for node_id in sorted(nodes):
                    record = records[node_id]
                    if record.status != "pending":
                        continue
                    if any(
                        records[parent].status in {"failed", "blocked"}
                        for parent in predecessors[node_id]
                    ):
                        records[node_id] = self._advance(
                            record,
                            status="blocked",
                            failure_code="PREDECESSOR_NOT_PASS",
                        )

                ready = [
                    node_id
                    for node_id in sorted(nodes)
                    if records[node_id].status == "pending"
                    and all(records[parent].status == "passed" for parent in predecessors[node_id])
                ]
                while ready and len(active) < self.max_concurrency:
                    node_id = ready.pop(0)
                    node = nodes[node_id]
                    try:
                        route = self._route(
                            run_id,
                            node,
                            graph_digest,
                            effective_policy_digest,
                            harness_digest,
                        )
                    except RoutingError:
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="NO_ELIGIBLE_STRATEGY",
                        )
                        continue
                    self.store.put("node_route_v2", route, run_id=run_id)
                    records[node_id] = self._advance(
                        records[node_id],
                        status="routed",
                        route_digest=route.content_digest,
                    )
                    if not self.store.claim_graph_node(
                        run_id,
                        node_id,
                        max_claims=max_claims,
                    ):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="DUPLICATE_OR_BUDGETED_CLAIM",
                        )
                        continue
                    prior_results = tuple(
                        _required_digest(records[parent].worker_result_digest)
                        for parent in predecessors[node_id]
                    )
                    prior_artifacts = tuple(
                        digest
                        for parent in predecessors[node_id]
                        if (digest := records[parent].patch_digest) is not None
                    )
                    if self.bounded_graph_execution and len(prior_artifacts) != len(
                        predecessors[node_id]
                    ):
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code="PREDECESSOR_ARTIFACT_UNAVAILABLE",
                        )
                        continue
                    request = WorkerRequest(
                        id=identifier("worker-request"),
                        run_id=_node_worker_run_id(run_id, node),
                        created_at=now(),
                        goal=node.objective or node.name,
                        accepted_plan_digest=graph_digest,
                        node_id=node.id,
                        accepted_graph_revision_digest=graph_digest,
                        graph_run_id=run_id,
                        generation=node.generation,
                        attempt=node.attempt,
                        harness_digest=harness_digest,
                        effective_policy_digest=effective_policy_digest,
                        remaining_budgets=freeze_json(
                            {
                                "worker_turns": 1,
                                "aggregate_claims": max_claims
                                - len(self.store.graph_claims(run_id)),
                            }
                        ),
                        prior_result_digests=prior_results,
                        prior_artifact_digests=prior_artifacts,
                    )
                    self.store.put("worker_request_v2", request, run_id=run_id)
                    records[node_id] = self._advance(
                        records[node_id],
                        status="running",
                        worker_request_digest=request.content_digest,
                    )
                    future = pool.submit(self.runner, node, request, route.selected_strategy)
                    active[future] = (node_id, request)

                if not active:
                    if any(item.status == "pending" for item in records.values()):
                        for node_id in sorted(nodes):
                            if records[node_id].status == "pending":
                                records[node_id] = self._advance(
                                    records[node_id],
                                    status="blocked",
                                    failure_code="NO_READY_NODE",
                                )
                    continue
                completed, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(completed, key=lambda item: active[item][0]):
                    node_id, request = active.pop(future)
                    node = nodes[node_id]
                    try:
                        result = future.result()
                        self._persist_result(
                            node,
                            request,
                            result,
                            records,
                            evidence_by_node,
                            graph_digest,
                        )
                    except Exception as error:
                        records[node_id] = self._advance(
                            records[node_id],
                            status="failed",
                            failure_code=f"WORKER_BOUNDARY:{type(error).__name__}",
                        )

        node_pass = all(item.status == "passed" for item in records.values())
        if self.defer_parent_evaluation:
            graph_run = graph_run.model_copy(
                update={
                    "status": "failed",
                    "generation": graph_run.generation + 1,
                    "failure_code": (
                        "PARENT_EVALUATION_UNAVAILABLE" if node_pass else "NODE_EXECUTION_FAILED"
                    ),
                }
            )
            self._save_run(graph_run)
            return graph_run

        all_evidence = tuple(evidence_by_node[node_id] for node_id in sorted(evidence_by_node))
        goal_decision = (
            _evaluate_criteria(
                goal.completion_criteria,
                tuple(item for record in all_evidence for item in record.criteria),
            )
            if node_pass
            else EvaluationDecision.FAIL
        )
        goal_evaluation = GoalEvaluatorRecord(
            id=identifier("goal-evaluation"),
            run_id=run_id,
            created_at=now(),
            goal_id=goal.id,
            accepted_graph_revision_digest=graph_digest,
            evidence_digests=tuple(_required_digest(item.content_digest) for item in all_evidence),
            decision=goal_decision,
        )
        self.store.put("goal_evaluator_v2", goal_evaluation, run_id=run_id)
        terminal_pass = all(
            records[node_id].status == "passed" for node_id in candidate.terminal_node_ids
        )
        completed_ok = node_pass and terminal_pass and goal_decision is EvaluationDecision.PASS
        graph_run = graph_run.model_copy(
            update={
                "status": "completed" if completed_ok else "failed",
                "generation": graph_run.generation + 1,
                "goal_evaluator_digest": goal_evaluation.content_digest,
                "failure_code": None if completed_ok else "GOAL_OR_NODE_EVALUATION_FAILED",
            }
        )
        self._save_run(graph_run)
        return graph_run

    def replay(self, run_id: Identifier) -> GraphReplay:
        """Reconstruct accepted orchestration facts without workers or services."""

        run = self.store.get("graph_run_v2", run_id, GraphRunRecord)
        acceptance = self.store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )[-1]
        history = self.store.list_records("node_execution_v2", NodeExecutionRecord, run_id=run_id)
        latest: dict[str, NodeExecutionRecord] = {}
        for record in history:
            if record.node_id not in latest or latest[record.node_id].sequence < record.sequence:
                latest[record.node_id] = record
        return GraphReplay(
            run=run,
            acceptance=acceptance,
            nodes=tuple(latest[node_id] for node_id in sorted(latest)),
            claims=self.store.graph_claims(run_id),
            route_count=len(
                self.store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)
            ),
            worker_result_count=sum(
                item.worker_result_digest is not None for item in latest.values()
            ),
            evidence_count=sum(item.evidence_digest is not None for item in latest.values()),
            evaluator_count=sum(item.evaluator_digest is not None for item in latest.values()),
        )

    def _route(
        self,
        run_id: str,
        node: Node,
        graph_digest: str,
        effective_policy_digest: str,
        harness_digest: str,
    ) -> NodeRouteRecord:
        assessment = assess_task(
            node.objective or node.name,
            run_id=run_id,
            risk=node.risk,
            required_capabilities=node.required_capabilities,
        ).model_copy(update={"complexity": node.complexity, "scale": node.scale})
        allowed_ids = set(self.allowed_strategy_ids)
        allowed_backends = set(self.allowed_backends)
        required = set(node.required_capabilities)
        eligible = tuple(
            strategy
            for strategy in self.strategies
            if strategy.id in allowed_ids
            and strategy.backend in allowed_backends
            and (strategy.backend not in {"ollama", "ollama_cli"} or self.local_backend_allowed)
            and required <= set(strategy.capabilities)
            and strategy.min_complexity <= assessment.complexity <= strategy.max_complexity
            and strategy.min_scale <= assessment.scale <= strategy.max_scale
            and assessment.risk <= strategy.max_risk
        )
        if not eligible:
            raise RoutingError("no strategy satisfies node assessment and policy")
        selected = select_strategy(
            eligible,
            mode=self.routing_mode,
            required_capabilities=node.required_capabilities,
            strategy_capabilities={item.id: item.capabilities for item in eligible},
            assessment=assessment,
            allowed_strategy_ids=tuple(item.id for item in eligible),
            allowed_backends=tuple(dict.fromkeys(item.backend for item in eligible)),
            local_backend_allowed=self.local_backend_allowed,
        )
        return NodeRouteRecord(
            id=identifier("node-route"),
            run_id=run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            assessment=assessment,
            eligible_strategy_ids=tuple(item.id for item in eligible),
            selected_strategy=selected,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
        )

    def _persist_result(
        self,
        node: Node,
        request: WorkerRequest,
        result: NodeExecutionResult,
        records: dict[str, NodeExecutionRecord],
        evidence_by_node: dict[str, NodeEvidenceRecord],
        graph_digest: str,
    ) -> None:
        worker_result = result.worker_result
        patch = result.node_patch
        if worker_result.run_id != request.run_id:
            raise ValueError("worker result belongs to another run")
        if worker_result.request_digest != request.content_digest:
            raise ValueError("worker result is not bound to its node request")
        self.store.put("worker_result_v2", worker_result, run_id=request.run_id)
        evidence = NodeEvidenceRecord(
            id=identifier("node-evidence"),
            run_id=request.run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            criteria=result.criterion_evidence,
        )
        self.store.put("node_evidence_v2", evidence, run_id=request.run_id)
        evidence_by_node[node.id] = evidence
        decision = (
            _evaluate_criteria(node.completion_criteria, result.criterion_evidence)
            if worker_result.status == "succeeded"
            else EvaluationDecision.FAIL
        )
        evaluation = NodeEvaluatorRecord(
            id=identifier("node-evaluation"),
            run_id=request.run_id,
            created_at=now(),
            node_id=node.id,
            accepted_graph_revision_digest=graph_digest,
            generation=node.generation,
            attempt=node.attempt,
            worker_result_digest=_required_digest(worker_result.content_digest),
            evidence_digest=_required_digest(evidence.content_digest),
            decision=decision,
        )
        self.store.put("node_evaluator_v2", evaluation, run_id=request.run_id)
        records[node.id] = self._advance(
            records[node.id],
            status="passed" if decision is EvaluationDecision.PASS else "failed",
            worker_result_digest=worker_result.content_digest,
            evidence_digest=evidence.content_digest,
            evaluator_digest=evaluation.content_digest,
            evaluator_decision=decision,
            failure_code=(
                None if decision is EvaluationDecision.PASS else "NODE_EVALUATION_NOT_PASS"
            ),
            workspace_id=result.workspace_id,
            workspace_digest=(None if patch is None else patch.workspace.content_digest),
            work_run_id=(None if patch is None else patch.workspace.run_id),
            patch_artifact_id=(None if patch is None else patch.patch.id),
            patch_descriptor_digest=(None if patch is None else patch.patch.content_digest),
            patch_digest=(None if patch is None else patch.patch.artifact_digest),
            acceptance_ledger_digest=(None if patch is None else patch.acceptance_ledger_digest),
            verification_result_digests=(
                () if patch is None else patch.verification_result_digests
            ),
        )

    def _advance(self, record: NodeExecutionRecord, **changes: object) -> NodeExecutionRecord:
        payload = record.model_dump(mode="python")
        payload.update(changes)
        payload.update({"sequence": record.sequence + 1, "content_digest": None})
        updated = NodeExecutionRecord.model_validate(payload, strict=True)
        self._save_node(updated)
        return updated

    def _save_node(self, record: NodeExecutionRecord) -> None:
        self.store.put(
            "node_execution_v2",
            record,
            run_id=record.run_id,
            revision=record.sequence + 1,
        )

    def _save_run(self, run: GraphRunRecord) -> None:
        self.store.put("graph_run_v2", run, run_id=run.id, revision=run.generation + 1)


def _evaluate_criteria(
    criteria: tuple[CompletionCriterion, ...],
    evidence: tuple[CriterionEvidence, ...],
) -> EvaluationDecision:
    by_id = {item.criterion_id: item for item in evidence}
    mandatory = tuple(item for item in criteria if item.mandatory)
    if any(item.id not in by_id for item in mandatory):
        return EvaluationDecision.FAIL
    if any(by_id[item.id].disposition != "satisfied" for item in mandatory):
        return EvaluationDecision.FAIL
    return EvaluationDecision.PASS


def _required_digest(value: str | None) -> str:
    if value is None:
        raise ValueError("persisted digested record is missing its digest")
    return value


def _node_worker_run_id(graph_run_id: str, node: Node) -> str:
    digest = canonical_digest(
        {
            "graph_run_id": graph_run_id,
            "node_id": node.id,
            "generation": node.generation,
            "attempt": node.attempt,
        }
    )
    return f"node-{digest[:32]}"
