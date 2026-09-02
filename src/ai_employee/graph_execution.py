"""Bounded accepted-graph execution over authoritative WorkCoordinator runs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Protocol, cast

from .domain import ExecutionPolicy, ExecutionStrategy, Goal, Graph, Node, RoutingMode
from .domain.base import Digest, Identifier
from .domain.models import AcceptedGraphRevision
from .domain.services_v2 import ApprovalService, Cancellation
from .domain.v2 import (
    AcceptanceLedger,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    DecisionOutcome,
    ExecutionResult,
    NodeVerificationBinding,
    NonMutatingResultAcceptance,
    PolicyDecision,
    StableFailureCode,
    WorkerRequest,
    WorkerResult,
    WorkspaceSnapshot,
)
from .graph_composition import (
    GraphPatchCompositionRecord,
    GraphPatchCompositionRequest,
    NodePatchArtifact,
)
from .graph_evaluation import ParentCandidateEvaluationRecord, ParentCandidateEvaluationReplay
from .orchestration import (
    WorkCoordinator,
    WorkRun,
    _acceptance_evidence_is_authoritative,
    _AcceptedNonMutatingResultSource,
    _authoritative_node_verification_results,
    _mandatory_acceptance_is_satisfied,
    _node_verification_configuration_is_valid,
    _persisted_node_verification_configuration_is_valid,
)
from .promotion_approval import (
    PromotionApprovalTrustKernel,
    PromotionPolicyDecision,
    validate_exact_parent_evidence_store,
)
from .serialization import canonical_json
from .services_v2 import DigestApprovalService
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_orchestration import (
    GraphReplay,
    GraphRunRecord,
    NodeAssessor,
    NodeExecutionResult,
    NodePatchRecord,
    NodeRunner,
    PlanReviewer,
    PlanReviser,
    TaskGraphAcceptance,
    TaskOrchestrator,
    TaskResultReviewer,
)
from .task_planning import ProposedGraph
from .task_review import TaskReviewSeverity
from .worker_supervision import WorkerSupervisionPolicy


class CoordinatorFactory(Protocol):
    """Create a node-scoped coordinator whose store is owned by the caller."""

    def __call__(
        self, node: Node, request: WorkerRequest, strategy: ExecutionStrategy
    ) -> WorkCoordinator: ...


class PatchComposer(Protocol):
    def compose(
        self, request: GraphPatchCompositionRequest, cancellation: Cancellation
    ) -> GraphPatchCompositionRecord: ...


class ParentCandidateEvaluator(Protocol):
    def evaluate(
        self,
        goal: Goal,
        accepted_revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        *,
        harness_digest: Digest,
        effective_policy_digest: Digest,
        cancellation: Cancellation,
    ) -> ParentCandidateEvaluationRecord: ...

    def replay(self, evaluation_id: Identifier) -> ParentCandidateEvaluationReplay: ...


class _NeverCancelled:
    def cancelled(self) -> bool:
        return False


class _ExecutionSession:
    def __init__(
        self,
        factory: CoordinatorFactory,
        repository: str,
        base_commit: str,
    ) -> None:
        self.factory = factory
        self.repository = repository
        self.base_commit = base_commit
        self.node_patches: dict[str, NodePatchArtifact] = {}
        self._lock = Lock()

    def run_node(
        self,
        node: Node,
        request: WorkerRequest,
        strategy: ExecutionStrategy,
    ) -> NodeExecutionResult:
        coordinator = self.factory(node, request, strategy)
        try:
            if coordinator.selected_strategy != strategy:
                raise ValueError("node coordinator is not bound to the routed strategy")
            run = coordinator.execute_node(
                request,
                node.completion_criteria,
                self.repository,
                self.base_commit,
                worker_name=strategy.backend,
                capture_patch="edit_intent" in node.required_capabilities,
            )
            result = _authoritative_node_result(coordinator, node, request, run)
        finally:
            coordinator.store.close()
        if result.node_patch is not None:
            patch = result.node_patch
            with self._lock:
                if patch.node_id != node.id:
                    raise ValueError("node patch identity does not match executed node")
                previous = self.node_patches.get(node.id)
                if previous is not None and (
                    patch.node_id != previous.node_id
                    or (
                        patch.accepted_graph_revision_digest
                        != previous.accepted_graph_revision_digest
                    )
                    or patch.generation != previous.generation
                    or patch.attempt <= previous.attempt
                ):
                    raise ValueError("node patch was produced more than once")
                self.node_patches[node.id] = patch
        return result


class GraphExecutionService:
    """Schedule accepted nodes, compose exact patches, and stop before parent evaluation."""

    def __init__(
        self,
        store: SQLiteStore,
        coordinator_factory: CoordinatorFactory,
        composer: PatchComposer | None,
        strategies: Iterable[ExecutionStrategy],
        *,
        repository: str,
        base_commit: str,
        max_concurrency: int = 2,
        generated_paths: Iterable[str] = (),
        routing_mode: RoutingMode = RoutingMode.ADAPTIVE,
        fixed_strategy_id: Identifier | None = None,
        allowed_strategy_ids: Iterable[str] = (),
        allowed_backends: Iterable[str] = (),
        local_backend_allowed: bool = False,
        parent_evaluator: ParentCandidateEvaluator | None = None,
        approval_service: ApprovalService | None = None,
        promotion_approval_policy: PromotionApprovalTrustKernel | None = None,
        operator_config_digest: Digest | None = None,
        operator_config_path: str | None = None,
        strategy_set: Identifier | None = None,
        plan_reviewer: PlanReviewer | None = None,
        plan_reviser: PlanReviser | None = None,
        node_assessor: NodeAssessor | None = None,
        routing_risk_floor: int = 0,
        independent_node_assessment: bool = False,
        task_reviewer: TaskResultReviewer | None = None,
        independent_task_review: bool = False,
        task_review_block_severities: Iterable[TaskReviewSeverity] = (
            TaskReviewSeverity.CRITICAL,
            TaskReviewSeverity.HIGH,
        ),
        worker_supervision_policy: WorkerSupervisionPolicy | None = None,
        adapter_timeout_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.coordinator_factory = coordinator_factory
        self.composer = composer
        self.strategies = tuple(strategies)
        self.repository = str(Path(repository).resolve())
        self.base_commit = base_commit
        self.max_concurrency = max_concurrency
        self.generated_paths = tuple(generated_paths)
        self.routing_mode = routing_mode
        self.fixed_strategy_id = fixed_strategy_id
        self.allowed_strategy_ids = tuple(allowed_strategy_ids)
        self.allowed_backends = tuple(allowed_backends)
        self.local_backend_allowed = local_backend_allowed
        self.operator_config_digest = operator_config_digest
        self.operator_config_path = operator_config_path
        self.strategy_set = strategy_set
        self.parent_evaluator = parent_evaluator
        self.plan_reviewer = plan_reviewer
        self.plan_reviser = plan_reviser
        self.node_assessor = node_assessor
        self.routing_risk_floor = routing_risk_floor
        self.independent_node_assessment = independent_node_assessment
        self.task_reviewer = task_reviewer
        self.independent_task_review = independent_task_review
        self.task_review_block_severities = tuple(task_review_block_severities)
        self.worker_supervision_policy = worker_supervision_policy
        self.adapter_timeout_seconds = adapter_timeout_seconds
        self.approval_service = approval_service or DigestApprovalService(
            store, operator_label="local-operator"
        )
        self.promotion_approval_policy = promotion_approval_policy

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
        resume: bool = False,
        replan: bool = False,
    ) -> GraphRunRecord:
        capabilities = tuple(available_capabilities)
        session = _ExecutionSession(self.coordinator_factory, self.repository, self.base_commit)
        orchestrator = self._orchestrator(session.run_node)
        graph_run = orchestrator.run(
            goal,
            proposed_graph,
            policy,
            harness_digest=harness_digest,
            effective_policy_digest=effective_policy_digest,
            run_id=run_id,
            available_capabilities=capabilities,
            plan_only=plan_only,
            resume=resume,
            replan=replan,
        )
        recovered = self._recover_policy_approval_pointer(graph_run)
        if recovered is not None:
            return recovered
        if plan_only or graph_run.failure_code != "PARENT_EVALUATION_UNAVAILABLE":
            return graph_run

        current_digest = graph_run.accepted_graph_revision_digest
        replay = orchestrator.replay(run_id)
        latest_nodes = {item.node_id: item for item in replay.nodes}
        for record in self.store.list_records("node_patch_v2", NodePatchRecord, run_id=run_id):
            patch = record.node_patch
            node_record = latest_nodes.get(patch.node_id)
            if (
                node_record is not None
                and node_record.status == "passed"
                and patch.accepted_graph_revision_digest == current_digest
                and patch.generation == node_record.output_generation
                and patch.attempt == node_record.attempt
                and patch.worker_request_digest == node_record.worker_request_digest
                and patch.patch.id == node_record.patch_artifact_id
                and patch.patch.content_digest == node_record.patch_descriptor_digest
                and patch.patch.artifact_digest == node_record.patch_digest
            ):
                session.node_patches[patch.node_id] = patch
        acceptance = replay.acceptance
        writing_nodes = tuple(
            node
            for node in acceptance.accepted_revision.graph.nodes
            if "edit_intent" in node.required_capabilities
        )
        expected = {node.id for node in writing_nodes}
        if not expected or not expected <= set(session.node_patches):
            return self._update_run(
                graph_run,
                failure_code="NODE_PATCH_UNAVAILABLE",
            )
        if set(session.node_patches) != expected:
            return self._update_run(
                graph_run,
                failure_code="STALE_OR_UNEXPECTED_NODE_PATCH",
            )
        if self.composer is None:
            return self._update_run(graph_run, failure_code="GRAPH_PATCH_COMPOSER_UNAVAILABLE")
        composition_request = GraphPatchCompositionRequest(
            id=identifier("graph-patch-composition-request"),
            run_id=run_id,
            created_at=now(),
            accepted_revision=acceptance.accepted_revision,
            repository=self.repository,
            base_commit=self.base_commit,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
            generated_paths=self.generated_paths,
            node_patches=tuple(session.node_patches[node.id] for node in writing_nodes),
        )
        composition = self.composer.compose(composition_request, _NeverCancelled())
        if composition.status != "succeeded" or composition.candidate_patch is None:
            return self._update_run(
                graph_run,
                failure_code="GRAPH_PATCH_COMPOSITION_FAILED",
                composition_id=composition.id,
                composition_digest=composition.content_digest,
            )
        candidate_fields = {
            "composition_id": composition.id,
            "composition_digest": composition.content_digest,
            "parent_candidate_artifact_id": composition.candidate_patch.id,
            "parent_candidate_digest": composition.candidate_patch.artifact_digest,
        }
        if self.parent_evaluator is None:
            return self._update_run(
                graph_run,
                failure_code="PARENT_EVALUATION_UNAVAILABLE",
                **candidate_fields,
            )
        try:
            evaluation = self.parent_evaluator.evaluate(
                goal,
                acceptance.accepted_revision,
                composition,
                harness_digest=harness_digest,
                effective_policy_digest=effective_policy_digest,
                cancellation=_NeverCancelled(),
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return self._update_run(
                graph_run,
                failure_code="PARENT_EVALUATION_UNAVAILABLE",
                **candidate_fields,
            )
        evaluation_fields = {
            **candidate_fields,
            "parent_evaluation_id": evaluation.id,
            "parent_evaluation_digest": evaluation.content_digest,
            "goal_evaluator_digest": evaluation.goal_evaluator_digest,
        }
        if evaluation.status != "ready_to_promote":
            failed_run = self._update_run(
                graph_run,
                failure_code=evaluation.failure_code or "PARENT_EVALUATION_FAILED",
                **evaluation_fields,
            )
            evaluation_digest = evaluation.content_digest
            if evaluation_digest is not None and orchestrator.prepare_parent_repair(
                run_id, evaluation_digest
            ):
                return self.run(
                    goal,
                    acceptance.accepted_revision.graph,
                    policy,
                    harness_digest=harness_digest,
                    effective_policy_digest=effective_policy_digest,
                    run_id=run_id,
                    available_capabilities=capabilities,
                    resume=True,
                )
            return self.store.get("graph_run_v2", failed_run.id, GraphRunRecord)
        approval_created_at = now()
        approval_decision = PolicyDecision(
            id=identifier("graph-promotion-policy"),
            run_id=run_id,
            created_at=approval_created_at,
            request_digest=composition.candidate_patch.artifact_digest,
            effective_policy_digest=effective_policy_digest,
            outcome=DecisionOutcome.APPROVAL_REQUIRED,
            reason_code="explicit_graph_promotion_approval",
            required_approval_classes=("promotion",),
        )
        approval_request = ApprovalRequest(
            id=identifier("graph-promotion-approval-request"),
            run_id=run_id,
            created_at=approval_created_at,
            request_digest=composition.candidate_patch.artifact_digest,
            policy_digest=effective_policy_digest,
            approval_classes=("promotion",),
            expires_at=approval_created_at + timedelta(hours=1),
        )
        self.store.put("policy_decision_v2", approval_decision, run_id=run_id)
        self.store.put("approval_request_v2", approval_request, run_id=run_id)
        promotion_policy_decision = None
        if self.promotion_approval_policy is not None:
            parent_replay: ParentCandidateEvaluationReplay | None = None
            evidence_storage_valid = False
            if (
                self.promotion_approval_policy.operator_policy.mode == "policy"
                and self.promotion_approval_policy.harness.approvals.promotion == "policy"
            ):
                try:
                    parent_replay = self.parent_evaluator.replay(evaluation.id)
                except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                    parent_replay = None
                if parent_replay is not None:
                    try:
                        exact_replay = validate_exact_parent_evidence_store(
                            self.store,
                            graph_run.model_copy(update=evaluation_fields),
                            acceptance.accepted_revision,
                            evaluation,
                            self.promotion_approval_policy.harness,
                        )
                        evidence_storage_valid = parent_replay == exact_replay
                    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                        evidence_storage_valid = False
            promotion_policy_decision = self.promotion_approval_policy.resolve(
                graph_run.model_copy(update=evaluation_fields),
                acceptance.accepted_revision,
                composition,
                evaluation,
                parent_replay,
                evidence_storage_valid=evidence_storage_valid,
            )
            self.store.put(
                "promotion_policy_decision_v2",
                promotion_policy_decision,
                run_id=run_id,
            )
        try:
            if (
                promotion_policy_decision is not None
                and promotion_policy_decision.decision == "policy_auto_approved"
                and isinstance(self.approval_service, DigestApprovalService)
            ):
                try:
                    approval = self.approval_service.request_policy_auto(
                        approval_request,
                        approval_decision,
                        promotion_policy_decision,
                    )
                except (KeyError, TypeError, ValueError):
                    approval = self.approval_service.request(approval_request, approval_decision)
            else:
                approval = self.approval_service.request(approval_request, approval_decision)
        except (KeyError, TypeError, ValueError):
            return self._update_run(
                graph_run,
                failure_code="PROMOTION_APPROVAL_UNAVAILABLE",
                **evaluation_fields,
            )
        return self._update_run(
            graph_run,
            status="ready_to_promote",
            failure_code=None,
            promotion_approval_id=approval.id,
            promotion_approval_request_digest=approval.request_digest,
            **evaluation_fields,
        )

    def replan(
        self,
        goal: Goal,
        proposal: ProposedGraph,
        policy: ExecutionPolicy,
        *,
        harness_digest: Digest,
        effective_policy_digest: Digest,
        run_id: Identifier,
        available_capabilities: Iterable[str],
    ) -> GraphRunRecord:
        """Deterministically accept and execute an already-produced strict revision."""

        return self.run(
            goal,
            proposal,
            policy,
            harness_digest=harness_digest,
            effective_policy_digest=effective_policy_digest,
            run_id=run_id,
            available_capabilities=available_capabilities,
            replan=True,
        )

    def replay(self, run_id: Identifier) -> GraphReplay:
        """Read persisted graph facts without invoking coordinators or composition."""

        return self._orchestrator(_replay_runner).replay(run_id)

    def _orchestrator(
        self,
        runner: Callable[[Node, WorkerRequest, ExecutionStrategy], NodeExecutionResult],
    ) -> TaskOrchestrator:
        return TaskOrchestrator(
            self.store,
            cast(NodeRunner, runner),
            self.strategies,
            max_concurrency=self.max_concurrency,
            routing_mode=self.routing_mode,
            fixed_strategy_id=self.fixed_strategy_id,
            allowed_strategy_ids=self.allowed_strategy_ids,
            allowed_backends=self.allowed_backends,
            local_backend_allowed=self.local_backend_allowed,
            bounded_graph_execution=True,
            defer_parent_evaluation=True,
            repository=self.repository,
            base_commit=self.base_commit,
            operator_config_digest=self.operator_config_digest,
            operator_config_path=self.operator_config_path,
            strategy_set=self.strategy_set,
            plan_reviewer=self.plan_reviewer,
            plan_reviser=self.plan_reviser,
            node_assessor=self.node_assessor,
            routing_risk_floor=self.routing_risk_floor,
            independent_node_assessment=self.independent_node_assessment,
            task_reviewer=self.task_reviewer,
            independent_task_review=self.independent_task_review,
            task_review_block_severities=self.task_review_block_severities,
            worker_supervision_policy=self.worker_supervision_policy,
            adapter_timeout_seconds=self.adapter_timeout_seconds,
        )

    def _update_run(self, run: GraphRunRecord, **changes: object) -> GraphRunRecord:
        updated = run.model_copy(update={"status": "failed", **changes})
        self.store.put(
            "graph_run_v2",
            updated,
            run_id=updated.id,
            revision=updated.generation + 1,
        )
        return updated

    def _recover_policy_approval_pointer(self, run: GraphRunRecord) -> GraphRunRecord | None:
        """Recover an exact policy approval and final pointer from durable authority facts."""

        if (
            run.failure_code != "PARENT_EVALUATION_UNAVAILABLE"
            or self.parent_evaluator is None
            or self.promotion_approval_policy is None
        ):
            return None
        authorities = tuple(
            item
            for item in self.store.list_records(
                "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run.id
            )
            if item.decision == "policy_auto_approved"
            and item.run_id == run.id
            and item.accepted_graph_revision_digest == run.accepted_graph_revision_digest
            and item.harness_digest == run.harness_digest
            and item.effective_policy_digest == run.effective_policy_digest
            and item.operator_config_digest == run.operator_config_digest
            and item.repository == run.repository
        )
        if len(authorities) != 1:
            return None
        authority = authorities[0]
        approvals = tuple(
            item
            for item in self.store.list_records("approval_v2", ApprovalRecord, run_id=run.id)
            if item.authorization_kind == "policy_auto"
            and item.authorization_digest == authority.content_digest
            and item.decision == "approved"
            and item.request_digest == authority.candidate_digest
            and item.policy_digest == authority.effective_policy_digest
            and item.accepted_graph_revision_digest == authority.accepted_graph_revision_digest
            and item.parent_evaluation_digest == authority.parent_evaluation_digest
            and item.verification_evidence_digests == authority.verification_evidence_digests
            and item.evaluation_evidence_digests == authority.evaluation_ledger_digests
            and item.semantic_evidence_digests == authority.semantic_evidence_digests
            and item.run_id == run.id
        )
        compositions = tuple(
            item
            for item in self.store.list_records(
                "graph_patch_composition_v2", GraphPatchCompositionRecord, run_id=run.id
            )
            if item.content_digest == authority.composition_digest
            and item.status == "succeeded"
            and item.candidate_patch is not None
            and item.candidate_patch.artifact_digest == authority.candidate_digest
            and item.accepted_graph_revision_digest == authority.accepted_graph_revision_digest
            and item.run_id == run.id
        )
        evaluations = tuple(
            item
            for item in self.store.list_records(
                "parent_candidate_evaluation_v2",
                ParentCandidateEvaluationRecord,
                run_id=run.id,
            )
            if item.content_digest == authority.parent_evaluation_digest
            and item.status == "ready_to_promote"
            and item.decision.value == "PASS"
            and item.composition_record_digest == authority.composition_digest
            and item.candidate_artifact_digest == authority.candidate_digest
            and item.goal_evaluator_digest == authority.goal_evaluator_digest
            and item.verification_result_digests == authority.verification_evidence_digests
            and item.evaluation_ledger_digests == authority.evaluation_ledger_digests
            and item.run_id == run.id
        )
        acceptances = tuple(
            item
            for item in self.store.list_records(
                "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run.id
            )
            if item.run_id == run.id
            and item.accepted_revision.content_digest == authority.accepted_graph_revision_digest
        )
        if (
            len(approvals) > 1
            or len(compositions) != 1
            or len(evaluations) != 1
            or len(acceptances) != 1
        ):
            return None
        try:
            replay = self.parent_evaluator.replay(evaluations[0].id)
            exact_replay = validate_exact_parent_evidence_store(
                self.store,
                run,
                acceptances[0].accepted_revision,
                evaluations[0],
                self.promotion_approval_policy.harness,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if replay != exact_replay:
            return None
        replay_semantic = tuple(
            digest
            for record in (
                *replay.semantic_requests,
                *replay.semantic_results,
                *replay.semantic_decisions,
                *replay.semantic_repair_requests,
            )
            if (digest := record.content_digest) is not None
        )
        if replay_semantic != authority.semantic_evidence_digests:
            return None
        if not approvals:
            if not isinstance(self.approval_service, DigestApprovalService):
                return None
            approval_requests = tuple(
                item
                for item in self.store.list_records(
                    "approval_request_v2", ApprovalRequest, run_id=run.id
                )
                if item.run_id == run.id
                and item.request_digest == authority.candidate_digest
                and item.policy_digest == authority.effective_policy_digest
                and item.approval_classes == ("promotion",)
            )
            policy_decisions = tuple(
                item
                for item in self.store.list_records(
                    "policy_decision_v2", PolicyDecision, run_id=run.id
                )
                if item.run_id == run.id
                and item.request_digest == authority.candidate_digest
                and item.effective_policy_digest == authority.effective_policy_digest
                and item.outcome is DecisionOutcome.APPROVAL_REQUIRED
                and item.required_approval_classes == ("promotion",)
            )
            if len(approval_requests) != 1 or len(policy_decisions) != 1:
                return None
            try:
                recovered_approval = self.approval_service.request_policy_auto(
                    approval_requests[0], policy_decisions[0], authority
                )
            except (KeyError, TypeError, ValueError):
                return None
            approvals = (recovered_approval,)
        composition = compositions[0]
        evaluation = evaluations[0]
        candidate = composition.candidate_patch
        if candidate is None:
            return None
        return self._update_run(
            run,
            status="ready_to_promote",
            failure_code=None,
            composition_id=composition.id,
            composition_digest=composition.content_digest,
            parent_candidate_artifact_id=candidate.id,
            parent_candidate_digest=candidate.artifact_digest,
            parent_evaluation_id=evaluation.id,
            parent_evaluation_digest=evaluation.content_digest,
            goal_evaluator_digest=evaluation.goal_evaluator_digest,
            promotion_approval_id=approvals[0].id,
            promotion_approval_request_digest=approvals[0].request_digest,
        )


def _authoritative_node_result(
    coordinator: WorkCoordinator,
    node: Node,
    request: WorkerRequest,
    run: WorkRun,
) -> NodeExecutionResult:
    store = coordinator.store
    persisted_run = store.get_work_run(run.id)
    writing = "edit_intent" in node.required_capabilities
    expected_status = "ready_to_promote" if writing else "completed"
    if persisted_run != run:
        raise ValueError("inner work run is not the authoritative persisted state")
    if (
        run.id != request.run_id
        or run.accepted_graph_digest != request.accepted_graph_revision_digest
        or run.node_id != node.id
        or run.node_generation != node.generation
        or run.node_attempt != node.attempt
        or run.worker_request_digest != request.content_digest
    ):
        raise ValueError("inner work run has stale graph/node/attempt bindings")
    if store.get("worker_request_v2", request.id, WorkerRequest) != request:
        raise ValueError("exact worker request was not persisted")
    if request.completion_criteria != node.completion_criteria:
        raise ValueError("worker request criteria are stale or mismatched")
    if run.worker_result_id is None:
        raise ValueError("inner work run has no worker result")
    worker_result = store.get("worker_result_v2", run.worker_result_id, WorkerResult)
    if (
        worker_result.run_id != request.run_id
        or worker_result.request_digest != request.content_digest
    ):
        raise ValueError("worker result is not bound to the exact request")
    acceptances = store.list_records(
        "non_mutating_result_acceptance_v2",
        NonMutatingResultAcceptance,
        run_id=run.id,
    )
    if worker_result.status != "succeeded":
        if acceptances:
            raise ValueError("failed worker result has an unexpected typed-result acceptance")
        expected_run_status = "cancelled" if worker_result.status == "cancelled" else "failed"
        if (
            run.status != expected_run_status
            or worker_result.failure is None
            or run.failure_code != worker_result.failure.code.value
        ):
            raise ValueError("worker failure is not the authoritative inner-run failure")
        return NodeExecutionResult(worker_result=worker_result, criterion_evidence=())
    typed_result = worker_result.non_mutating_result
    if typed_result is None:
        if acceptances:
            raise ValueError("worker result has an unexpected typed-result acceptance")
        result_acceptance = None
    else:
        if len(acceptances) != 1:
            raise ValueError("worker typed result lacks one explicit acceptance")
        result_acceptance = acceptances[0]
        if (
            result_acceptance.run_id != request.run_id
            or result_acceptance.graph_run_id != request.graph_run_id
            or result_acceptance.node_id != node.id
            or result_acceptance.accepted_graph_revision_digest
            != request.accepted_graph_revision_digest
            or result_acceptance.generation != request.generation
            or result_acceptance.attempt != request.attempt
            or result_acceptance.worker_request_digest != request.content_digest
            or result_acceptance.worker_result_id != worker_result.id
            or result_acceptance.worker_result_digest != worker_result.content_digest
            or result_acceptance.result_id != typed_result.id
            or result_acceptance.result_digest != typed_result.content_digest
        ):
            raise ValueError("worker typed-result acceptance is stale")
        if result_acceptance.status == "rejected":
            if (
                run.status != "failed"
                or worker_result.status != "succeeded"
                or result_acceptance.failure_code is None
                or run.failure_code != result_acceptance.failure_code.value
            ):
                raise ValueError("rejected typed result is not the authoritative failure")
            return NodeExecutionResult(
                worker_result=worker_result,
                criterion_evidence=(),
                result_acceptance=result_acceptance,
            )
        if (
            typed_result.run_id != request.run_id
            or typed_result.graph_run_id != request.graph_run_id
            or typed_result.worker_request_digest != request.content_digest
            or typed_result.node_id != node.id
            or typed_result.accepted_graph_revision_digest != request.accepted_graph_revision_digest
            or typed_result.generation != request.generation
            or typed_result.attempt != request.attempt
        ):
            raise ValueError("accepted worker typed result is stale")
    repairable_patch_failure = (
        writing
        and run.status == "failed"
        and run.failure_code == StableFailureCode.PATCH_PREFLIGHT_FAILED.value
    )
    if repairable_patch_failure:
        failed_actions = tuple(
            item
            for item in store.list_records("action_result_v2", ExecutionResult, run_id=run.id)
            if item.status == "failed"
            and item.failure is not None
            and item.failure.code is StableFailureCode.PATCH_PREFLIGHT_FAILED
        )
        if len(failed_actions) != 1 or run.workspace_id is None:
            raise ValueError("post-result patch preflight failure is absent or ambiguous")
        return NodeExecutionResult(
            worker_result=worker_result,
            criterion_evidence=(),
            workspace_id=run.workspace_id,
            result_acceptance=result_acceptance,
            failure_code=StableFailureCode.PATCH_PREFLIGHT_FAILED.value,
        )
    if writing:
        binding_configuration_valid = _node_verification_configuration_is_valid(
            run.id,
            node.completion_criteria,
            coordinator.verification_requests,
            coordinator.verification_bindings,
        ) and _persisted_node_verification_configuration_is_valid(
            store,
            run.id,
            node.completion_criteria,
            coordinator.verification_requests,
            coordinator.verification_bindings,
        )
        if not binding_configuration_valid:
            return NodeExecutionResult(
                worker_result=worker_result,
                criterion_evidence=(),
                workspace_id=run.workspace_id,
                result_acceptance=result_acceptance,
                failure_code=StableFailureCode.VERIFICATION_BINDING_INVALID.value,
            )
        if run.failure_code == StableFailureCode.VERIFICATION_BINDING_INVALID.value:
            raise ValueError("valid node verification was classified as an invalid binding")
        authoritative_verification_results = _authoritative_node_verification_results(
            store,
            run,
            node.completion_criteria,
            coordinator.verification_requests,
            coordinator.verification_bindings,
        )
        if authoritative_verification_results is None:
            return NodeExecutionResult(
                worker_result=worker_result,
                criterion_evidence=(),
                workspace_id=run.workspace_id,
                result_acceptance=result_acceptance,
                failure_code=StableFailureCode.VERIFICATION_BINDING_INVALID.value,
            )
    else:
        persisted_verification = store.list_records(
            "verification_result_v2", ExecutionResult, run_id=run.id
        )
        persisted_bindings = store.list_records(
            "node_verification_binding_v2", NodeVerificationBinding, run_id=run.id
        )
        if (
            coordinator.verification_requests
            or coordinator.verification_bindings
            or persisted_verification
            or persisted_bindings
            or run.verification_result_digests
        ):
            return NodeExecutionResult(
                worker_result=worker_result,
                criterion_evidence=(),
                workspace_id=run.workspace_id,
                result_acceptance=result_acceptance,
                failure_code=StableFailureCode.VERIFICATION_BINDING_INVALID.value,
            )
        authoritative_verification_results = ()
    repairable_verification_failure = (
        writing
        and run.status == "failed"
        and run.failure_code == StableFailureCode.VERIFICATION_FAILED.value
    )
    if (
        run.status != expected_status and not repairable_verification_failure
    ) or worker_result.status != "succeeded":
        raise ValueError("inner work run did not reach its authoritative terminal state")
    descriptors = tuple(
        store.get("artifact_descriptor_v2", artifact_id, ArtifactDescriptor)
        for artifact_id in run.output_artifact_ids
    )
    action_results = store.list_records("action_result_v2", ExecutionResult, run_id=run.id)
    verification_results = store.list_records(
        "verification_result_v2", ExecutionResult, run_id=run.id
    )
    produced_digests = {
        digest
        for result in (*action_results, *verification_results)
        for digest in (result.stdout_artifact_digest, result.stderr_artifact_digest)
        if digest is not None
    }
    accepted_non_mutating_result: _AcceptedNonMutatingResultSource | None = None
    if result_acceptance is not None:
        assert typed_result is not None
        acceptance_artifact = result_acceptance.artifact
        matches = tuple(
            item
            for item in descriptors
            if acceptance_artifact is not None and item.id == acceptance_artifact.id
        )
        if len(matches) != 1 or matches[0] != acceptance_artifact:
            raise ValueError("accepted typed-result artifact is absent, duplicate, or stale")
        accepted_artifact = matches[0]
        body = coordinator.artifact_reader(accepted_artifact)
        expected_body = canonical_json(typed_result).encode("utf-8")
        source = accepted_artifact.source
        if (
            body != expected_body
            or accepted_artifact.size_bytes != len(expected_body)
            or sha256(body).hexdigest() != accepted_artifact.artifact_digest
            or accepted_artifact.run_id != request.run_id
            or accepted_artifact.producer_action_id != worker_result.id
            or accepted_artifact.logical_kind != typed_result.logical_kind
            or accepted_artifact.media_type != typed_result.media_type
            or accepted_artifact.redaction_state != "none"
            or not isinstance(source, Mapping)
            or set(source)
            != {
                "graph_run_id",
                "worker_request_digest",
                "node_id",
                "accepted_graph_revision_digest",
                "generation",
                "attempt",
                "worker_result_id",
                "worker_result_digest",
                "result_id",
                "result_digest",
            }
            or source.get("graph_run_id") != request.graph_run_id
            or source.get("worker_request_digest") != request.content_digest
            or source.get("node_id") != node.id
            or source.get("accepted_graph_revision_digest")
            != request.accepted_graph_revision_digest
            or source.get("generation") != request.generation
            or source.get("attempt") != request.attempt
            or source.get("worker_result_id") != worker_result.id
            or source.get("worker_result_digest") != worker_result.content_digest
            or source.get("result_id") != typed_result.id
            or source.get("result_digest") != typed_result.content_digest
        ):
            raise ValueError("accepted typed-result artifact is not canonical")
        accepted_non_mutating_result = (
            request,
            worker_result,
            result_acceptance,
            accepted_artifact,
            body,
        )
        produced_digests.add(accepted_artifact.artifact_digest)
    if not writing:
        if run.patch_artifact_id is not None or not descriptors:
            raise ValueError("patchless node lacks authoritative artifacts")
        for descriptor in descriptors:
            if (
                descriptor.run_id != run.id
                or descriptor.content_digest is None
                or descriptor.artifact_digest not in produced_digests
            ):
                raise ValueError("patchless artifact is not bound to a mediated result")
    if run.workspace_id is None:
        raise ValueError("inner work run has no workspace")
    workspace = store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
    if (
        workspace.run_id != request.run_id
        or workspace.head_commit != coordinator.store.get_work_run(run.id).base_commit
        or Path(workspace.original_worktree).resolve() != Path(run.repository).resolve()
        or run.harness_digest != request.harness_digest
    ):
        raise ValueError("workspace snapshot is not bound to the inner work run")
    if not writing:
        patch = None
    elif run.patch_artifact_id is None:
        raise ValueError("inner work run has no patch artifact")
    else:
        patch = store.get("artifact_descriptor_v2", run.patch_artifact_id, ArtifactDescriptor)
    if patch is not None:
        descriptors_by_id = {item.id: item for item in descriptors}
        descriptors_by_id[patch.id] = patch
        descriptors = tuple(descriptors_by_id.values())
    if patch is not None:
        body = coordinator.artifact_reader(patch)
        source = patch.source
        if (
            not body.strip()
            or len(body) != patch.size_bytes
            or sha256(body).hexdigest() != patch.artifact_digest
            or patch.run_id != request.run_id
            or patch.logical_kind != "workspace_patch"
            or patch.media_type != "text/x-diff"
            or patch.producer_action_id != workspace.id
            or not isinstance(source, Mapping)
            or source.get("base_tree") != workspace.base_tree
            or source.get("workspace_digest") != workspace.content_digest
            or source.get("generated_paths") != coordinator.generated_paths
            or source.get("harness_digest") != request.harness_digest
        ):
            raise ValueError("patch is empty or not bound to the exact workspace")

    verification_digests = tuple(
        _required(item.content_digest) for item in authoritative_verification_results
    )
    if tuple(verification_digests) != run.verification_result_digests:
        raise ValueError("WorkRun verification ledger does not match persisted results")
    if run.acceptance_ledger_id is None:
        raise ValueError("inner work run has no acceptance ledger")
    ledger = store.get("acceptance_ledger_v2", run.acceptance_ledger_id, AcceptanceLedger)
    if ledger.run_id != run.id or tuple(item.criterion_id for item in ledger.criteria) != tuple(
        item.id for item in node.completion_criteria
    ):
        raise ValueError("acceptance ledger does not map the declared node criteria")
    result_digest_by_request = {
        item.request_digest: _required(item.content_digest)
        for item in authoritative_verification_results
        if item.status == "succeeded"
    }
    verification_by_requirement = {
        binding.requirement_id: result_digest_by_request.get(binding.process_request_digest, "")
        for binding in coordinator.verification_bindings
    }
    authoritative_refs = {
        *verification_digests,
        run.review_digest,
        None if result_acceptance is None else result_acceptance.content_digest,
        *(item.content_digest for item in descriptors),
        *(item.artifact_digest for item in descriptors),
    }
    for evidence in ledger.criteria:
        if evidence.disposition == "satisfied" and (
            not evidence.evidence_refs
            or not set(evidence.evidence_refs) <= {item for item in authoritative_refs if item}
        ):
            raise ValueError("criterion cites non-authoritative or empty evidence")
    if not _acceptance_evidence_is_authoritative(
        node.completion_criteria,
        ledger.criteria,
        verification_by_requirement,
        descriptors,
        accepted_non_mutating_result=accepted_non_mutating_result,
    ):
        return NodeExecutionResult(
            worker_result=worker_result,
            criterion_evidence=(),
            workspace_id=workspace.id,
            result_acceptance=result_acceptance,
            failure_code=StableFailureCode.VERIFICATION_BINDING_INVALID.value,
        )
    if not repairable_verification_failure and not _mandatory_acceptance_is_satisfied(
        node.completion_criteria, ledger.criteria
    ):
        return NodeExecutionResult(
            worker_result=worker_result,
            criterion_evidence=ledger.criteria,
            workspace_id=workspace.id,
            artifact_descriptors=descriptors,
            result_acceptance=result_acceptance,
            acceptance_ledger_digest=_required(ledger.content_digest),
            failure_code=StableFailureCode.VERIFICATION_FAILED.value,
        )
    node_patch = (
        None
        if patch is None
        else NodePatchArtifact(
            node_id=node.id,
            graph_run_id=request.graph_run_id or request.run_id,
            accepted_graph_revision_digest=request.accepted_graph_revision_digest
            or request.accepted_plan_digest,
            generation=node.generation,
            attempt=node.attempt,
            worker_request_digest=_required(request.content_digest),
            worker_result_digest=_required(worker_result.content_digest),
            acceptance_ledger_digest=_required(ledger.content_digest),
            verification_result_digests=verification_digests,
            workspace=workspace,
            patch=patch,
        )
    )
    return NodeExecutionResult(
        worker_result=worker_result,
        criterion_evidence=ledger.criteria,
        workspace_id=workspace.id,
        node_patch=node_patch,
        artifact_descriptors=descriptors,
        result_acceptance=result_acceptance,
        acceptance_ledger_digest=_required(ledger.content_digest),
        failure_code=(run.failure_code if repairable_verification_failure else None),
    )


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("authoritative record is missing its digest")
    return value


def _replay_runner(
    _node: Node, _request: WorkerRequest, _strategy: ExecutionStrategy
) -> NodeExecutionResult:
    raise AssertionError("replay must not invoke a worker")
