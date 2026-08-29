"""Developer-managed browser evaluator using only mediated browser services."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from .domain.base import Digest, Identifier
from .domain.browser import (
    BROWSER_EVALUATOR_ID,
    BrowserEvaluationServices,
    BrowserObservation,
    BrowserScenario,
    browser_origin,
)
from .domain.evaluation import (
    CriterionOutcome,
    CriterionResult,
    EvaluationFinding,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorBehavior,
    EvaluatorDescriptor,
    EvaluatorLimits,
    EvaluatorServices,
    EvaluatorSpecification,
    FindingSeverity,
    ObservationManifest,
)
from .domain.v2 import ArtifactDescriptor
from .serialization import versioned_digest


class BrowserPlaywrightEvaluator:
    """Evaluate a confined browser scenario through a mediated service only."""

    descriptor = EvaluatorDescriptor(
        provider_id=BROWSER_EVALUATOR_ID,
        provider_schema_version="v1",
        behavior=EvaluatorBehavior.DETERMINISTIC,
        required_capabilities=("browser",),
        supported_observation_kinds=("browser_screenshot", "browser_accessibility"),
        limits=EvaluatorLimits(
            maximum_processes=0,
            maximum_artifact_bytes=8_000_000,
            maximum_observations=2,
            maximum_actions=50,
            maximum_duration_seconds=60.0,
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
        scenario = specification.browser_scenario
        assert scenario is not None
        if request.run_id != specification.run_id:
            raise ValueError("evaluation request and specification belong to different runs")
        if request.evaluator_specification_digest != specification.content_digest:
            raise ValueError("evaluation request is stale for its evaluator specification")
        self._validate_preflight_budget(request, scenario)
        browser = cast(BrowserEvaluationServices, services)
        if browser.cancelled():
            raise ValueError("browser evaluation was cancelled before launch")

        raw_session_id = browser.open_browser(scenario, request)
        try:
            if not isinstance(raw_session_id, str) or not raw_session_id:
                raise TypeError("browser service returned an invalid session identifier")
            session_id = raw_session_id
            raw_observation = browser.observe_browser(session_id, scenario, request)
            if not isinstance(raw_observation, BrowserObservation):
                raise TypeError("browser service returned an invalid observation")
            observation = raw_observation
            if browser.cancelled() and observation.status != "cancelled":
                raise ValueError("browser service ignored evaluation cancellation")
            artifacts = self._validate_observation(
                observation, session_id, scenario, request, specification
            )
        finally:
            browser.teardown_browser(raw_session_id)

        artifact_digests = tuple(item.artifact_digest for item in artifacts)
        manifest = ObservationManifest(
            id=browser.new_id("observation-manifest"),
            run_id=request.run_id,
            created_at=browser.created_at(),
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
            "timed_out": CriterionOutcome.INDETERMINATE,
        }[observation.status]
        criteria = tuple(
            CriterionResult(
                criterion_id=criterion_id,
                outcome=outcome,
                explanation=(
                    "confined browser scenario completed"
                    if observation.status == "succeeded"
                    else f"confined browser scenario was {observation.status}"
                ),
                observation_artifact_digests=artifact_digests,
            )
            for criterion_id in specification.criterion_ids
        )
        findings: tuple[EvaluationFinding, ...] = ()
        if observation.failure is not None:
            findings = (
                EvaluationFinding(
                    finding_id=browser.new_id("browser-finding"),
                    code=observation.failure.code.value,
                    severity=(
                        FindingSeverity.HIGH
                        if observation.status == "failed"
                        else FindingSeverity.MEDIUM
                    ),
                    message=observation.failure.message,
                    criterion_ids=specification.criterion_ids,
                    observation_artifact_digests=artifact_digests,
                ),
            )
        return EvaluationResult(
            id=browser.new_id("evaluation-result"),
            run_id=request.run_id,
            created_at=browser.created_at(),
            request_digest=request.content_digest or "",
            candidate_digest=request.candidate_digest,
            generation=request.generation,
            evaluator_specification_digest=request.evaluator_specification_digest,
            effective_policy_digest=request.effective_policy_digest,
            provider_descriptor_digest=self.descriptor_digest,
            behavior=self.descriptor.behavior,
            expected_criterion_ids=specification.criterion_ids,
            observation_manifest=manifest,
            execution_result_digest=observation.content_digest,
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
        scenario = specification.browser_scenario
        if scenario is None:
            raise ValueError("browser evaluator requires a typed scenario")
        requested = tuple(item.logical_kind for item in scenario.captures)
        if specification.requested_observation_kinds != requested:
            raise ValueError("browser observations must exactly match the scenario captures")
        if not set(requested) <= set(self.descriptor.supported_observation_kinds):
            raise ValueError("evaluator specification requests unsupported observations")

    def _validate_preflight_budget(
        self, request: EvaluationRequest, scenario: BrowserScenario
    ) -> None:
        limits = self.descriptor.limits
        action_count = len(scenario.actions)
        if action_count > request.remaining_budget.remaining_actions:
            raise ValueError("evaluation action budget would be exceeded")
        if action_count > limits.maximum_actions:
            raise ValueError("evaluator action limit would be exceeded")
        if scenario.timeout_seconds > request.remaining_budget.remaining_duration_seconds:
            raise ValueError("evaluation duration budget would be exceeded")
        if scenario.timeout_seconds > limits.maximum_duration_seconds:
            raise ValueError("evaluator duration limit would be exceeded")
        if len(scenario.captures) > limits.maximum_observations:
            raise ValueError("evaluator observation limit would be exceeded")
        if scenario.captures and request.remaining_budget.remaining_artifact_bytes < 1:
            raise ValueError("evaluation artifact budget is exhausted")
        if scenario.captures and limits.maximum_artifact_bytes < 1:
            raise ValueError("evaluator artifact limit is exhausted")

    def _validate_observation(
        self,
        observation: BrowserObservation,
        session_id: Identifier,
        scenario: BrowserScenario,
        request: EvaluationRequest,
        specification: EvaluatorSpecification,
    ) -> tuple[ArtifactDescriptor, ...]:
        if observation.run_id != request.run_id:
            raise ValueError("browser observation belongs to another run")
        if observation.request_digest != request.content_digest:
            raise ValueError("browser observation belongs to another evaluation request")
        scenario_digest = versioned_digest(scenario)
        if observation.scenario_digest != scenario_digest:
            raise ValueError("browser observation belongs to another scenario")
        if observation.session_id != session_id:
            raise ValueError("browser observation belongs to another session")
        if observation.actions_completed > len(scenario.actions):
            raise ValueError("browser observation exceeds the scenario action count")
        if observation.actions_completed > request.remaining_budget.remaining_actions:
            raise ValueError("evaluation action budget was exceeded")
        if observation.actions_completed > self.descriptor.limits.maximum_actions:
            raise ValueError("evaluator action limit was exceeded")
        if observation.status == "succeeded" and observation.actions_completed != len(
            scenario.actions
        ):
            raise ValueError("successful browser observation did not complete every action")
        if observation.duration_seconds > scenario.timeout_seconds:
            raise ValueError("browser scenario timeout was exceeded")
        if observation.duration_seconds > request.remaining_budget.remaining_duration_seconds:
            raise ValueError("evaluation duration budget was exceeded")
        if observation.duration_seconds > self.descriptor.limits.maximum_duration_seconds:
            raise ValueError("evaluator duration limit was exceeded")
        if observation.final_url is not None and browser_origin(observation.final_url) != (
            browser_origin(scenario.origin, origin_only=True)
        ):
            raise ValueError("browser observation escaped the exact scenario origin")
        if observation.status == "succeeded" and observation.final_url is None:
            raise ValueError("successful browser observation requires a final URL")

        artifacts = observation.artifacts
        actual_kinds = tuple(item.logical_kind for item in artifacts)
        requested_kinds = specification.requested_observation_kinds
        if observation.status == "succeeded" and actual_kinds != requested_kinds:
            raise ValueError("successful browser observation did not capture exact artifacts")
        if tuple(kind for kind in requested_kinds if kind in actual_kinds) != actual_kinds:
            raise ValueError("browser observation contains undeclared or reordered artifacts")
        expected_media_types = {
            "browser_screenshot": "image/png",
            "browser_accessibility": "application/json",
        }
        for artifact in artifacts:
            if artifact.run_id != request.run_id:
                raise ValueError("browser artifact belongs to another run")
            if artifact.producer_action_id != observation.id:
                raise ValueError("browser artifact belongs to another observation")
            if artifact.media_type != expected_media_types[artifact.logical_kind]:
                raise ValueError("browser artifact has an invalid media type")
            if not isinstance(artifact.source, Mapping):
                raise ValueError("browser artifact has no provenance")
            if artifact.source.get("request_digest") != request.content_digest:
                raise ValueError("browser artifact belongs to another evaluation request")
            if artifact.source.get("scenario_digest") != scenario_digest:
                raise ValueError("browser artifact belongs to another scenario")
            if artifact.source.get("session_id") != session_id:
                raise ValueError("browser artifact belongs to another session")
        artifact_bytes = sum(item.size_bytes for item in artifacts)
        if artifact_bytes > request.remaining_budget.remaining_artifact_bytes:
            raise ValueError("evaluation artifact byte budget was exceeded")
        if artifact_bytes > self.descriptor.limits.maximum_artifact_bytes:
            raise ValueError("evaluator artifact byte limit was exceeded")
        return artifacts
