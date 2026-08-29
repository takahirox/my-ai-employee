"""Developer-managed first-party evaluator implementations and registry."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from types import MappingProxyType

from .browser_evaluator import BrowserPlaywrightEvaluator
from .domain.base import Digest, Identifier
from .domain.evaluation import (
    AVAILABLE_FIRST_PARTY_EVALUATOR_IDS,
    PROCESS_EVALUATOR_ID,
    RESERVED_EVALUATOR_IDS,
    CriterionOutcome,
    CriterionResult,
    EvaluationFinding,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorBehavior,
    EvaluatorDescriptor,
    EvaluatorLimits,
    EvaluatorProvider,
    EvaluatorServices,
    EvaluatorSpecification,
    FindingSeverity,
    ObservationManifest,
)
from .domain.services_v2 import Cancellation, ProcessExecutor
from .domain.v2 import (
    ArtifactDescriptor,
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from .serialization import versioned_digest


class HarnessProcessEvaluationServices:
    """Expose only predeclared Harness commands through mediated process services."""

    def __init__(
        self,
        commands: Mapping[Identifier, ProcessRequest],
        executor: ProcessExecutor,
        decide: Callable[[ProcessRequest], PolicyDecision],
        cancellation: Cancellation,
        *,
        artifact_resolver: Callable[[Digest, Identifier, Identifier], ArtifactDescriptor],
        id_factory: Callable[[str], Identifier],
        clock: Callable[[], datetime],
    ) -> None:
        self._commands = MappingProxyType(dict(commands))
        self._executor = executor
        self._decide = decide
        self._cancellation = cancellation
        self._artifact_resolver = artifact_resolver
        self._id_factory = id_factory
        self._clock = clock
        self._executions: list[ExecutionResult] = []
        self._execution_requests: dict[Identifier, ProcessRequest] = {}

    @property
    def executions(self) -> tuple[ExecutionResult, ...]:
        return tuple(self._executions)

    def new_id(self, prefix: str) -> Identifier:
        return self._id_factory(prefix)

    def created_at(self) -> datetime:
        return self._clock()

    def artifact_descriptor(
        self,
        artifact_digest: Digest,
        logical_kind: Identifier,
        producer_execution_id: Identifier,
    ) -> ArtifactDescriptor:
        try:
            command = self._execution_requests[producer_execution_id]
        except KeyError as error:
            raise ValueError("artifact names an unknown process execution") from error
        descriptor = self._artifact_resolver(artifact_digest, logical_kind, producer_execution_id)
        if descriptor.artifact_digest != artifact_digest:
            raise ValueError("artifact resolver returned a mismatched descriptor")
        if descriptor.logical_kind != logical_kind:
            raise ValueError("artifact resolver returned another observation kind")
        if descriptor.run_id != command.run_id:
            raise ValueError("process artifact belongs to another run")
        if descriptor.producer_action_id != command.id:
            raise ValueError("process artifact belongs to another declared request")
        if not isinstance(descriptor.source, Mapping):
            raise ValueError("process artifact has no provenance")
        if descriptor.source.get("request_digest") != command.content_digest:
            raise ValueError("process artifact belongs to another process request")
        if descriptor.source.get("execution_id") != producer_execution_id:
            raise ValueError("process artifact belongs to another execution")
        return descriptor

    def execute_declared_process(
        self, command_ref: Identifier, request: EvaluationRequest
    ) -> ExecutionResult:
        try:
            command = self._commands[command_ref]
        except KeyError as error:
            raise ValueError(f"unknown declared evaluation command: {command_ref}") from error
        if command.run_id != request.run_id:
            raise ValueError("declared evaluation command belongs to another run")
        decision = self._decide(command)
        if decision.request_digest != command.content_digest:
            raise ValueError("process policy decision is not bound to the declared command")
        if decision.run_id != command.run_id:
            raise ValueError("process policy decision belongs to another run")
        if decision.effective_policy_digest != request.effective_policy_digest:
            raise ValueError("process policy decision is stale for the evaluation request")
        if decision.outcome is not DecisionOutcome.ALLOW:
            raise ValueError("declared evaluation command was not allowed by effective policy")
        result = self._executor.execute(command, decision, self._cancellation)
        if result.run_id != command.run_id:
            raise ValueError("process execution belongs to another run")
        if result.request_digest != command.content_digest:
            raise ValueError("process execution belongs to another declared request")
        if result.id in self._execution_requests:
            raise ValueError("process executor reused an execution identifier")
        self._execution_requests[result.id] = command
        self._executions.append(result)
        return result


class ProcessEvaluator:
    """Evaluate an exact candidate using one declared Harness command."""

    descriptor = EvaluatorDescriptor(
        provider_id=PROCESS_EVALUATOR_ID,
        provider_schema_version="v1",
        behavior=EvaluatorBehavior.DETERMINISTIC,
        required_capabilities=("process",),
        supported_observation_kinds=("process_stdout", "process_stderr"),
        limits=EvaluatorLimits(
            maximum_processes=1,
            maximum_artifact_bytes=2_000_000,
            maximum_observations=2,
        ),
    )

    @property
    def descriptor_digest(self) -> Digest:
        return versioned_digest(self.descriptor)

    def evaluate(
        self,
        request: EvaluationRequest,
        specification: EvaluatorSpecification,
        services: EvaluatorServices,
    ) -> EvaluationResult:
        self._validate_specification(specification)
        if request.run_id != specification.run_id:
            raise ValueError("evaluation request and specification belong to different runs")
        if request.evaluator_specification_digest != specification.content_digest:
            raise ValueError("evaluation request is stale for its evaluator specification")
        if specification.command_ref is None:
            raise ValueError("process evaluator requires a declared command reference")
        limits = self.descriptor.limits
        if request.remaining_budget.remaining_processes < 1:
            raise ValueError("evaluation process budget is exhausted")
        if limits.maximum_processes < 1:
            raise ValueError("evaluator process limit is exhausted")
        if (
            specification.requested_observation_kinds
            and request.remaining_budget.remaining_artifact_bytes < 1
        ):
            raise ValueError("evaluation artifact budget is exhausted")
        if specification.requested_observation_kinds and limits.maximum_artifact_bytes < 1:
            raise ValueError("evaluator artifact limit is exhausted")
        if len(specification.requested_observation_kinds) > limits.maximum_observations:
            raise ValueError("evaluator observation limit would be exceeded")
        raw_execution = services.execute_declared_process(specification.command_ref, request)
        if not isinstance(raw_execution, ExecutionResult):
            raise TypeError("process evaluator service returned an invalid execution result")
        execution = raw_execution
        if execution.run_id != request.run_id:
            raise ValueError("process execution belongs to another run")
        artifacts_list: list[ArtifactDescriptor] = []
        for logical_kind, digest in (
            ("process_stdout", execution.stdout_artifact_digest),
            ("process_stderr", execution.stderr_artifact_digest),
        ):
            if digest is None or logical_kind not in specification.requested_observation_kinds:
                continue
            descriptor = services.artifact_descriptor(digest, logical_kind, execution.id)
            if descriptor.run_id != request.run_id:
                raise ValueError("process artifact belongs to another run")
            if descriptor.logical_kind != logical_kind:
                raise ValueError("process artifact has the wrong observation kind")
            if not isinstance(descriptor.source, Mapping):
                raise ValueError("process artifact has no provenance")
            if descriptor.source.get("request_digest") != execution.request_digest:
                raise ValueError("process artifact belongs to another process request")
            if descriptor.source.get("execution_id") != execution.id:
                raise ValueError("process artifact belongs to another execution")
            artifacts_list.append(descriptor)
        artifacts = tuple(artifacts_list)
        if len(artifacts) > limits.maximum_observations:
            raise ValueError("evaluator observation limit was exceeded")
        artifact_bytes = sum(item.size_bytes for item in artifacts)
        if artifact_bytes > request.remaining_budget.remaining_artifact_bytes:
            raise ValueError("evaluation artifact byte budget was exceeded")
        if artifact_bytes > limits.maximum_artifact_bytes:
            raise ValueError("evaluator artifact byte limit was exceeded")
        artifact_digests = tuple(dict.fromkeys(item.artifact_digest for item in artifacts))
        manifest = ObservationManifest(
            id=services.new_id("observation-manifest"),
            run_id=request.run_id,
            created_at=services.created_at(),
            request_digest=request.content_digest or "",
            candidate_digest=request.candidate_digest,
            generation=request.generation,
            evaluator_specification_digest=request.evaluator_specification_digest,
            effective_policy_digest=request.effective_policy_digest,
            observation_kinds=tuple(item.logical_kind for item in artifacts),
            artifacts=artifacts,
        )
        outcome = {
            "succeeded": CriterionOutcome.SATISFIED,
            "failed": CriterionOutcome.UNSATISFIED,
            "cancelled": CriterionOutcome.INDETERMINATE,
            "indeterminate": CriterionOutcome.INDETERMINATE,
        }[execution.status]
        criteria = tuple(
            CriterionResult(
                criterion_id=criterion_id,
                outcome=outcome,
                explanation=(
                    "declared Harness command succeeded"
                    if outcome is CriterionOutcome.SATISFIED
                    else f"declared Harness command was {execution.status}"
                ),
                observation_artifact_digests=artifact_digests,
            )
            for criterion_id in specification.criterion_ids
        )
        findings: tuple[EvaluationFinding, ...] = ()
        if execution.failure is not None:
            findings = (
                EvaluationFinding(
                    finding_id=services.new_id("process-finding"),
                    code=execution.failure.code.value,
                    severity=(
                        FindingSeverity.HIGH
                        if execution.status == "failed"
                        else FindingSeverity.MEDIUM
                    ),
                    message=execution.failure.message,
                    criterion_ids=specification.criterion_ids,
                    observation_artifact_digests=artifact_digests,
                ),
            )
        return EvaluationResult(
            id=services.new_id("evaluation-result"),
            run_id=request.run_id,
            created_at=services.created_at(),
            request_digest=request.content_digest or "",
            candidate_digest=request.candidate_digest,
            generation=request.generation,
            evaluator_specification_digest=request.evaluator_specification_digest,
            effective_policy_digest=request.effective_policy_digest,
            provider_descriptor_digest=self.descriptor_digest,
            behavior=self.descriptor.behavior,
            expected_criterion_ids=specification.criterion_ids,
            observation_manifest=manifest,
            execution_result_digest=execution.content_digest,
            findings=findings,
            criterion_results=criteria,
        )

    def _validate_specification(self, specification: EvaluatorSpecification) -> None:
        if specification.provider_id != self.descriptor.provider_id:
            raise ValueError("evaluator specification names another provider")
        if specification.provider_schema_version != self.descriptor.provider_schema_version:
            raise ValueError("evaluator provider schema version is unsupported")
        if specification.provider_descriptor_digest != self.descriptor_digest:
            raise ValueError("evaluator provider descriptor is stale")
        if specification.behavior is not self.descriptor.behavior:
            raise ValueError("evaluator behavior differs from its provider")
        if set(specification.required_capabilities) != set(self.descriptor.required_capabilities):
            raise ValueError("evaluator specification capabilities differ from its provider")
        if not set(specification.requested_observation_kinds) <= set(
            self.descriptor.supported_observation_kinds
        ):
            raise ValueError("evaluator specification requests unsupported observations")


class StaticEvaluatorRegistry:
    """Immutable registry with no imports, entry points, or third-party discovery."""

    def __init__(self, providers: tuple[EvaluatorProvider, ...]) -> None:
        values: dict[Identifier, EvaluatorProvider] = {}
        for provider in providers:
            provider_id = provider.descriptor.provider_id
            if provider_id in values:
                raise ValueError(f"duplicate evaluator provider ID: {provider_id}")
            if provider_id not in AVAILABLE_FIRST_PARTY_EVALUATOR_IDS:
                raise ValueError(f"evaluator provider is not developer-managed: {provider_id}")
            values[provider_id] = provider
        self._providers: Mapping[Identifier, EvaluatorProvider] = MappingProxyType(values)

    @property
    def available_ids(self) -> tuple[Identifier, ...]:
        return tuple(sorted(self._providers))

    @property
    def reserved_ids(self) -> tuple[Identifier, ...]:
        return tuple(sorted(RESERVED_EVALUATOR_IDS))

    def resolve(self, provider_id: Identifier) -> EvaluatorProvider:
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise KeyError(f"evaluator provider is unavailable: {provider_id}") from error


DEFAULT_EVALUATOR_REGISTRY = StaticEvaluatorRegistry(
    (ProcessEvaluator(), BrowserPlaywrightEvaluator())
)
