from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.domain.evaluation import (
    CandidateRevision,
    CriterionOutcome,
    CriterionResult,
    EvaluationBudget,
    EvaluationDecision,
    EvaluationEvidenceLedger,
    EvaluationFinding,
    EvaluationFreshness,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorBehavior,
    EvaluatorLimits,
    EvaluatorSpecification,
    FindingSeverity,
    FreshnessMismatch,
    decide_evaluation,
    evaluate_freshness,
    replay_evaluation_decision,
)
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from ai_employee.evaluators import (
    DEFAULT_EVALUATOR_REGISTRY,
    HarnessProcessEvaluationServices,
    ProcessEvaluator,
)
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64
ONE = "1" * 64


class NeverCancelled:
    def cancelled(self) -> bool:
        return False


class SuccessfulExecutor:
    def __init__(
        self,
        stdout: ArtifactDescriptor,
        *,
        request_digest: str | None = None,
    ) -> None:
        self.stdout = stdout
        self.request_digest = request_digest
        self.calls = 0

    def execute(
        self,
        request: ProcessRequest,
        _decision: PolicyDecision,
        _cancellation: object,
    ) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            id="execution-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=self.request_digest or request.content_digest or "",
            status="succeeded",
            exit_code=0,
            duration_seconds=0.01,
            stdout_artifact_digest=self.stdout.artifact_digest,
        )


def candidate(*, generation: int = 3, patch_digest: str = ZERO) -> CandidateRevision:
    return CandidateRevision(
        id="candidate-1",
        run_id="run-1",
        created_at=NOW,
        generation=generation,
        base_commit="a" * 40,
        candidate_patch_digest=patch_digest,
    )


def specification(provider: ProcessEvaluator | None = None) -> EvaluatorSpecification:
    provider = provider or ProcessEvaluator()
    return EvaluatorSpecification(
        id="spec-1",
        run_id="run-1",
        created_at=NOW,
        provider_id=provider.descriptor.provider_id,
        provider_schema_version=provider.descriptor.provider_schema_version,
        provider_descriptor_digest=provider.descriptor_digest,
        behavior=EvaluatorBehavior.DETERMINISTIC,
        required_capabilities=("process",),
        requested_observation_kinds=("process_stdout",),
        command_ref="test",
        criterion_ids=("tests-pass",),
    )


def evaluation_request(value: CandidateRevision, spec: EvaluatorSpecification) -> EvaluationRequest:
    return EvaluationRequest(
        id="evaluation-request-1",
        run_id=value.run_id,
        created_at=NOW,
        candidate_digest=value.content_digest or "",
        generation=value.generation,
        evaluator_specification_digest=spec.content_digest or "",
        effective_policy_digest=ZERO,
        remaining_budget=EvaluationBudget(
            remaining_processes=1,
            remaining_artifact_bytes=1024,
        ),
    )


def evaluation_services(
    request: ProcessRequest,
    *,
    execution_request_digest: str | None = None,
    artifact_run_id: str | None = None,
    artifact_request_digest: str | None = None,
    artifact_execution_id: str = "execution-1",
    artifact_size: int = 2,
) -> HarnessProcessEvaluationServices:
    def allow(value: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="policy-1",
            run_id=value.run_id,
            created_at=NOW,
            request_digest=value.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="allowed",
        )

    counts: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        counts[prefix] = counts.get(prefix, 0) + 1
        return f"{prefix}-{counts[prefix]}"

    stdout = ArtifactDescriptor(
        id="stdout-1",
        run_id=artifact_run_id or request.run_id,
        created_at=NOW,
        artifact_digest=ONE,
        media_type="application/octet-stream",
        size_bytes=artifact_size,
        logical_kind="process_stdout",
        producer_action_id=request.id,
        source={
            "bounded": True,
            "request_digest": artifact_request_digest or request.content_digest,
            "execution_id": artifact_execution_id,
        },
        store_locator=f"sha256/{ONE[:2]}/{ONE}",
    )

    return HarnessProcessEvaluationServices(
        {"test": request},
        SuccessfulExecutor(  # type: ignore[arg-type]
            stdout, request_digest=execution_request_digest
        ),
        allow,
        NeverCancelled(),
        artifact_resolver=lambda digest, _kind, _execution_id: {ONE: stdout}[digest],
        id_factory=new_id,
        clock=lambda: NOW,
    )


