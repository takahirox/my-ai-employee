"""Durable v0.2 work-run coordinator built from controlled service boundaries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import Literal

from pydantic import ConfigDict, Field
from pydantic.main import BaseModel

from .domain import (
    CompletionCriterion,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    TaskAssessment,
)
from .domain.base import Identifier, freeze_json
from .domain.policy_v2 import PolicyLayer, PolicyResolver
from .domain.services_v2 import (
    ApprovalService,
    Cancellation,
    DownloadClient,
    Installer,
    ProcessExecutor,
    WorkerAdapter,
    WorkspaceManager,
)
from .domain.v2 import (
    AcceptanceLedger,
    ActionKind,
    ActionProposal,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    CriterionEvidence,
    DecisionOutcome,
    DigestedRecordV2,
    DownloadRequest,
    DownloadResult,
    EditIntentRequest,
    ExecutionResult,
    InstallRequest,
    PolicyDecision,
    ProcessRequest,
    WorkerRequest,
    WorkerResult,
    WorkspaceRequest,
    WorkspaceSnapshot,
)
from .graph import accept_task_graph
from .runtime import DeterministicRuntime
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_orchestration import TaskGraphAcceptance, one_node_graph

WorkStatus = Literal[
    "planning",
    "planned",
    "running",
    "waiting_approval",
    "verifying",
    "reviewing",
    "ready_to_promote",
    "promoting",
    "completed",
    "paused",
    "cancelled",
    "failed",
]
WorkActor = Literal["runtime", "worker", "operator", "service"]


def bind_service_decision(
    request: DigestedRecordV2, proposal_decision: PolicyDecision
) -> PolicyDecision:
    """Bind an already-resolved proposal decision to its exact service request."""

    payload = proposal_decision.model_dump()
    payload.update(
        {
            "id": identifier("service-policy"),
            "created_at": now(),
            "request_digest": request.content_digest,
            "content_digest": None,
        }
    )
    return PolicyDecision.model_validate(payload, strict=True)


class WorkRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: str
    goal: str
    repository: str
    base_commit: str
    worker: str
    task_assessment: TaskAssessment | None = None
    assessment_strategy: ExecutionStrategy | None = None
    selected_strategy: ExecutionStrategy | None = None
    strategy_set: Identifier | None = None
    status: WorkStatus = "planning"
    generation: int = Field(default=0, ge=0)
    plan_only: bool = False
    effective_policy_digest: str
    accepted_graph_digest: str | None = None
    node_id: str | None = None
    node_generation: int = Field(default=0, ge=0)
    node_attempt: int = Field(default=0, ge=0)
    worker_request_digest: str | None = None
    completion_criteria: tuple[CompletionCriterion, ...] = ()
    workspace_id: str | None = None
    worker_result_id: str | None = None
    pending_approval_id: str | None = None
    patch_artifact_id: str | None = None
    completed_action_digests: tuple[str, ...] = ()
    verification_result_digests: tuple[str, ...] = ()
    review_digest: str | None = None
    acceptance_ledger_id: str | None = None
    capture_patch: bool = True
    output_artifact_ids: tuple[Identifier, ...] = ()
    failure_code: str | None = None


class WorkEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    id: str
    run_id: str
    sequence: int = Field(ge=1)
    created_at: datetime
    kind: str
    actor: WorkActor
    request_digest: str | None = None
    result_digest: str | None = None
    policy_digest: str | None = None
    artifact_digests: tuple[str, ...] = ()
    previous_event_digest: str | None = None
    details: object = None


class _Cancellation:
    def __init__(self, store: SQLiteStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def cancelled(self) -> bool:
        return self.store.control(self.run_id) == "cancel"


class _Channel:
    def __init__(self, coordinator: WorkCoordinator, run: WorkRun) -> None:
        self.coordinator = coordinator
        self.run = run
        self.decisions: list[tuple[ActionProposal, PolicyDecision]] = []

    def submit(self, proposal: ActionProposal) -> PolicyDecision:
        if proposal.run_id != self.run.id:
            raise ValueError("stale worker proposal belongs to another run")
        decision = self.coordinator._decide(proposal)
        self.decisions.append((proposal, decision))
        self.coordinator.store.put("policy_decision_v2", decision, run_id=self.run.id)
        self.coordinator._event(
            self.run.id,
            "action_decided",
            "runtime",
            request_digest=proposal.content_digest,
            result_digest=decision.content_digest,
            policy_digest=decision.effective_policy_digest,
        )
        return decision


class WorkCoordinator:
    """Coordinates workers and services; DeterministicRuntime remains the authority root."""

    def __init__(
        self,
        store: SQLiteStore,
        runtime: DeterministicRuntime,
        workspace: WorkspaceManager,
        worker_factory: Callable[[WorkspaceSnapshot | None, Cancellation], WorkerAdapter],
        process_factory: Callable[[WorkspaceSnapshot], ProcessExecutor],
        artifact_reader: Callable[[ArtifactDescriptor], bytes],
        policy_layers: tuple[PolicyLayer, ...],
        *,
        task_assessment: TaskAssessment | None = None,
        assessment_strategy: ExecutionStrategy | None = None,
        selected_strategy: ExecutionStrategy | None = None,
        strategy_set: Identifier | None = None,
        request_promotion_approval: bool = True,
        approval_service: ApprovalService | None = None,
        download_client: DownloadClient | None = None,
        installer_factory: Callable[[WorkspaceSnapshot], Installer] | None = None,
        max_worker_turns: int = 1,
        verification_requests: tuple[ProcessRequest, ...] = (),
        protected_paths: tuple[str, ...] = (".git/**",),
        allowed_processes: tuple[tuple[str, ...], ...] = (),
    ) -> None:
        if runtime.store is not store:
            raise ValueError(
                "coordinator and DeterministicRuntime must share the authoritative store"
            )
        if max_worker_turns < 1:
            raise ValueError("max_worker_turns must be positive")
        if (task_assessment is None) != (selected_strategy is None):
            raise ValueError("task_assessment and selected_strategy must be provided together")
        if strategy_set is not None and selected_strategy is None:
            raise ValueError("strategy_set requires a selected strategy")
        if assessment_strategy is not None and task_assessment is None:
            raise ValueError("assessment_strategy requires a task assessment")
        self.store = store
        self.runtime = runtime
        self.workspace = workspace
        self.worker_factory = worker_factory
        self.process_factory = process_factory
        self.artifact_reader = artifact_reader
        self.policy_layers = policy_layers
        self.task_assessment = task_assessment
        self.assessment_strategy = assessment_strategy
        self.selected_strategy = selected_strategy
        self.strategy_set = strategy_set
        self.request_promotion_approval = request_promotion_approval
        self.approval_service = approval_service
        self.download_client = download_client
        self.installer_factory = installer_factory
        self.max_worker_turns = max_worker_turns
        self.verification_requests = verification_requests
        self.protected_paths = protected_paths
        self.allowed_processes = allowed_processes

    def execute_node(
        self,
        request: WorkerRequest,
        completion_criteria: tuple[CompletionCriterion, ...],
        repository: str,
        base_commit: str,
        *,
        worker_name: str,
        capture_patch: bool = True,
    ) -> WorkRun:
        """Execute one accepted graph request without creating another graph authority."""

        return self.start(
            request.goal,
            repository,
            base_commit,
            worker_name=worker_name,
            run_id=request.run_id,
            _accepted_request=request,
            _completion_criteria=completion_criteria,
            _capture_patch=capture_patch,
        )

    def start(
        self,
        goal: str,
        repository: str,
        base_commit: str,
        *,
        worker_name: str,
        plan_only: bool = False,
        run_id: str | None = None,
        _accepted_request: WorkerRequest | None = None,
        _completion_criteria: tuple[CompletionCriterion, ...] = (),
        _capture_patch: bool = True,
    ) -> WorkRun:
        policy_digest = canonical_digest([layer.content_digest for layer in self.policy_layers])
        if _accepted_request is not None:
            if run_id is not None and run_id != _accepted_request.run_id:
                raise ValueError("accepted node request run binding is stale")
            if _accepted_request.effective_policy_digest != policy_digest:
                raise ValueError("accepted node request policy binding is stale")
            run_id = _accepted_request.run_id
            harness_digest = _accepted_request.harness_digest
        else:
            run_id = run_id or identifier("work")
            harness_digest = canonical_digest({"repository": repository})
        wall_limits = tuple(
            layer.max_wall_seconds
            for layer in self.policy_layers
            if layer.max_wall_seconds is not None
        )
        max_wall_seconds = min(wall_limits, default=3600.0)
        accepted_graph_digest: str
        node_id: str
        if _accepted_request is None:
            graph_goal = Goal(id=identifier("goal"), statement=goal)
            graph = one_node_graph(
                graph_goal,
                graph_id=identifier("graph"),
                node_id=identifier("node"),
                max_wall_seconds=max_wall_seconds,
            )
            graph_policy = ExecutionPolicy(
                max_nodes=1,
                max_attempts=1,
                max_wall_seconds=max_wall_seconds,
            )
            accepted_graph = accept_task_graph(
                graph,
                graph_policy,
                available_capabilities=tuple(
                    dict.fromkeys(
                        capability
                        for layer in self.policy_layers
                        for capability in (layer.allowed_capabilities or ())
                    )
                ),
            )
            graph_acceptance = TaskGraphAcceptance(
                id=identifier("graph-acceptance"),
                run_id=run_id,
                created_at=now(),
                accepted_revision=accepted_graph,
                effective_policy_digest=policy_digest,
                harness_digest=harness_digest,
            )
            self.store.save_graph(run_id, accepted_graph)
            self.store.put("task_graph_acceptance_v2", graph_acceptance, run_id=run_id)
            accepted_graph_digest = graph_acceptance.content_digest or ""
            node_id = graph.nodes[0].id
        else:
            accepted_graph_digest = _accepted_request.accepted_graph_revision_digest or ""
            node_id = _accepted_request.node_id or ""
        run = WorkRun(
            id=run_id,
            goal=goal,
            repository=repository,
            base_commit=base_commit,
            worker=worker_name,
            task_assessment=self.task_assessment,
            assessment_strategy=self.assessment_strategy,
            selected_strategy=self.selected_strategy,
            strategy_set=self.strategy_set,
            plan_only=plan_only,
            effective_policy_digest=policy_digest,
            accepted_graph_digest=accepted_graph_digest,
            node_id=node_id,
            node_generation=(0 if _accepted_request is None else _accepted_request.generation),
            node_attempt=(0 if _accepted_request is None else _accepted_request.attempt),
            worker_request_digest=(
                None if _accepted_request is None else _accepted_request.content_digest
            ),
            completion_criteria=_completion_criteria,
            capture_patch=_capture_patch,
        )
        self.store.save_work_run(run)
        for layer in self.policy_layers:
            self.store.put("policy_layer_v2", layer, run_id=run.id)
        self._event(run.id, "run_created", "runtime", policy_digest=policy_digest)
        cancellation = _Cancellation(self.store, run.id)
        adapter = self.worker_factory(None, cancellation)
        availability = adapter.probe()
        self.store.put("worker_availability_v2", availability, run_id=run.id)
        self._event(
            run.id,
            "worker_probed",
            "worker",
            result_digest=availability.content_digest,
        )
        if availability.availability == "unavailable":
            return self._update(run, status="failed", failure_code="WORKER_UNAVAILABLE")
        if plan_only:
            return self._update(run, status="planned")
        request = WorkspaceRequest(
            id=identifier("workspace-request"),
            run_id=run.id,
            created_at=now(),
            repository=repository,
            base_commit=base_commit,
        )
        self.store.put("workspace_request_v2", request, run_id=run.id)
        snapshot = self.workspace.create(request)
        self.store.put("workspace_v2", snapshot, run_id=run.id)
        self._event(
            run.id,
            "workspace_created",
            "service",
            request_digest=request.content_digest,
            result_digest=snapshot.content_digest,
        )
        run = self._update(run, status="running", workspace_id=snapshot.id)
        adapter = self.worker_factory(snapshot, cancellation)
        worker_request = _accepted_request or WorkerRequest(
            id=identifier("worker-request"),
            run_id=run.id,
            created_at=now(),
            goal=goal,
            accepted_plan_digest=run.accepted_graph_digest or canonical_digest({"goal": goal}),
            node_id=run.node_id,
            accepted_graph_revision_digest=run.accepted_graph_digest,
            graph_run_id=run.id,
            workspace_context=(),
            harness_digest=harness_digest,
            effective_policy_digest=run.effective_policy_digest,
            remaining_budgets=freeze_json({"worker_turns": self.max_worker_turns}),
        )
        self.store.put("worker_request_v2", worker_request, run_id=run.id)
        channel = _Channel(self, run)
        result = adapter.propose(worker_request, channel)
        self.store.put("worker_result_v2", result, run_id=run.id)
        self._event(
            run.id,
            "worker_finished",
            "worker",
            request_digest=worker_request.content_digest,
            result_digest=result.content_digest,
            artifact_digests=tuple(
                digest
                for digest in (
                    result.stdout_artifact_digest,
                    result.stderr_artifact_digest,
                )
                if digest is not None
            ),
        )
        run = self._update(run, worker_result_id=result.id)
        if result.status != "succeeded":
            code = result.failure.code.value if result.failure else "WORKER_PROTOCOL_ERROR"
            return self._update(run, status="failed", failure_code=code)
        return self._execute_actions(run, snapshot, channel, cancellation)

    def resume(self, run_id: str) -> WorkRun:
        run = WorkRun.model_validate(self.store.get_work_run(run_id))
        generation, checkpoint = self.store.load_work_checkpoint(run_id)
        current_policy_digest = canonical_digest(
            [layer.content_digest for layer in self.policy_layers]
        )
        if (
            generation != run.generation
            or checkpoint.get("policy_digest") != run.effective_policy_digest
            or current_policy_digest != run.effective_policy_digest
        ):
            raise ValueError("stale checkpoint or policy rejected")
        if self.store.control(run_id) == "cancel":
            self.store.clear_control(run_id)
            if run.status != "cancelled":
                return self._update(run, status="cancelled", generation=run.generation + 1)
        if run.status == "waiting_approval":
            if run.pending_approval_id is None:
                raise ValueError("waiting run has no approval")
            approval = self.store.get("approval_v2", run.pending_approval_id, ApprovalRecord)
            if approval.decision == "pending":
                return run
            if (
                approval.decision != "approved"
                or approval.policy_digest != run.effective_policy_digest
            ):
                return self._update(run, status="failed", failure_code="STALE_OR_DENIED_APPROVAL")
            if self.approval_service is None or run.worker_result_id is None:
                raise ValueError("resume requires the original approval and worker result services")
            result = self.store.get("worker_result_v2", run.worker_result_id, WorkerResult)
            decisions = self.store.list_records("policy_decision_v2", PolicyDecision, run_id=run.id)
            decision_by_request = {item.request_digest: item for item in decisions}
            channel = _Channel(self, run)
            for proposal in result.proposals:
                decision = decision_by_request.get(proposal.content_digest or "")
                if decision is None:
                    raise ValueError("persisted proposal has no policy decision")
                if approval.request_digest == proposal.content_digest:
                    decision = self.approval_service.apply(decision, approval)
                channel.decisions.append((proposal, decision))
            if run.workspace_id is None:
                raise ValueError("resumed action run has no workspace")
            snapshot = self.store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
            self.workspace.adopt(snapshot)
            resumed = self._update(
                run,
                generation=run.generation + 1,
                status="running",
                pending_approval_id=None,
            )
            return self._execute_actions(
                resumed, snapshot, channel, _Cancellation(self.store, run.id)
            )
        if run.status in {"paused", "cancelled"}:
            if run.worker_result_id is None or run.workspace_id is None:
                raise ValueError("paused or cancelled run is missing durable action state")
            result = self.store.get("worker_result_v2", run.worker_result_id, WorkerResult)
            decisions = self.store.list_records("policy_decision_v2", PolicyDecision, run_id=run.id)
            decision_by_request = {item.request_digest: item for item in decisions}
            channel = _Channel(self, run)
            for proposal in result.proposals:
                decision = decision_by_request.get(proposal.content_digest or "")
                if decision is None:
                    raise ValueError("persisted proposal has no policy decision")
                channel.decisions.append((proposal, decision))
            snapshot = self.store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
            self.workspace.adopt(snapshot)
            self.store.clear_control(run.id)
            resumed = self._update(run, generation=run.generation + 1, status="running")
            return self._execute_actions(
                resumed, snapshot, channel, _Cancellation(self.store, run.id)
            )
        return self._update(run, generation=run.generation + 1, status="running")

    def _execute_actions(
        self,
        run: WorkRun,
        snapshot: WorkspaceSnapshot,
        channel: _Channel,
        cancellation: Cancellation,
    ) -> WorkRun:
        executor = self.process_factory(snapshot)
        installer = None if self.installer_factory is None else self.installer_factory(snapshot)
        completed = list(run.completed_action_digests)
        output_artifact_ids = list(run.output_artifact_ids)

        def retain(descriptors: tuple[ArtifactDescriptor, ...]) -> None:
            self._retain_artifacts(run.id, descriptors, output_artifact_ids)

        for proposal, decision in channel.decisions:
            digest = proposal.content_digest or ""
            if digest in completed:
                continue
            if self.store.control(run.id) == "pause":
                return self._update(run, status="paused")
            if cancellation.cancelled():
                return self._update(run, status="cancelled")
            if decision.outcome is DecisionOutcome.DENY:
                return self._update(run, status="failed", failure_code="POLICY_DENIED")
            if decision.outcome is DecisionOutcome.APPROVAL_REQUIRED:
                approval = self._request_approval(proposal, decision)
                return self._update(run, status="waiting_approval", pending_approval_id=approval.id)
            payload = proposal.payload
            service_decision = bind_service_decision(payload, decision)
            self._event(
                run.id,
                "action_started",
                "service",
                request_digest=payload.content_digest,
                policy_digest=service_decision.effective_policy_digest,
            )
            if proposal.kind is ActionKind.PROCESS and isinstance(payload, ProcessRequest):
                result = executor.execute(payload, service_decision, cancellation)
            elif proposal.kind is ActionKind.DOWNLOAD and isinstance(payload, DownloadRequest):
                if self.download_client is None:
                    return self._update(
                        run, status="failed", failure_code="DOWNLOAD_SERVICE_UNAVAILABLE"
                    )
                result = self.download_client.fetch(payload, service_decision, cancellation)
            elif proposal.kind is ActionKind.INSTALL and isinstance(payload, InstallRequest):
                if installer is None:
                    return self._update(
                        run, status="failed", failure_code="INSTALL_SERVICE_UNAVAILABLE"
                    )
                result = installer.install(payload, service_decision, cancellation)
            elif proposal.kind is ActionKind.EDIT_INTENT and isinstance(payload, EditIntentRequest):
                result = self.workspace.apply_edit(
                    snapshot, payload, service_decision, cancellation
                )
            else:
                return self._update(run, status="failed", failure_code="UNSUPPORTED_ACTION")
            self.store.put("action_result_v2", result, run_id=run.id)
            retain(_mediated_result_artifacts(result, executor))
            self._event(
                run.id,
                "action_finished",
                "service",
                request_digest=payload.content_digest,
                result_digest=result.content_digest,
                artifact_digests=tuple(
                    digest
                    for digest in (
                        result.stdout_artifact_digest,
                        result.stderr_artifact_digest,
                    )
                    if digest is not None
                ),
            )
            if result.status != "succeeded":
                code = result.failure.code.value if result.failure else "PROCESS_FAILED"
                return self._update(run, status="failed", failure_code=code)
            completed.append(digest)
            run = self._update(
                run,
                completed_action_digests=tuple(completed),
                output_artifact_ids=tuple(output_artifact_ids),
            )
        run = self._update(run, status="verifying")
        verification_digests: list[str] = []
        for request in self.verification_requests:
            if self.store.control(run.id) == "pause":
                return self._update(run, status="paused")
            if request.run_id != run.id:
                return self._update(run, status="failed", failure_code="STALE_VERIFICATION_REQUEST")
            verification_proposal = ActionProposal(
                id=identifier("verification-proposal"),
                run_id=run.id,
                created_at=now(),
                worker_id="runtime-verifier",
                kind=ActionKind.PROCESS,
                payload=request,
                reason="required Harness verification",
            )
            self.store.put("verification_request_v2", request, run_id=run.id)
            proposal_decision = self._decide(verification_proposal)
            if proposal_decision.outcome is not DecisionOutcome.ALLOW:
                return self._update(run, status="failed", failure_code="VERIFICATION_POLICY_DENIED")
            result = executor.execute(
                request,
                bind_service_decision(request, proposal_decision),
                cancellation,
            )
            self.store.put("verification_result_v2", result, run_id=run.id)
            retain(_mediated_result_artifacts(result, executor))
            if result.status != "succeeded":
                return self._update(run, status="failed", failure_code="VERIFICATION_FAILED")
            verification_digests.append(result.content_digest or "")

        if not run.capture_patch:
            artifacts = tuple(
                self.store.get("artifact_descriptor_v2", artifact_id, ArtifactDescriptor)
                for artifact_id in output_artifact_ids
            )
            criteria = _declared_criterion_evidence(
                run.completion_criteria,
                self.verification_requests,
                tuple(verification_digests),
                artifacts,
            )
            ledger = AcceptanceLedger(
                id=identifier("acceptance-ledger"),
                run_id=run.id,
                created_at=now(),
                criteria=criteria,
            )
            self.store.put("acceptance_ledger_v2", ledger, run_id=run.id)
            return self._update(
                run,
                status="completed",
                verification_result_digests=tuple(verification_digests),
                acceptance_ledger_id=ledger.id,
                output_artifact_ids=tuple(output_artifact_ids),
            )

        patch = self.workspace.capture_diff(snapshot)
        self.store.put("artifact_descriptor_v2", patch, run_id=run.id)
        retain((patch,))
        patch_bytes = self.artifact_reader(patch)
        if not patch_bytes.strip():
            return self._update(run, status="failed", failure_code="EMPTY_PATCH")
        patch_text = patch_bytes.decode("utf-8", "replace")
        changed_paths = tuple(
            line[6:]
            for line in patch_text.splitlines()
            if (line.startswith("+++ b/") or line.startswith("--- a/")) and line[6:] != "/dev/null"
        )
        if any(
            fnmatch(path, pattern) for path in changed_paths for pattern in self.protected_paths
        ):
            return self._update(run, status="failed", failure_code="REVIEW_BLOCKED")
        promotion_approval_id: str | None = None
        if self.request_promotion_approval and self.approval_service is not None:
            promotion_decision = PolicyDecision(
                id=identifier("promotion-policy"),
                run_id=run.id,
                created_at=now(),
                request_digest=patch.artifact_digest,
                effective_policy_digest=run.effective_policy_digest,
                outcome=DecisionOutcome.APPROVAL_REQUIRED,
                reason_code="explicit_promotion_approval",
                required_approval_classes=("promotion",),
            )
            promotion_request = ApprovalRequest(
                id=identifier("promotion-approval-request"),
                run_id=run.id,
                created_at=now(),
                request_digest=patch.artifact_digest,
                policy_digest=run.effective_policy_digest,
                approval_classes=("promotion",),
                expires_at=now() + timedelta(hours=1),
            )
            promotion_approval = self.approval_service.request(
                promotion_request, promotion_decision
            )
            promotion_approval_id = promotion_approval.id
        # The exact patch digest is the deterministic review input. Required Harness
        # commands are submitted as ordinary process proposals by the coordinator caller.
        review_digest = canonical_digest({"patch": patch.artifact_digest, "blocked": False})
        criteria = (
            _declared_criterion_evidence(
                run.completion_criteria,
                self.verification_requests,
                tuple(verification_digests),
                (patch,),
            )
            if run.completion_criteria
            else (
                *(
                    CriterionEvidence(
                        criterion_id=f"verification-{index}",
                        disposition="satisfied",
                        evidence_refs=(digest,),
                    )
                    for index, digest in enumerate(verification_digests, start=1)
                ),
                CriterionEvidence(
                    criterion_id="reviewed-patch",
                    disposition="satisfied",
                    evidence_refs=(patch.artifact_digest, review_digest),
                ),
                CriterionEvidence(
                    criterion_id="promotion-ready",
                    disposition="satisfied",
                    evidence_refs=(patch.artifact_digest, review_digest),
                ),
            )
        )
        ledger = AcceptanceLedger(
            id=identifier("acceptance-ledger"),
            run_id=run.id,
            created_at=now(),
            criteria=criteria,
        )
        self.store.put("acceptance_ledger_v2", ledger, run_id=run.id)
        self._event(
            run.id,
            "review_finished",
            "runtime",
            request_digest=patch.artifact_digest,
            result_digest=review_digest,
            artifact_digests=(patch.artifact_digest,),
        )
        return self._update(
            run,
            status="ready_to_promote",
            patch_artifact_id=patch.id,
            pending_approval_id=promotion_approval_id,
            verification_result_digests=tuple(verification_digests),
            review_digest=review_digest,
            acceptance_ledger_id=ledger.id,
            output_artifact_ids=tuple(output_artifact_ids),
        )

    def _retain_artifacts(
        self, run_id: str, descriptors: tuple[ArtifactDescriptor, ...], retained: list[str]
    ) -> None:
        for descriptor in descriptors:
            if descriptor.run_id != run_id or descriptor.content_digest is None:
                raise ValueError("mediated artifact descriptor has stale provenance")
            self.store.put("artifact_descriptor_v2", descriptor, run_id=run_id)
            if descriptor.id not in retained:
                retained.append(descriptor.id)

    def _decide(self, proposal: ActionProposal) -> PolicyDecision:
        resolution = PolicyResolver().resolve(
            proposal,
            self.policy_layers,
            decision_id=identifier("policy-decision"),
            created_at=now(),
        )
        run_policy_digest = self.store.get_work_run(proposal.run_id).effective_policy_digest
        if (
            proposal.kind is ActionKind.PROCESS
            and isinstance(proposal.payload, ProcessRequest)
            and proposal.payload.argv not in self.allowed_processes
        ):
            return PolicyDecision(
                id=identifier("policy-decision"),
                run_id=proposal.run_id,
                created_at=now(),
                request_digest=proposal.content_digest or "",
                effective_policy_digest=run_policy_digest,
                outcome=DecisionOutcome.DENY,
                reason_code="process_not_declared_by_harness",
                limits=resolution.decision.limits,
            )
        if proposal.kind is ActionKind.EDIT_INTENT and isinstance(
            proposal.payload, EditIntentRequest
        ):
            writable = resolution.effective_policy.writable_paths or ()
            path_denied = any(
                not any(fnmatch(path, pattern) for pattern in writable)
                or any(fnmatch(path, pattern) for pattern in self.protected_paths)
                for path in proposal.payload.paths
            )
            if path_denied:
                return PolicyDecision(
                    id=identifier("policy-decision"),
                    run_id=proposal.run_id,
                    created_at=now(),
                    request_digest=proposal.content_digest or "",
                    effective_policy_digest=run_policy_digest,
                    outcome=DecisionOutcome.DENY,
                    reason_code="edit_path_denied_by_harness",
                    limits=resolution.decision.limits,
                )
        payload = resolution.decision.model_dump()
        payload.update({"effective_policy_digest": run_policy_digest, "content_digest": None})
        return PolicyDecision.model_validate(payload, strict=True)

    def _request_approval(
        self, proposal: ActionProposal, decision: PolicyDecision
    ) -> ApprovalRecord:
        if self.approval_service is None:
            raise ValueError("approval service is required by policy")
        request = ApprovalRequest(
            id=identifier("approval-request"),
            run_id=proposal.run_id,
            created_at=now(),
            request_digest=proposal.content_digest or "",
            policy_digest=decision.effective_policy_digest,
            approval_classes=decision.required_approval_classes,
            expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1),
        )
        approval = self.approval_service.request(request, decision)
        self.store.put("approval_v2", approval, run_id=proposal.run_id)
        return approval

    def _update(self, run: WorkRun, **changes: object) -> WorkRun:
        updated = run.model_copy(update=changes)
        self.store.save_work_run(updated)
        self.store.checkpoint_work(
            updated.id,
            updated.generation,
            {
                "status": updated.status,
                "policy_digest": updated.effective_policy_digest,
                "completed_action_digests": updated.completed_action_digests,
            },
        )
        self._event(updated.id, "run_status", "runtime", details={"status": updated.status})
        return updated

    def _event(self, run_id: str, kind: str, actor: WorkActor, **values: object) -> None:
        previous = self.store.work_events(run_id)
        prior_digest = canonical_digest(previous[-1]) if previous else None
        event = WorkEvent.model_validate(
            {
                "id": identifier("event-v2"),
                "run_id": run_id,
                "sequence": len(previous) + 1,
                "created_at": now(),
                "kind": kind,
                "actor": actor,
                "previous_event_digest": prior_digest,
                **values,
            }
        )
        self.store.append_work_event(event)


def _declared_criterion_evidence(
    criteria: tuple[CompletionCriterion, ...],
    verification_requests: tuple[ProcessRequest, ...],
    verification_digests: tuple[str, ...],
    artifacts: tuple[ArtifactDescriptor, ...],
) -> tuple[CriterionEvidence, ...]:
    """Map declared criteria only to exact first-party results and artifacts."""

    if len(verification_requests) != len(verification_digests):
        raise ValueError("verification request/result cardinality mismatch")
    verification_by_id = {
        request.id: digest
        for request, digest in zip(verification_requests, verification_digests, strict=True)
    }
    artifact_refs: dict[str, tuple[str, str]] = {}
    kinds: dict[str, list[ArtifactDescriptor]] = {}
    for artifact in artifacts:
        artifact_refs[artifact.id] = (
            artifact.content_digest or "",
            artifact.artifact_digest,
        )
        kinds.setdefault(artifact.logical_kind, []).append(artifact)
    # Logical-kind binding is authoritative only when it resolves uniquely.
    for logical_kind, matches in kinds.items():
        if len(matches) == 1:
            artifact_refs[logical_kind] = (
                matches[0].content_digest or "",
                matches[0].artifact_digest,
            )
    result: list[CriterionEvidence] = []
    for criterion in criteria:
        refs: list[str] = []
        missing = False
        for requirement_id in criterion.verification_requirement_ids:
            digest = verification_by_id.get(requirement_id)
            if digest is None:
                missing = True
            else:
                refs.append(digest)
        for artifact_id in criterion.required_artifact_ids:
            artifact_ref = artifact_refs.get(artifact_id)
            if artifact_ref is None:
                missing = True
            else:
                refs.extend(artifact_ref)
        declared = bool(criterion.verification_requirement_ids or criterion.required_artifact_ids)
        unique_refs = tuple(dict.fromkeys(ref for ref in refs if ref))
        result.append(
            CriterionEvidence(
                criterion_id=criterion.id,
                disposition=(
                    "satisfied" if declared and not missing and unique_refs else "uncovered"
                ),
                evidence_refs=unique_refs,
            )
        )
    return tuple(result)


def _mediated_result_artifacts(
    result: ExecutionResult, service: object
) -> tuple[ArtifactDescriptor, ...]:
    """Resolve descriptors from the service that produced an authoritative result."""

    descriptors: list[ArtifactDescriptor] = []
    if isinstance(result, DownloadResult) and result.artifact is not None:
        descriptors.append(result.artifact)
    resolver = getattr(service, "output_descriptor", None)
    if callable(resolver):
        for digest, logical_kind in (
            (result.stdout_artifact_digest, "process_stdout"),
            (result.stderr_artifact_digest, "process_stderr"),
        ):
            if digest is not None:
                descriptor = resolver(digest, logical_kind, result.id)
                if not isinstance(descriptor, ArtifactDescriptor):
                    raise TypeError("service returned a non-descriptor artifact reference")
                descriptors.append(descriptor)
    unique = {item.id: item for item in descriptors}
    if len(unique) != len(descriptors):
        raise ValueError("mediated result returned duplicate artifact descriptor IDs")
    return tuple(unique.values())
