"""Authoritative evaluation of one exact composed graph candidate."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import ClassVar, Literal, Protocol, Self, TypeVar, cast

from pydantic import ConfigDict, Field, model_validator
from pydantic.main import BaseModel

from .domain import Goal, ProjectHarnessV2
from .domain.base import Digest, Identifier
from .domain.browser import BrowserEvaluationServices, BrowserObservation
from .domain.evaluation import (
    CandidateRevision,
    EvaluationBudget,
    EvaluationDecision,
    EvaluationEvidenceLedger,
    EvaluationRequest,
    EvaluatorServices,
    EvaluatorSpecification,
    decide_evaluation,
    evaluate_freshness,
)
from .domain.models import AcceptedGraphRevision
from .domain.services_v2 import Cancellation, ProcessExecutor, WorkspaceManager
from .domain.v2 import (
    AcceptanceLedger,
    ArtifactDescriptor,
    CriterionEvidence,
    DigestedRecordV2,
    PolicyDecision,
    ProcessRequest,
    WorkspaceSnapshot,
)
from .evaluators import DEFAULT_EVALUATOR_REGISTRY, HarnessProcessEvaluationServices
from .graph_composition import GraphPatchCompositionRecord, GraphPatchCompositionRequest
from .parent_review import (
    ParentNodeReviewBinding,
    ParentSemanticRepairRequest,
    ParentSemanticReviewDecision,
    ParentSemanticReviewer,
    ParentSemanticReviewRequest,
    ParentSemanticReviewResult,
    ParentSemanticSeverity,
    StaleParentSemanticReviewResult,
    decide_parent_semantic_review,
    validate_parent_semantic_review_result,
)
from .serialization import canonical_digest, project_harness_digest, versioned_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_orchestration import (
    GoalEvaluatorRecord,
    GraphRunRecord,
    NodeExecutionRecord,
    TaskGraphAcceptance,
    _validate_retained_node,
)

PolicyDecider = Callable[[ProcessRequest], PolicyDecision]
ExecutorFactory = Callable[[WorkspaceSnapshot], ProcessExecutor]


class BrowserExecutionServices(BrowserEvaluationServices, Protocol):
    """Browser boundary plus the observations needed for durable replay."""

    observations: list[BrowserObservation]


BrowserServicesFactory = Callable[[WorkspaceSnapshot, Cancellation], BrowserExecutionServices]
RecordT = TypeVar("RecordT", bound=DigestedRecordV2)


class ParentVerificationBinding(BaseModel):
    """One required Harness evaluator and its exact provider-specific authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    harness_evaluator_id: Identifier
    harness_command_ref: Identifier | None = None
    specification: EvaluatorSpecification
    process_request: ProcessRequest | None = None

    @model_validator(mode="after")
    def _request_is_bound_to_the_specification(self) -> Self:
        if self.specification.provider_id == "process.harness":
            if self.harness_command_ref is None or self.process_request is None:
                raise ValueError("parent process evaluator requires its exact request")
            if self.specification.run_id != self.process_request.run_id:
                raise ValueError("parent evaluator and process request belong to different runs")
            if self.specification.command_ref != self.process_request.id:
                raise ValueError("parent evaluator does not name its exact process request")
            if self.specification.browser_scenario is not None:
                raise ValueError("parent process evaluator cannot retain a browser scenario")
        elif self.specification.provider_id == "browser.playwright":
            if self.harness_command_ref is not None or self.process_request is not None:
                raise ValueError("parent browser evaluator cannot retain process authority")
            if self.specification.command_ref is not None:
                raise ValueError("parent browser evaluator cannot name a process request")
            if self.specification.browser_scenario is None:
                raise ValueError("parent browser evaluator requires its exact scenario")
        else:
            raise ValueError("unsupported parent evaluator provider")
        return self