def evaluated() -> tuple[
    CandidateRevision,
    EvaluatorSpecification,
    EvaluationRequest,
    EvaluationResult,
]:
    current = candidate()
    spec = specification()
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared offline verification",
    )
    result = ProcessEvaluator().evaluate(request, spec, evaluation_services(command))
    return current, spec, request, result


def test_candidate_and_process_evaluation_are_exact_and_frozen() -> None:
    current, spec, request, result = evaluated()
    assert result.request_digest == request.content_digest
    assert result.candidate_digest == current.content_digest
    assert result.evaluator_specification_digest == spec.content_digest
    assert result.criterion_results[0].outcome is CriterionOutcome.SATISFIED
    assert result.observation_manifest.artifacts[0].artifact_digest == ONE
    with pytest.raises(ValidationError):
        current.generation = 4  # type: ignore[misc]
    with pytest.raises(ValidationError, match="exactly one patch or tree"):
        CandidateRevision(
            id="candidate-empty",
            run_id="run-1",
            created_at=NOW,
            generation=0,
            base_commit="a" * 40,
        )
    with pytest.raises(ValidationError, match="exactly one patch or tree"):
        CandidateRevision(
            id="candidate-ambiguous",
            run_id="run-1",
            created_at=NOW,
            generation=0,
            base_commit="a" * 40,
            candidate_patch_digest=ZERO,
            candidate_tree_digest=ONE,
        )


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        ({"candidate_digest": ONE}, FreshnessMismatch.CANDIDATE),
        ({"generation": 4}, FreshnessMismatch.GENERATION),
        ({"evaluator_specification_digest": ONE}, FreshnessMismatch.EVALUATOR_SPECIFICATION),
        ({"effective_policy_digest": ONE}, FreshnessMismatch.EFFECTIVE_POLICY),
    ],
)
def test_freshness_rejects_stale_fences(
    replacement: dict[str, object], reason: FreshnessMismatch
) -> None:
    current, spec, request, result = evaluated()
    payload = request.model_dump(exclude={"content_digest", "digest_metadata"})
    payload.update(replacement)
    stale_request = EvaluationRequest.model_validate(payload, strict=True)
    freshness = evaluate_freshness(current, spec, ZERO, stale_request, result)
    assert not freshness.fresh
    assert reason in freshness.mismatches
    assert (
        decide_evaluation(
            result.criterion_results,
            result.findings,
            freshness,
            stale_request.remaining_budget,
            result.behavior,
            expected_criterion_ids=spec.criterion_ids,
        )
        is EvaluationDecision.FAIL
    )


def test_registry_and_undeclared_process_fail_closed() -> None:
    assert DEFAULT_EVALUATOR_REGISTRY.available_ids == ("process.harness",)
    assert "browser.playwright" in DEFAULT_EVALUATOR_REGISTRY.reserved_ids
    with pytest.raises(KeyError, match="unavailable"):
        DEFAULT_EVALUATOR_REGISTRY.resolve("browser.playwright")
    current = candidate()
    payload = specification().model_dump(exclude={"content_digest", "digest_metadata"})
    payload["command_ref"] = "undeclared"
    spec = EvaluatorSpecification.model_validate(payload, strict=True)
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id="run-1",
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    with pytest.raises(ValueError, match="unknown declared"):
        ProcessEvaluator().evaluate(request, spec, evaluation_services(command))


