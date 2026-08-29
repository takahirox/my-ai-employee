"""Authoritative evaluation of one exact composed graph candidate."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar, Literal, Self

from pydantic import ConfigDict, Field, model_validator
from pydantic.main import BaseModel

from .domain import Goal, ProjectHarnessV2
from .domain.base import Digest, Identifier
from .domain.evaluation import (
    CandidateRevision,
    EvaluationBudget,
    EvaluationDecision,
    EvaluationEvidenceLedger,
    EvaluationRequest,
    EvaluatorSpecification,
    decide_evaluation,
    evaluate_freshness,
)
from .domain.models import AcceptedGraphRevision
from .domain.services_v2 import Cancellation, ProcessExecutor, WorkspaceManager
from .domain.v2 import (
    ArtifactDescriptor,
    DigestedRecordV2,
    PolicyDecision,
    ProcessRequest,
    WorkspaceSnapshot,
)
from .evaluators import DEFAULT_EVALUATOR_REGISTRY, HarnessProcessEvaluationServices
from .graph_composition import GraphPatchCompositionRecord, GraphPatchCompositionRequest
from .serialization import canonical_digest, versioned_digest
from .services_v2._common import identifier, now
from .storage import SQLiteStore
from .task_orchestration import (
    GoalEvaluatorRecord,
    GraphRunRecord,
    TaskGraphAcceptance,
)

PolicyDecider = Callable[[ProcessRequest], PolicyDecision]
ExecutorFactory = Callable[[WorkspaceSnapshot], ProcessExecutor]


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
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.harness = harness
        self.executor_factory = executor_factory
        self.policy_decider = policy_decider

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
        self.store.put("parent_candidate_evaluation_request_v2", request, run_id=request.run_id)
        self.store.put("candidate_revision_v2", request.candidate, run_id=request.run_id)
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

        executor = self.executor_factory(request.composition_workspace)
        ledger_digests: list[Digest] = []
        verification_result_digests: list[Digest] = []
        for binding in request.verification_bindings:
            specification = binding.specification
            process_request = binding.process_request
            if process_request is None:
                return self._finish(
                    request,
                    decision=EvaluationDecision.FAIL,
                    failure_code="PARENT_EVALUATION_UNAVAILABLE",
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
                )
            self.store.put("evaluator_specification_v2", specification, run_id=request.run_id)
            self.store.put("verification_request_v2", process_request, run_id=request.run_id)
            evaluation_request = EvaluationRequest(
                id=identifier("parent-evaluation-request"),
                run_id=request.run_id,
                created_at=now(),
                candidate_digest=_required(request.candidate.content_digest),
                generation=request.candidate.generation,
                evaluator_specification_digest=_required(specification.content_digest),
                effective_policy_digest=request.effective_policy_digest,
                remaining_budget=EvaluationBudget(
                    remaining_processes=1,
                    remaining_artifact_bytes=self.harness.budgets.artifact_bytes,
                ),
            )
            self.store.put("evaluation_request_v2", evaluation_request, run_id=request.run_id)

            def decide(value: ProcessRequest) -> PolicyDecision:
                decision = self.policy_decider(value)
                self.store.put("policy_decision_v2", decision, run_id=request.run_id)
                return decision

            services = HarnessProcessEvaluationServices(
                {process_request.id: process_request},
                executor,
                decide,
                cancellation,
                artifact_resolver=_unavailable_artifact,
                id_factory=identifier,
                clock=now,
            )
            try:
                provider = DEFAULT_EVALUATOR_REGISTRY.resolve(specification.provider_id)
                result = provider.evaluate(evaluation_request, specification, services)
                if len(services.executions) != 1:
                    raise ValueError(
                        "parent evaluator did not execute exactly one declared request"
                    )
                execution = services.executions[0]
                self.store.put("verification_result_v2", execution, run_id=request.run_id)
                if result.execution_result_digest != execution.content_digest:
                    raise ValueError("evaluation result does not cite its exact process result")
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
                )
            ledger_digests.append(_required(ledger.content_digest))
            verification_result_digests.append(_required(execution.content_digest))
            if ledger.decision is not EvaluationDecision.PASS:
                return self._finish(
                    request,
                    decision=EvaluationDecision.FAIL,
                    failure_code="PARENT_VERIFICATION_FAILED",
                    ledger_digests=tuple(ledger_digests),
                    verification_result_digests=tuple(verification_result_digests),
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
            )
        return self._finish(
            request,
            decision=EvaluationDecision.PASS,
            ledger_digests=tuple(ledger_digests),
            verification_result_digests=tuple(verification_result_digests),
        )

    def replay(self, evaluation_id: Identifier) -> ParentCandidateEvaluationReplay:
        record = self.store.get(
            "parent_candidate_evaluation_v2",
            evaluation_id,
            ParentCandidateEvaluationRecord,
        )
        return ParentCandidateEvaluationReplay(record=record)

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
        if self.harness.provisional or canonical_digest(self.harness) != request.harness_digest:
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
    ) -> ParentCandidateEvaluationRecord:
        goal_evaluation = GoalEvaluatorRecord(
            id=identifier("parent-goal-evaluation"),
            run_id=request.run_id,
            created_at=now(),
            goal_id=request.goal.id,
            accepted_graph_revision_digest=_required(request.accepted_revision.content_digest),
            evidence_digests=ledger_digests,
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


def _unavailable_artifact(
    _digest: Digest,
    _logical_kind: Identifier,
    _execution_id: Identifier,
) -> ArtifactDescriptor:
    raise ValueError("parent evaluator did not declare process-output observations")