class ParentCandidateEvaluationRequest(DigestedRecordV2):
    """All immutable authority required to evaluate one composed parent candidate."""

    schema_name: ClassVar[str] = "parent_candidate_evaluation_request"
    goal: Goal
    goal_digest: Digest
    harness_digest: Digest
    accepted_revision: AcceptedGraphRevision
    composition_id: Identifier
    composition_request_digest: Digest
    composition_record_digest: Digest
    composition_workspace: WorkspaceSnapshot
    candidate: CandidateRevision
    candidate_artifact: ArtifactDescriptor
    effective_policy_digest: Digest
    verification_bindings: tuple[ParentVerificationBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _candidate_and_bindings_are_exact(self) -> Self:
        if self.goal_digest != canonical_digest(self.goal):
            raise ValueError("parent evaluation Goal digest is stale")
        if self.accepted_revision.content_digest is None:
            raise ValueError("accepted graph revision has no digest")
        run_ids = {
            self.run_id,
            self.composition_workspace.run_id,
            self.candidate.run_id,
            self.candidate_artifact.run_id,
            *(item.specification.run_id for item in self.verification_bindings),
            *(
                item.process_request.run_id
                for item in self.verification_bindings
                if item.process_request is not None
            ),
        }
        if len(run_ids) != 1:
            raise ValueError("parent evaluation inputs belong to different graph runs")
        if self.candidate.base_commit != self.composition_workspace.head_commit:
            raise ValueError("parent candidate is based on another commit")
        if self.candidate.candidate_patch_digest != self.candidate_artifact.artifact_digest:
            raise ValueError("parent candidate and artifact digest differ")
        if self.candidate_artifact.producer_action_id != self.composition_workspace.id:
            raise ValueError("parent candidate was not produced by the composition workspace")
        source = self.candidate_artifact.source
        if (
            not isinstance(source, Mapping)
            or source.get("base_tree") != self.composition_workspace.base_tree
            or source.get("workspace_digest") != self.composition_workspace.content_digest
        ):
            raise ValueError("parent candidate provenance does not bind the composition workspace")
        evaluator_ids = tuple(item.harness_evaluator_id for item in self.verification_bindings)
        command_refs = tuple(
            item.harness_command_ref
            for item in self.verification_bindings
            if item.harness_command_ref is not None
        )
        request_ids = tuple(
            item.process_request.id
            for item in self.verification_bindings
            if item.process_request is not None
        )
        for label, values in (
            ("Harness evaluator", evaluator_ids),
            ("Harness command", command_refs),
            ("verification request", request_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"parent {label} bindings must be unique")
        return self


class ParentCandidateEvaluationRecord(DigestedRecordV2):
    """Persisted parent decision; readiness never performs promotion."""

    schema_name: ClassVar[str] = "parent_candidate_evaluation_record"
    request_digest: Digest
    accepted_graph_revision_digest: Digest
    composition_record_digest: Digest
    composition_workspace_digest: Digest
    candidate_digest: Digest
    candidate_descriptor_digest: Digest
    candidate_artifact_digest: Digest
    effective_policy_digest: Digest
    verification_request_digests: tuple[Digest, ...] = ()
    verification_result_digests: tuple[Digest, ...] = ()
    evaluation_ledger_digests: tuple[Digest, ...] = ()
    goal_evaluator_digest: Digest
    decision: EvaluationDecision
    status: Literal["ready_to_promote", "failed"]
    failure_code: str | None = None

    @model_validator(mode="after")
    def _readiness_requires_a_pass(self) -> Self:
        ready = self.status == "ready_to_promote"
        if ready != (self.decision is EvaluationDecision.PASS):
            raise ValueError("parent candidate readiness and evaluation decision disagree")
        if ready == (self.failure_code is not None):
            raise ValueError("parent candidate failure code and readiness disagree")
        return self


class ParentCandidateEvaluationReplay(BaseModel):
    """Stored parent evidence without processes, workspaces, composition, or promotion."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    record: ParentCandidateEvaluationRecord
    acceptance_ledger: AcceptanceLedger
    evaluation_ledgers: tuple[EvaluationEvidenceLedger, ...]
    semantic_requests: tuple[ParentSemanticReviewRequest, ...] = ()
    semantic_results: tuple[ParentSemanticReviewResult, ...] = ()
    semantic_decisions: tuple[ParentSemanticReviewDecision, ...] = ()
    semantic_repair_requests: tuple[ParentSemanticRepairRequest, ...] = ()
    process_invocations: Literal[0] = 0
    workspace_reads: Literal[0] = 0
    composition_invocations: Literal[0] = 0
    promotion_invocations: Literal[0] = 0


class GraphCandidateEvaluator:
    """Run only required first-party Harness evaluators in the composition workspace."""

    def __init__(
        self,
        store: SQLiteStore,
        workspace: WorkspaceManager,
        harness: ProjectHarnessV2,
        executor_factory: ExecutorFactory,
        policy_decider: PolicyDecider,
        browser_services_factory: BrowserServicesFactory | None = None,
        semantic_reviewer: ParentSemanticReviewer | None = None,
        semantic_block_severities: tuple[ParentSemanticSeverity, ...] = (
            ParentSemanticSeverity.CRITICAL,
            ParentSemanticSeverity.HIGH,
        ),
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.harness = harness
        self.executor_factory = executor_factory
        self.policy_decider = policy_decider
        self.browser_services_factory = browser_services_factory
        enabled = self.harness.verification.review.parent_semantic_review
        if enabled != (semantic_reviewer is not None):
            raise ValueError("parent semantic review requires Harness and operator opt-in")
        if not semantic_block_severities or len(semantic_block_severities) != len(
            set(semantic_block_severities)
        ):
            raise ValueError("parent semantic-review blocking severities must be unique")
        self.semantic_reviewer = semantic_reviewer
        self.semantic_block_severities = semantic_block_severities

    def evaluate(
        self,
        goal: Goal,
        accepted_revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        *,
        harness_digest: Digest,
        effective_policy_digest: Digest,
        cancellation: Cancellation,
    ) -> ParentCandidateEvaluationRecord:
        request = self._request(
            goal,
            accepted_revision,
            composition,
            harness_digest,
            effective_policy_digest,
        )
        try:
            self._validate_authority(request, composition)
            self._validate_goal_coverage(request)
            self._validate_live_candidate(request)
        except (KeyError, OSError, TypeError, ValueError):
            return self._finish(
                request,
                decision=EvaluationDecision.FAIL,
                failure_code="PARENT_EVALUATION_BINDING_MISMATCH",
            )
        resumed = self._resumable_parent_evaluation(request)
        if resumed is not None:
            return resumed
        self.store.put("parent_candidate_evaluation_request_v2", request, run_id=request.run_id)
        self.store.put("candidate_revision_v2", request.candidate, run_id=request.run_id)

        ledger_digests: list[Digest] = []
        evaluation_ledgers: list[EvaluationEvidenceLedger] = []
        verification_result_digests: list[Digest] = []
        semantic_artifacts: dict[Digest, ArtifactDescriptor] = {}
        criterion_evidence: dict[Identifier, CriterionEvidence] = {}
        for binding in request.verification_bindings:
            specification = binding.specification
            process_request = binding.process_request
            self.store.put("evaluator_specification_v2", specification, run_id=request.run_id)
            scenario = specification.browser_scenario
            evaluation_request = EvaluationRequest(
                id=identifier("parent-evaluation-request"),
                run_id=request.run_id,
                created_at=now(),
                candidate_digest=_required(request.candidate.content_digest),
                generation=request.candidate.generation,
                evaluator_specification_digest=_required(specification.content_digest),
                effective_policy_digest=request.effective_policy_digest,
                remaining_budget=EvaluationBudget(
                    remaining_processes=1 if process_request is not None else 0,
                    remaining_artifact_bytes=self.harness.budgets.artifact_bytes,
                    remaining_actions=0 if scenario is None else len(scenario.actions),
                    remaining_duration_seconds=(
                        0.0 if scenario is None else scenario.timeout_seconds
                    ),
                ),
            )
            self.store.put("evaluation_request_v2", evaluation_request, run_id=request.run_id)
            try:
                provider = DEFAULT_EVALUATOR_REGISTRY.resolve(specification.provider_id)
                if process_request is not None:
                    self.store.put(
                        "verification_request_v2", process_request, run_id=request.run_id
                    )

                    def decide(value: ProcessRequest) -> PolicyDecision:
                        decision = self.policy_decider(value)
                        self.store.put("policy_decision_v2", decision, run_id=request.run_id)
                        return decision

                    process_services = HarnessProcessEvaluationServices(
                        {process_request.id: process_request},
                        self.executor_factory(request.composition_workspace),
                        decide,
                        cancellation,
                        artifact_resolver=_unavailable_artifact,
                        id_factory=identifier,
                        clock=now,
                    )
                    result = provider.evaluate(evaluation_request, specification, process_services)
                    if len(process_services.executions) != 1:
                        raise ValueError(
                            "parent evaluator did not execute exactly one declared request"
                        )
                    execution = process_services.executions[0]
                    self.store.put("verification_result_v2", execution, run_id=request.run_id)
                    runtime_result_digest = _required(execution.content_digest)
                else:
                    if self.browser_services_factory is None:
                        raise RuntimeError("parent browser evaluation is not configured")
                    browser_services = self.browser_services_factory(
                        request.composition_workspace, cancellation
                    )
                    result = provider.evaluate(
                        evaluation_request,
                        specification,
                        cast(EvaluatorServices, browser_services),
                    )
                    if len(browser_services.observations) != 1:
                        raise ValueError(
                            "parent evaluator did not produce exactly one browser observation"
                        )
                    browser_observation = browser_services.observations[0]
                    self.store.put(
                        "browser_observation_v2", browser_observation, run_id=request.run_id
                    )
                    runtime_result_digest = _required(browser_observation.content_digest)
                if result.execution_result_digest != runtime_result_digest:
                    raise ValueError("evaluation result does not cite its exact runtime result")
                for artifact in result.observation_manifest.artifacts:
                    self.store.put("artifact_descriptor_v2", artifact, run_id=request.run_id)
                self.store.put(
                    "observation_manifest_v2",
                    result.observation_manifest,
                    run_id=request.run_id,
                )
                self.store.put("evaluation_result_v2", result, run_id=request.run_id)
                freshness = evaluate_freshness(
                    request.candidate,
                    specification,
                    request.effective_policy_digest,
                    evaluation_request,
                    result,
                )
                decision_value = decide_evaluation(
                    result.criterion_results,
                    result.findings,
                    freshness,
                    evaluation_request.remaining_budget,
                    result.behavior,
                    expected_criterion_ids=specification.criterion_ids,
                )
                ledger = EvaluationEvidenceLedger(
                    id=identifier("parent-evaluation-ledger"),
                    run_id=request.run_id,
                    created_at=now(),
                    candidate_digest=_required(request.candidate.content_digest),
                    generation=request.candidate.generation,
                    evaluator_specification_digest=_required(specification.content_digest),
                    effective_policy_digest=request.effective_policy_digest,
                    evaluation_result_digests=(_required(result.content_digest),),
                    observation_manifest_digests=(
                        _required(result.observation_manifest.content_digest),
                    ),
                    expected_criterion_ids=specification.criterion_ids,
                    criterion_results=result.criterion_results,
                    findings=result.findings,
                    freshness=freshness,
                    remaining_budget=evaluation_request.remaining_budget,
                    behavior=result.behavior,
                    decision=decision_value,
                )
                self.store.put("evaluation_evidence_ledger_v2", ledger, run_id=request.run_id)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                return self._finish(
                    request,
                    decision=EvaluationDecision.FAIL,
                    failure_code="PARENT_EVALUATION_UNAVAILABLE",
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
                    criterion_evidence=criterion_evidence,
                )
            ledger_digest = _required(ledger.content_digest)
            ledger_digests.append(ledger_digest)
            evaluation_ledgers.append(ledger)
            for artifact in result.observation_manifest.artifacts:
                if artifact.redaction_state != "secret":
                    semantic_artifacts[_required(artifact.content_digest)] = artifact
            verification_result_digests.append(runtime_result_digest)
            evidence_refs = (
                ledger_digest,
                _required(result.content_digest),
                _required(result.observation_manifest.content_digest),
                *(item.artifact_digest for item in result.observation_manifest.artifacts),
            )
            for criterion_id in specification.criterion_ids:
                criterion_evidence[criterion_id] = CriterionEvidence(
                    criterion_id=criterion_id,
                    disposition=(
                        "satisfied" if ledger.decision is EvaluationDecision.PASS else "blocked"
                    ),
                    evidence_refs=evidence_refs,
                )
            if ledger.decision is not EvaluationDecision.PASS:
                return self._finish(
                    request,
                    decision=EvaluationDecision.FAIL,
                    failure_code="PARENT_VERIFICATION_FAILED",
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
                    criterion_evidence=criterion_evidence,
                )

        try:
            self._validate_live_candidate(request)
        except (OSError, ValueError):
            return self._finish(
                request,
                decision=EvaluationDecision.FAIL,
                failure_code="PARENT_CANDIDATE_STALE",
                ledger_digests=tuple(ledger_digests),
                verification_result_digests=tuple(verification_result_digests),
                criterion_evidence=criterion_evidence,
            )
        if self.semantic_reviewer is not None:
            try:
                semantic_request = self._semantic_request(
                    request,
                    composition,
                    evaluation_ledgers,
                    criterion_evidence,
                    tuple(semantic_artifacts[digest] for digest in sorted(semantic_artifacts)),
                )
                resumed_request = self._resumable_semantic_request(semantic_request)
            except (KeyError, OSError, TypeError, ValueError):
                return self._finish(
                    request,
                    decision=EvaluationDecision.FAIL,
                    failure_code="PARENT_SEMANTIC_BINDING_MISMATCH",
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
                    criterion_evidence=criterion_evidence,
                )
            if resumed_request is None:
                self.store.put(
                    "parent_semantic_review_request_v2",
                    semantic_request,
                    run_id=request.run_id,
                )
            else:
                semantic_request = resumed_request
            semantic_result: ParentSemanticReviewResult | None = None
            try:
                semantic_result, semantic_decision = self._review_semantics(semantic_request)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError):
                if semantic_result is not None:
                    self.store.put(
                        "stale_parent_semantic_review_result_v2",
                        StaleParentSemanticReviewResult(
                            id=identifier("stale-parent-semantic-review-result"),
                            run_id=request.run_id,
                            created_at=now(),
                            request_digest=_required(semantic_request.content_digest),
                            result_digest=_required(semantic_result.content_digest),
                            expected_graph_revision_digest=(
                                semantic_request.accepted_graph_revision_digest
                            ),
                            result_graph_revision_digest=(
                                semantic_result.accepted_graph_revision_digest
                            ),
                            expected_generation=semantic_request.generation,
                            result_generation=semantic_result.generation,
                            expected_candidate_digest=semantic_request.candidate_digest,
                            result_candidate_digest=semantic_result.candidate_digest,
                            expected_review_attempt=semantic_request.review_attempt,
                            result_review_attempt=semantic_result.review_attempt,
                        ),
                        run_id=request.run_id,
                    )
                semantic_decision = ParentSemanticReviewDecision(
                    id=identifier("parent-semantic-review-decision"),
                    run_id=request.run_id,
                    created_at=now(),
                    request_digest=_required(semantic_request.content_digest),
                    accepted_graph_revision_digest=(
                        semantic_request.accepted_graph_revision_digest
                    ),
                    generation=semantic_request.generation,
                    review_attempt=semantic_request.review_attempt,
                    candidate_digest=semantic_request.candidate_digest,
                    candidate_artifact_digest=semantic_request.candidate_artifact_digest,
                    action=EvaluationDecision.FAIL,
                    reason_code="PARENT_SEMANTIC_REVIEW_UNAVAILABLE",
                )
                self.store.put(
                    "parent_semantic_review_decision_v2",
                    semantic_decision,
                    run_id=request.run_id,
                )
            semantic_evidence = [
                _required(semantic_request.content_digest),
                *(() if semantic_result is None else (_required(semantic_result.content_digest),)),
                _required(semantic_decision.content_digest),
            ]
            repair = self._semantic_repair_request(
                semantic_request, semantic_result, semantic_decision
            )
            if repair is not None:
                self.store.put("parent_semantic_repair_request_v2", repair, run_id=request.run_id)
                semantic_evidence.append(_required(repair.content_digest))
            self._merge_semantic_evidence(
                criterion_evidence,
                semantic_request,
                semantic_result,
                semantic_decision,
                tuple(semantic_evidence),
            )
            if semantic_decision.action is not EvaluationDecision.PASS:
                return self._finish(
                    request,
                    decision=semantic_decision.action,
                    failure_code=semantic_decision.reason_code,
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
                    criterion_evidence=criterion_evidence,
                    semantic_evidence_digests=tuple(semantic_evidence),
                )
        else:
            semantic_evidence = []
        return self._finish(
            request,
            decision=EvaluationDecision.PASS,
            ledger_digests=tuple(ledger_digests),
            verification_result_digests=tuple(verification_result_digests),
            criterion_evidence=criterion_evidence,
            semantic_evidence_digests=tuple(semantic_evidence),
        )

    def replay(self, evaluation_id: Identifier) -> ParentCandidateEvaluationReplay:
        record = self.store.get(
            "parent_candidate_evaluation_v2",
            evaluation_id,
            ParentCandidateEvaluationRecord,
        )
        goal_evaluators = _unique_records(
            item
            for item in self.store.list_records(
                "goal_evaluator_v2", GoalEvaluatorRecord, run_id=record.run_id
            )
            if item.content_digest == record.goal_evaluator_digest
        )
        if len(goal_evaluators) != 1:
            raise ValueError("parent Goal evaluator evidence is missing or ambiguous")
        goal_evaluator = goal_evaluators[0]
        acceptance_ledgers = _unique_records(
            item
            for item in self.store.list_records(
                "acceptance_ledger_v2", AcceptanceLedger, run_id=record.run_id
            )
            if item.content_digest in goal_evaluator.evidence_digests
        )
        if len(acceptance_ledgers) != 1:
            raise ValueError("parent AcceptanceLedger is missing or ambiguous")
        by_digest = {
            item.content_digest: item
            for item in self.store.list_records(
                "evaluation_evidence_ledger_v2",
                EvaluationEvidenceLedger,
                run_id=record.run_id,
            )
        }
        try:
            evaluation_ledgers = tuple(
                by_digest[digest] for digest in record.evaluation_ledger_digests
            )
        except KeyError as error:
            raise ValueError("parent evaluation evidence is missing") from error
        deterministic_prefix = (
            _required(acceptance_ledgers[0].content_digest),
            *record.evaluation_ledger_digests,
        )
        if goal_evaluator.evidence_digests[: len(deterministic_prefix)] != deterministic_prefix:
            raise ValueError("parent Goal evaluator evidence bindings are stale")
        semantic_digest_set = set(
            goal_evaluator.evidence_digests[1 + len(record.evaluation_ledger_digests) :]
        )
        semantic_requests = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_request_v2",
                ParentSemanticReviewRequest,
                run_id=record.run_id,
            )
            if item.content_digest in semantic_digest_set
        )
        semantic_results = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_result_v2",
                ParentSemanticReviewResult,
                run_id=record.run_id,
            )
            if item.content_digest in semantic_digest_set
        )
        semantic_decisions = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_decision_v2",
                ParentSemanticReviewDecision,
                run_id=record.run_id,
            )
            if item.content_digest in semantic_digest_set
        )
        semantic_repairs = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_repair_request_v2",
                ParentSemanticRepairRequest,
                run_id=record.run_id,
            )
            if item.content_digest in semantic_digest_set
        )
        if semantic_digest_set:
            if len(semantic_requests) != 1 or len(semantic_decisions) != 1:
                raise ValueError("parent semantic evidence is missing or ambiguous")
            semantic_request = semantic_requests[0]
            semantic_decision = semantic_decisions[0]
            if semantic_results:
                if len(semantic_results) != 1:
                    raise ValueError("parent semantic result is ambiguous")
                validate_parent_semantic_review_result(semantic_request, semantic_results[0])
                if semantic_decision.result_digest != semantic_results[0].content_digest:
                    raise ValueError("parent semantic decision has a stale result binding")
            elif semantic_decision.result_digest is not None:
                raise ValueError("parent semantic decision result is missing")
            expected_semantic = (
                _required(semantic_request.content_digest),
                *(() if not semantic_results else (_required(semantic_results[0].content_digest),)),
                _required(semantic_decision.content_digest),
                *tuple(_required(item.content_digest) for item in semantic_repairs),
            )
            if (
                goal_evaluator.evidence_digests[1 + len(record.evaluation_ledger_digests) :]
                != expected_semantic
            ):
                raise ValueError("parent semantic Goal evidence bindings are stale")
        return ParentCandidateEvaluationReplay(
            record=record,
            acceptance_ledger=acceptance_ledgers[0],
            evaluation_ledgers=evaluation_ledgers,
            semantic_requests=semantic_requests,
            semantic_results=semantic_results,
            semantic_decisions=semantic_decisions,
            semantic_repair_requests=semantic_repairs,
        )

    def _semantic_request(
        self,
        parent_request: ParentCandidateEvaluationRequest,
        composition: GraphPatchCompositionRecord,
        ledgers: list[EvaluationEvidenceLedger],
        criterion_evidence: Mapping[Identifier, CriterionEvidence],
        artifacts: tuple[ArtifactDescriptor, ...],
    ) -> ParentSemanticReviewRequest:
        assert self.semantic_reviewer is not None
        nodes = tuple(
            sorted(parent_request.accepted_revision.graph.nodes, key=lambda item: item.id)
        )
        graph_digest = _required(parent_request.accepted_revision.content_digest)
        latest: dict[Identifier, NodeExecutionRecord] = {}
        for record in self.store.list_records(
            "node_execution_v2", NodeExecutionRecord, run_id=parent_request.run_id
        ):
            if record.accepted_graph_revision_digest != graph_digest or record.status != "passed":
                continue
            previous = latest.get(record.node_id)
            if previous is None or (
                record.generation,
                record.attempt,
                record.sequence,
                record.created_at,
                record.id,
            ) > (
                previous.generation,
                previous.attempt,
                previous.sequence,
                previous.created_at,
                previous.id,
            ):
                latest[record.node_id] = record
        if set(latest) != {item.id for item in nodes}:
            raise ValueError("parent semantic review lacks exact accepted node executions")
        for record in latest.values():
            _validate_retained_node(self.store, record)
        composition_requests = tuple(
            item
            for item in self.store.list_records(
                "graph_patch_composition_request_v2",
                GraphPatchCompositionRequest,
                run_id=parent_request.run_id,
            )
            if item.content_digest == composition.request_digest
        )
        if len(composition_requests) != 1:
            raise ValueError("parent semantic review lacks one exact composition request")
        patches = {item.node_id: item for item in composition_requests[0].node_patches}
        ordered = {item.node_id: item for item in composition.ordered_inputs}
        if len(patches) != len(composition_requests[0].node_patches) or set(patches) != set(
            ordered
        ):
            raise ValueError("parent semantic review has ambiguous composition inputs")
        for node_id, patch in patches.items():
            node_record = latest.get(node_id)
            binding = ordered[node_id]
            if node_record is None or (
                patch.accepted_graph_revision_digest != graph_digest
                or patch.generation != node_record.output_generation
                or patch.attempt != node_record.attempt
                or patch.worker_request_digest != node_record.worker_request_digest
                or patch.worker_result_digest != node_record.worker_result_digest
                or patch.acceptance_ledger_digest != node_record.acceptance_ledger_digest
                or patch.verification_result_digests != node_record.verification_result_digests
                or patch.patch.id != node_record.patch_artifact_id
                or patch.patch.content_digest != node_record.patch_descriptor_digest
                or patch.patch.artifact_digest != node_record.patch_digest
                or binding.worker_request_digest != patch.worker_request_digest
                or binding.worker_result_digest != patch.worker_result_digest
                or binding.acceptance_ledger_digest != patch.acceptance_ledger_digest
                or binding.verification_result_digests != patch.verification_result_digests
                or binding.patch_artifact_id != patch.patch.id
                or binding.patch_descriptor_digest != patch.patch.content_digest
                or binding.patch_digest != patch.patch.artifact_digest
            ):
                raise ValueError("parent semantic review composition input is stale")
        return ParentSemanticReviewRequest(
            id=identifier("parent-semantic-review-request"),
            run_id=parent_request.run_id,
            created_at=now(),
            goal=parent_request.goal,
            goal_digest=parent_request.goal_digest,
            accepted_revision=parent_request.accepted_revision,
            accepted_graph_revision_digest=graph_digest,
            generation=parent_request.accepted_revision.revision_number,
            review_attempt=0,
            reviewer_strategy=self.semantic_reviewer.strategy,
            harness_digest=parent_request.harness_digest,
            effective_policy_digest=parent_request.effective_policy_digest,
            composition_record_digest=parent_request.composition_record_digest,
            composition_workspace_digest=_required(
                parent_request.composition_workspace.content_digest
            ),
            candidate_digest=_required(parent_request.candidate.content_digest),
            candidate_descriptor=parent_request.candidate_artifact,
            candidate_descriptor_digest=_required(parent_request.candidate_artifact.content_digest),
            candidate_artifact_digest=parent_request.candidate_artifact.artifact_digest,
            node_bindings=tuple(
                ParentNodeReviewBinding(
                    node_id=node.id,
                    generation=latest[node.id].generation,
                    result_generation=_required_int(latest[node.id].output_generation),
                    attempt=latest[node.id].attempt,
                    objective_digest=canonical_digest(node.objective or node.name),
                    completion_criteria_digest=canonical_digest(node.completion_criteria),
                    worker_request_digest=_required(latest[node.id].worker_request_digest),
                    worker_result_digest=_required(latest[node.id].worker_result_digest),
                    evidence_digest=_required(latest[node.id].evidence_digest),
                    evaluator_digest=_required(latest[node.id].evaluator_digest),
                )
                for node in nodes
            ),
            deterministic_ledgers=tuple(ledgers),
            deterministic_ledger_digests=tuple(_required(item.content_digest) for item in ledgers),
            criterion_evidence=tuple(
                criterion_evidence[item.id]
                for item in sorted(
                    parent_request.goal.completion_criteria, key=lambda item: item.id
                )
            ),
            artifact_descriptors=artifacts,
        )

    def _review_semantics(
        self,
        request: ParentSemanticReviewRequest,
    ) -> tuple[ParentSemanticReviewResult, ParentSemanticReviewDecision]:
        assert self.semantic_reviewer is not None
        prior_results = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_result_v2",
                ParentSemanticReviewResult,
                run_id=request.run_id,
            )
            if item.request_digest == request.content_digest
        )
        prior_decisions = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_decision_v2",
                ParentSemanticReviewDecision,
                run_id=request.run_id,
            )
            if item.request_digest == request.content_digest and item.result_digest is not None
        )
        if prior_results or prior_decisions:
            if len(prior_results) != 1 or len(prior_decisions) != 1:
                raise ValueError("resumed parent semantic evidence is ambiguous")
            result, decision = prior_results[0], prior_decisions[0]
            validate_parent_semantic_review_result(request, result)
            expected = decide_parent_semantic_review(
                request,
                result,
                block_severities=self.semantic_block_severities,
                decision_id=decision.id,
                run_id=decision.run_id,
                created_at=decision.created_at,
            )
            if expected != decision:
                raise ValueError("resumed parent semantic decision is not deterministic")
            return result, decision
        result = self.semantic_reviewer.review(request)
        try:
            validate_parent_semantic_review_result(request, result)
        except ValueError:
            self.store.put(
                "stale_parent_semantic_review_result_v2",
                StaleParentSemanticReviewResult(
                    id=identifier("stale-parent-semantic-review-result"),
                    run_id=request.run_id,
                    created_at=now(),
                    request_digest=_required(request.content_digest),
                    result_digest=_required(result.content_digest),
                    expected_graph_revision_digest=request.accepted_graph_revision_digest,
                    result_graph_revision_digest=result.accepted_graph_revision_digest,
                    expected_generation=request.generation,
                    result_generation=result.generation,
                    expected_candidate_digest=request.candidate_digest,
                    result_candidate_digest=result.candidate_digest,
                    expected_review_attempt=request.review_attempt,
                    result_review_attempt=result.review_attempt,
                ),
                run_id=request.run_id,
            )
            raise
        self.store.put("parent_semantic_review_result_v2", result, run_id=request.run_id)
        decision = decide_parent_semantic_review(
            request,
            result,
            block_severities=self.semantic_block_severities,
            decision_id=identifier("parent-semantic-review-decision"),
            run_id=request.run_id,
            created_at=now(),
        )
        self.store.put("parent_semantic_review_decision_v2", decision, run_id=request.run_id)
        return result, decision

    def _resumable_semantic_request(
        self,
        current: ParentSemanticReviewRequest,
    ) -> ParentSemanticReviewRequest | None:
        candidates = _unique_records(
            item
            for item in self.store.list_records(
                "parent_semantic_review_request_v2",
                ParentSemanticReviewRequest,
                run_id=current.run_id,
            )
            if item.content_digest == current.content_digest
        )
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("resumed parent semantic request is ambiguous")
        candidate = candidates[0]
        persisted_ledgers = {
            item.content_digest
            for item in self.store.list_records(
                "evaluation_evidence_ledger_v2",
                EvaluationEvidenceLedger,
                run_id=current.run_id,
            )
        }
        if not set(candidate.deterministic_ledger_digests) <= persisted_ledgers:
            raise ValueError("resumed parent semantic request has missing deterministic evidence")
        return candidate

    def _resumable_parent_evaluation(
        self,
        current: ParentCandidateEvaluationRequest,
    ) -> ParentCandidateEvaluationRecord | None:
        """Reuse only a complete evaluation of the exact persisted composition."""

        candidates = _unique_records(
            item
            for item in self.store.list_records(
                "parent_candidate_evaluation_v2",
                ParentCandidateEvaluationRecord,
                run_id=current.run_id,
            )
            if (
                (
                    item.decision is EvaluationDecision.PASS
                    or item.failure_code
                    in {
                        "PARENT_VERIFICATION_FAILED",
                        "PARENT_SEMANTIC_REPAIR",
                        "PARENT_SEMANTIC_COVERAGE_LIMITED",
                        "PARENT_SEMANTIC_OPERATOR_REQUIRED",
                        "PARENT_SEMANTIC_UNRECOVERABLE",
                    }
                )
                and item.accepted_graph_revision_digest == current.accepted_revision.content_digest
                and item.composition_record_digest == current.composition_record_digest
                and item.composition_workspace_digest
                == current.composition_workspace.content_digest
                and item.candidate_digest == current.candidate.content_digest
                and item.candidate_descriptor_digest == current.candidate_artifact.content_digest
                and item.candidate_artifact_digest == current.candidate_artifact.artifact_digest
                and item.effective_policy_digest == current.effective_policy_digest
            )
        )
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("resumed parent candidate evaluation is ambiguous")
        candidate = candidates[0]
        requests = _unique_records(
            item
            for item in self.store.list_records(
                "parent_candidate_evaluation_request_v2",
                ParentCandidateEvaluationRequest,
                run_id=current.run_id,
            )
            if item.content_digest == candidate.request_digest
        )
        if len(requests) != 1:
            raise ValueError("resumed parent evaluation request is missing or ambiguous")
        previous = requests[0]
        if (
            previous.goal != current.goal
            or previous.goal_digest != current.goal_digest
            or previous.harness_digest != current.harness_digest
            or previous.accepted_revision != current.accepted_revision
            or previous.composition_record_digest != current.composition_record_digest
            or previous.composition_workspace != current.composition_workspace
            or previous.candidate_artifact != current.candidate_artifact
        ):
            raise ValueError("resumed parent evaluation has stale authority bindings")
        self.replay(candidate.id)
        return candidate

    def _semantic_repair_request(
        self,
        request: ParentSemanticReviewRequest,
        result: ParentSemanticReviewResult | None,
        decision: ParentSemanticReviewDecision,
    ) -> ParentSemanticRepairRequest | None:
        if decision.action is not EvaluationDecision.REPAIR:
            return None
        if result is None:
            raise ValueError("parent semantic REPAIR has no accepted result")
        accepted = {
            canonical_digest(item): item
            for item in result.findings
            if canonical_digest(item) in decision.accepted_finding_digests
        }
        if set(accepted) != set(decision.accepted_finding_digests):
            raise ValueError("parent semantic REPAIR findings are stale")
        objectives = tuple(
            sorted(
                {
                    item.repair_objective
                    for item in accepted.values()
                    if item.repair_objective is not None
                }
            )
        )
        if not objectives:
            raise ValueError("parent semantic REPAIR has no bounded objective")
        return ParentSemanticRepairRequest(
            id=identifier("parent-semantic-repair-request"),
            run_id=request.run_id,
            created_at=now(),
            review_request_digest=_required(request.content_digest),
            review_result_digest=_required(result.content_digest),
            review_decision_digest=_required(decision.content_digest),
            accepted_graph_revision_digest=request.accepted_graph_revision_digest,
            generation=request.generation,
            review_attempt=request.review_attempt,
            candidate_digest=request.candidate_digest,
            candidate_artifact_digest=request.candidate_artifact_digest,
            affected_node_ids=tuple(
                sorted({node_id for item in accepted.values() for node_id in item.node_ids})
            ),
            accepted_finding_digests=decision.accepted_finding_digests,
            repair_objectives=objectives,
        )

    def _merge_semantic_evidence(
        self,
        criterion_evidence: dict[Identifier, CriterionEvidence],
        request: ParentSemanticReviewRequest,
        result: ParentSemanticReviewResult | None,
        decision: ParentSemanticReviewDecision,
        evidence_digests: tuple[Digest, ...],
    ) -> None:
        affected = (
            set(request.criterion_ids)
            if result is None or result.limitations
            else {
                criterion_id
                for finding in result.findings
                if canonical_digest(finding) in decision.accepted_finding_digests
                for criterion_id in finding.criterion_ids
            }
        )
        finding_digests = decision.accepted_finding_digests
        for criterion_id in request.criterion_ids:
            current = criterion_evidence[criterion_id]
            criterion_evidence[criterion_id] = CriterionEvidence(
                criterion_id=criterion_id,
                disposition=(
                    "blocked"
                    if decision.action is not EvaluationDecision.PASS and criterion_id in affected
                    else current.disposition
                ),
                evidence_refs=tuple(
                    sorted({*current.evidence_refs, *evidence_digests, *finding_digests})
                ),
            )

    def _request(
        self,
        goal: Goal,
        accepted_revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        harness_digest: Digest,
        effective_policy_digest: Digest,
    ) -> ParentCandidateEvaluationRequest:
        if composition.status != "succeeded":
            raise ValueError("failed composition cannot be evaluated")
        if composition.composition_workspace is None or composition.candidate_patch is None:
            raise ValueError("composition has no parent candidate")
        by_id = {item.id: item for item in self.harness.evaluators}
        bindings: list[ParentVerificationBinding] = []
        for evaluator_id in self.harness.verification.required_evaluators:
            declaration = by_id[evaluator_id]
            provider = DEFAULT_EVALUATOR_REGISTRY.resolve(declaration.provider_id)
            if declaration.provider_id == "browser.playwright":
                scenario = declaration.browser_scenario
                if scenario is None:
                    raise ValueError("required browser evaluator has no scenario")
                bindings.append(
                    ParentVerificationBinding(
                        harness_evaluator_id=declaration.id,
                        specification=EvaluatorSpecification(
                            id=identifier("parent-evaluator-specification"),
                            run_id=composition.run_id,
                            created_at=now(),
                            provider_id=provider.descriptor.provider_id,
                            provider_schema_version=provider.descriptor.provider_schema_version,
                            provider_descriptor_digest=versioned_digest(provider.descriptor),
                            behavior=provider.descriptor.behavior,
                            required_capabilities=provider.descriptor.required_capabilities,
                            requested_observation_kinds=tuple(
                                item.logical_kind for item in scenario.captures
                            ),
                            browser_scenario=scenario,
                            criterion_ids=declaration.criterion_ids,
                        ),
                    )
                )
                continue
            if declaration.command_ref is None:
                raise ValueError("required parent evaluator has no Harness command")
            command = self.harness.commands[declaration.command_ref]
            process_request = ProcessRequest(
                id=identifier("parent-verification-request"),
                run_id=composition.run_id,
                created_at=now(),
                argv=command.argv,
                cwd=command.cwd,
                inherit_environment=command.inherit_environment,
                timeout_seconds=min(300.0, self.harness.budgets.wall_seconds),
                budget_class="parent-verification",
                purpose=f"required parent Harness verification: {declaration.command_ref}",
            )
            specification = EvaluatorSpecification(
                id=identifier("parent-evaluator-specification"),
                run_id=composition.run_id,
                created_at=now(),
                provider_id=provider.descriptor.provider_id,
                provider_schema_version=provider.descriptor.provider_schema_version,
                provider_descriptor_digest=versioned_digest(provider.descriptor),
                behavior=provider.descriptor.behavior,
                required_capabilities=provider.descriptor.required_capabilities,
                requested_observation_kinds=(),
                command_ref=process_request.id,
                criterion_ids=declaration.criterion_ids,
            )
            bindings.append(
                ParentVerificationBinding(
                    harness_evaluator_id=declaration.id,
                    harness_command_ref=declaration.command_ref,
                    specification=specification,
                    process_request=process_request,
                )
            )
        candidate = CandidateRevision(
            id=identifier("parent-candidate"),
            run_id=composition.run_id,
            created_at=now(),
            generation=accepted_revision.revision_number,
            base_commit=composition.base_commit,
            candidate_patch_digest=composition.candidate_patch.artifact_digest,
        )
        return ParentCandidateEvaluationRequest(
            id=identifier("parent-candidate-evaluation-request"),
            run_id=composition.run_id,
            created_at=now(),
            goal=goal,
            goal_digest=canonical_digest(goal),
            harness_digest=harness_digest,
            accepted_revision=accepted_revision,
            composition_id=composition.id,
            composition_request_digest=composition.request_digest,
            composition_record_digest=_required(composition.content_digest),
            composition_workspace=composition.composition_workspace,
            candidate=candidate,
            candidate_artifact=composition.candidate_patch,
            effective_policy_digest=effective_policy_digest,
            verification_bindings=tuple(bindings),
        )

    def _validate_authority(
        self,
        request: ParentCandidateEvaluationRequest,
        composition: GraphPatchCompositionRecord,
    ) -> None:
        if (
            self.harness.provisional
            or project_harness_digest(self.harness) != request.harness_digest
        ):
            raise ValueError("parent Harness is provisional or stale")
        if self.harness.budgets.processes < len(request.verification_bindings):
            raise ValueError("parent verification process budget is exhausted")
        persisted_run = self.store.get("graph_run_v2", request.run_id, GraphRunRecord)
        if (
            persisted_run.goal_id != request.goal.id
            or persisted_run.accepted_graph_revision_digest
            != request.accepted_revision.content_digest
        ):
            raise ValueError("parent evaluation is not bound to the exact graph run")
        acceptances = self.store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=request.run_id
        )
        matching_acceptances = tuple(
            item
            for item in acceptances
            if item.accepted_revision == request.accepted_revision
            and item.harness_digest == request.harness_digest
            and item.effective_policy_digest == request.effective_policy_digest
        )
        if len(matching_acceptances) != 1:
            raise ValueError("accepted graph authority is missing or stale")
        persisted_composition = self.store.get(
            "graph_patch_composition_v2", composition.id, GraphPatchCompositionRecord
        )
        if (
            persisted_composition != composition
            or composition.content_digest != request.composition_record_digest
        ):
            raise ValueError("composition record is missing or stale")
        composition_requests = self.store.list_records(
            "graph_patch_composition_request_v2",
            GraphPatchCompositionRequest,
            run_id=request.run_id,
        )
        matching_requests = tuple(
            item
            for item in composition_requests
            if item.content_digest == request.composition_request_digest
        )
        if len(matching_requests) != 1:
            raise ValueError("composition request is missing or ambiguous")
        composition_request = matching_requests[0]
        if (
            composition_request.accepted_revision != request.accepted_revision
            or composition_request.effective_policy_digest != request.effective_policy_digest
            or composition_request.base_commit != request.candidate.base_commit
        ):
            raise ValueError("composition request has stale parent bindings")
        persisted_workspace = self.store.get(
            "workspace_v2", request.composition_workspace.id, WorkspaceSnapshot
        )
        persisted_artifact = self.store.get(
            "artifact_descriptor_v2", request.candidate_artifact.id, ArtifactDescriptor
        )
        if (
            persisted_workspace != request.composition_workspace
            or persisted_artifact != request.candidate_artifact
        ):
            raise ValueError("composition workspace or candidate artifact is missing")

    def _validate_goal_coverage(self, request: ParentCandidateEvaluationRequest) -> None:
        expected = tuple(item.id for item in request.goal.completion_criteria)
        covered = tuple(
            criterion_id
            for binding in request.verification_bindings
            for criterion_id in binding.specification.criterion_ids
        )
        required_commands = tuple(
            item.harness_command_ref
            for item in request.verification_bindings
            if item.harness_command_ref is not None
        )
        if (
            not expected
            or len(covered) != len(set(covered))
            or set(covered) != set(expected)
            or set(required_commands) != set(self.harness.verification.required)
        ):
            raise ValueError("required parent Goal criteria do not have exact Harness coverage")
        by_criterion = {
            criterion_id: binding.harness_command_ref
            for binding in request.verification_bindings
            for criterion_id in binding.specification.criterion_ids
        }
        artifact_names = {
            request.candidate_artifact.id,
            request.candidate_artifact.logical_kind,
        }
        for criterion in request.goal.completion_criteria:
            command_ref = by_criterion[criterion.id]
            expected_requirements = () if command_ref is None else (command_ref,)
            if set(criterion.verification_requirement_ids) != set(expected_requirements):
                raise ValueError("Goal criterion verification binding is missing or stale")
            if not set(criterion.required_artifact_ids) <= artifact_names:
                raise ValueError("Goal criterion requires unavailable candidate artifacts")

    def _validate_live_candidate(self, request: ParentCandidateEvaluationRequest) -> None:
        self.workspace.adopt(request.composition_workspace)
        current = self.workspace.capture_diff(request.composition_workspace)
        if current.artifact_digest != request.candidate_artifact.artifact_digest:
            raise ValueError("composition workspace no longer contains the exact candidate")

    def _finish(
        self,
        request: ParentCandidateEvaluationRequest,
        *,
        decision: EvaluationDecision,
        failure_code: str | None = None,
        ledger_digests: tuple[Digest, ...] = (),
        verification_result_digests: tuple[Digest, ...] = (),
        criterion_evidence: Mapping[Identifier, CriterionEvidence] | None = None,
        semantic_evidence_digests: tuple[Digest, ...] = (),
    ) -> ParentCandidateEvaluationRecord:
        criterion_evidence = criterion_evidence or {}
        acceptance_ledger = AcceptanceLedger(
            id=identifier("parent-acceptance-ledger"),
            run_id=request.run_id,
            created_at=now(),
            criteria=tuple(
                criterion_evidence.get(
                    criterion.id,
                    CriterionEvidence(
                        criterion_id=criterion.id,
                        disposition="uncovered",
                    ),
                )
                for criterion in request.goal.completion_criteria
            ),
        )
        self.store.put("acceptance_ledger_v2", acceptance_ledger, run_id=request.run_id)
        goal_evaluation = GoalEvaluatorRecord(
            id=identifier("parent-goal-evaluation"),
            run_id=request.run_id,
            created_at=now(),
            goal_id=request.goal.id,
            accepted_graph_revision_digest=_required(request.accepted_revision.content_digest),
            evidence_digests=(
                _required(acceptance_ledger.content_digest),
                *ledger_digests,
                *semantic_evidence_digests,
            ),
            decision=decision,
        )
        self.store.put("goal_evaluator_v2", goal_evaluation, run_id=request.run_id)
        record = ParentCandidateEvaluationRecord(
            id=identifier("parent-candidate-evaluation"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=_required(request.content_digest),
            accepted_graph_revision_digest=_required(request.accepted_revision.content_digest),
            composition_record_digest=request.composition_record_digest,
            composition_workspace_digest=_required(request.composition_workspace.content_digest),
            candidate_digest=_required(request.candidate.content_digest),
            candidate_descriptor_digest=_required(request.candidate_artifact.content_digest),
            candidate_artifact_digest=request.candidate_artifact.artifact_digest,
            effective_policy_digest=request.effective_policy_digest,
            verification_request_digests=tuple(
                _required(item.process_request.content_digest)
                for item in request.verification_bindings
                if item.process_request is not None
            ),
            verification_result_digests=verification_result_digests,
            evaluation_ledger_digests=ledger_digests,
            goal_evaluator_digest=_required(goal_evaluation.content_digest),
            decision=decision,
            status=("ready_to_promote" if decision is EvaluationDecision.PASS else "failed"),
            failure_code=failure_code,
        )
        self.store.put("parent_candidate_evaluation_v2", record, run_id=request.run_id)
        return record


def _required(value: str | None) -> str:
    if value is None:
        raise ValueError("authoritative parent evaluation record is missing its digest")
    return value


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError("authoritative parent evaluation record is missing its generation")
    return value


def _unique_records(values: Iterable[RecordT]) -> tuple[RecordT, ...]:
    """Deduplicate immutable metadata variants by their validated content identity."""

    by_digest: dict[Digest, RecordT] = {}
    for value in values:
        by_digest.setdefault(_required(value.content_digest), value)
    return tuple(by_digest[digest] for digest in sorted(by_digest))


def _unavailable_artifact(
    _digest: Digest,
    _logical_kind: Identifier,
    _execution_id: Identifier,
) -> ArtifactDescriptor:
    raise ValueError("parent evaluator did not declare process-output observations")