def test_decision_is_deterministic_and_never_trusts_uncertainty() -> None:
    fresh = EvaluationFreshness(fresh=True)
    budget = EvaluationBudget(remaining_processes=1, remaining_artifact_bytes=1)
    satisfied = (
        CriterionResult(
            criterion_id="visual",
            outcome=CriterionOutcome.SATISFIED,
            explanation="observation satisfied the criterion",
        ),
    )
    indeterminate = (
        CriterionResult(
            criterion_id="visual",
            outcome=CriterionOutcome.INDETERMINATE,
            explanation="observation unavailable",
        ),
    )
    unsatisfied = (
        CriterionResult(
            criterion_id="visual",
            outcome=CriterionOutcome.UNSATISFIED,
            explanation="criterion failed",
        ),
    )
    assert (
        decide_evaluation(
            satisfied,
            (),
            fresh,
            budget,
            EvaluatorBehavior.DETERMINISTIC,
            expected_criterion_ids=("visual",),
        )
        is EvaluationDecision.PASS
    )
    assert (
        decide_evaluation(
            indeterminate,
            (),
            fresh,
            budget,
            EvaluatorBehavior.DETERMINISTIC,
            expected_criterion_ids=("visual",),
        )
        is EvaluationDecision.ESCALATE
    )
    assert (
        decide_evaluation(
            satisfied,
            (),
            fresh,
            budget,
            EvaluatorBehavior.PROBABILISTIC,
            expected_criterion_ids=("visual",),
        )
        is EvaluationDecision.ESCALATE
    )
    assert (
        decide_evaluation(
            unsatisfied,
            (),
            fresh,
            budget,
            EvaluatorBehavior.DETERMINISTIC,
            expected_criterion_ids=("visual",),
        )
        is EvaluationDecision.REPAIR
    )
    critical = (
        EvaluationFinding(
            finding_id="finding-1",
            code="unsafe-output",
            severity=FindingSeverity.CRITICAL,
            message="unsafe output was observed",
        ),
    )
    assert (
        decide_evaluation(
            satisfied,
            critical,
            fresh,
            budget,
            EvaluatorBehavior.DETERMINISTIC,
            expected_criterion_ids=("visual",),
        )
        is EvaluationDecision.FAIL
    )
    assert (
        decide_evaluation(
            satisfied,
            (),
            fresh,
            budget,
            EvaluatorBehavior.DETERMINISTIC,
            expected_criterion_ids=("visual", "accessibility"),
        )
        is EvaluationDecision.FAIL
    )


def test_evaluation_ledger_persists_and_replays_without_execution(tmp_path: Path) -> None:
    current, spec, request, result = evaluated()
    freshness = evaluate_freshness(current, spec, ZERO, request, result)
    decision = decide_evaluation(
        result.criterion_results,
        result.findings,
        freshness,
        request.remaining_budget,
        result.behavior,
        expected_criterion_ids=spec.criterion_ids,
    )
    ledger = EvaluationEvidenceLedger(
        id="evaluation-ledger-1",
        run_id="run-1",
        created_at=NOW,
        candidate_digest=current.content_digest or "",
        generation=current.generation,
        evaluator_specification_digest=spec.content_digest or "",
        effective_policy_digest=ZERO,
        evaluation_result_digests=(result.content_digest or "",),
        observation_manifest_digests=(result.observation_manifest.content_digest or "",),
        expected_criterion_ids=spec.criterion_ids,
        criterion_results=result.criterion_results,
        findings=result.findings,
        freshness=freshness,
        remaining_budget=request.remaining_budget,
        behavior=result.behavior,
        decision=decision,
    )
    with SQLiteStore(tmp_path / "fleet.db") as store:
        store.put("evaluation_evidence_ledger_v2", ledger, run_id="run-1")
        restored = store.get(
            "evaluation_evidence_ledger_v2",
            ledger.id,
            EvaluationEvidenceLedger,
        )
    assert replay_evaluation_decision(restored) is EvaluationDecision.PASS


def test_cross_run_specification_and_nested_evidence_fail_closed() -> None:
    current, spec, _request, result = evaluated()
    spec_payload = spec.model_dump(exclude={"content_digest", "digest_metadata"})
    spec_payload["run_id"] = "run-2"
    foreign_spec = EvaluatorSpecification.model_validate(spec_payload, strict=True)
    foreign_request = evaluation_request(current, foreign_spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    with pytest.raises(ValueError, match="different runs"):
        ProcessEvaluator().evaluate(
            foreign_request, foreign_spec, evaluation_services(command)
        )

    result_payload = result.model_dump(exclude={"content_digest", "digest_metadata"})
    manifest_payload = result_payload["observation_manifest"]
    assert isinstance(manifest_payload, dict)
    manifest_payload["run_id"] = "run-2"
    with pytest.raises(ValidationError, match="artifact belongs to another run"):
        EvaluationResult.model_validate(result_payload, strict=True)


def test_wrong_process_request_digest_is_rejected() -> None:
    current = candidate()
    spec = specification()
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    services = evaluation_services(command, execution_request_digest=ONE)
    with pytest.raises(ValueError, match="another declared request"):
        ProcessEvaluator().evaluate(request, spec, services)


@pytest.mark.parametrize(
    ("service_kwargs", "message"),
    [
        ({"artifact_request_digest": ONE}, "another process request"),
        ({"artifact_execution_id": "execution-2"}, "another execution"),
        ({"artifact_run_id": "run-2"}, "another run"),
    ],
)
def test_artifact_provenance_and_identical_digest_ambiguity_are_rejected(
    service_kwargs: dict[str, object], message: str
) -> None:
    current = candidate()
    spec = specification()
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    services = evaluation_services(command, **service_kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=message):
        ProcessEvaluator().evaluate(request, spec, services)


def test_exhausted_budget_and_artifact_budget_fail_closed() -> None:
    current = candidate()
    spec = specification()
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    payload = request.model_dump(exclude={"content_digest", "digest_metadata"})
    payload["remaining_budget"] = {
        "remaining_processes": 0,
        "remaining_artifact_bytes": 1024,
    }
    exhausted = EvaluationRequest.model_validate(payload, strict=True)
    services = evaluation_services(command)
    with pytest.raises(ValueError, match="process budget is exhausted"):
        ProcessEvaluator().evaluate(exhausted, spec, services)
    assert services.executions == ()

    payload["remaining_budget"] = {
        "remaining_processes": 1,
        "remaining_artifact_bytes": 1,
    }
    too_small = EvaluationRequest.model_validate(payload, strict=True)
    with pytest.raises(ValueError, match="artifact byte budget"):
        ProcessEvaluator().evaluate(too_small, spec, evaluation_services(command))


def test_exhausted_evaluator_limit_prevents_execution() -> None:
    class DisabledProcessEvaluator(ProcessEvaluator):
        descriptor = ProcessEvaluator.descriptor.model_copy(
            update={
                "limits": EvaluatorLimits(
                    maximum_processes=0,
                    maximum_artifact_bytes=2_000_000,
                    maximum_observations=2,
                )
            }
        )

    provider = DisabledProcessEvaluator()
    current = candidate()
    spec = specification(provider)
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    services = evaluation_services(command)
    with pytest.raises(ValueError, match="process limit is exhausted"):
        provider.evaluate(request, spec, services)
    assert services.executions == ()


def test_exhausted_evaluator_artifact_limit_prevents_execution() -> None:
    class NoArtifactEvaluator(ProcessEvaluator):
        descriptor = ProcessEvaluator.descriptor.model_copy(
            update={
                "limits": EvaluatorLimits(
                    maximum_processes=1,
                    maximum_artifact_bytes=0,
                    maximum_observations=2,
                )
            }
        )

    provider = NoArtifactEvaluator()
    current = candidate()
    spec = specification(provider)
    request = evaluation_request(current, spec)
    command = ProcessRequest(
        id="command-1",
        run_id=current.run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="declared verification",
    )
    services = evaluation_services(command)
    with pytest.raises(ValueError, match="artifact limit is exhausted"):
        provider.evaluate(request, spec, services)
    assert services.executions == ()
