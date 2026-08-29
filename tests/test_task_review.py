from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ai_employee.domain import ExecutionStrategy, RoutingMode
from ai_employee.domain.base import freeze_json
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    CriterionEvidence,
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.serialization import canonical_json
from ai_employee.task_review import (
    CliTaskResultReviewer,
    TaskReviewPayload,
    TaskReviewRequest,
    bind_task_review_payload,
    task_review_schema_json,
    validate_task_review_result,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)
GRAPH = "1" * 64
HARNESS = "2" * 64
POLICY = "3" * 64
EVIDENCE = "4" * 64
EVALUATOR = "5" * 64


def _strategy(backend: str = "ollama_cli") -> ExecutionStrategy:
    return ExecutionStrategy(
        id=f"task-reviewer-{backend}",
        routing_mode=RoutingMode.ADAPTIVE,
        backend=backend,
        model="reviewer-model",
        effort="low",
        capabilities=("process",),
    )


def _request(
    strategy: ExecutionStrategy | None = None,
    artifact_descriptors: tuple[ArtifactDescriptor, ...] = (),
) -> TaskReviewRequest:
    worker_request = WorkerRequest(
        id="worker-request",
        run_id="node-run",
        created_at=NOW,
        goal="fix the defect",
        completion_criteria=("the defect is fixed",),
        required_capabilities=("process",),
        accepted_plan_digest=GRAPH,
        node_id="fix",
        accepted_graph_revision_digest=GRAPH,
        graph_run_id="review-run",
        generation=0,
        attempt=0,
        harness_digest=HARNESS,
        effective_policy_digest=POLICY,
        remaining_budgets=None,
    )
    worker_result = WorkerResult(
        id="worker-result",
        run_id=worker_request.run_id,
        created_at=NOW,
        request_digest=worker_request.content_digest or "0" * 64,
        status="succeeded",
        duration_seconds=0.01,
        assistant_note="implemented the smallest bounded correction",
    )
    evidence = CriterionEvidence(
        criterion_id="criterion-fix",
        disposition="satisfied",
        evidence_refs=(EVIDENCE,),
    )
    return TaskReviewRequest(
        id="task-review-request",
        run_id="review-run",
        created_at=NOW,
        node_id="fix",
        objective="fix the defect",
        completion_criteria=("the defect is fixed",),
        criterion_ids=("criterion-fix",),
        accepted_graph_revision_digest=GRAPH,
        generation=0,
        attempt=0,
        reviewer_strategy=strategy or _strategy(),
        harness_digest=HARNESS,
        effective_policy_digest=POLICY,
        worker_request_digest=worker_request.content_digest or "0" * 64,
        worker_request=worker_request,
        worker_result_digest=worker_result.content_digest or "0" * 64,
        worker_result=worker_result,
        criterion_evidence=(evidence,),
        deterministic_evidence_digests=(EVIDENCE, EVALUATOR),
        artifact_descriptors=artifact_descriptors,
    )


class _Executor:
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def execute(
        self,
        request: ProcessRequest,
        _decision: PolicyDecision,
        _cancellation: object,
    ) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            id="task-review-execution",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "0" * 64,
            status="succeeded",
            duration_seconds=0.01,
            stdout_artifact_digest="9" * 64,
        )


def _allow(request: ProcessRequest) -> PolicyDecision:
    return PolicyDecision(
        id="task-review-policy",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "0" * 64,
        effective_policy_digest=POLICY,
        outcome=DecisionOutcome.ALLOW,
        reason_code="explicit_task_review",
    )


def test_task_reviewer_receives_exact_bounded_payload_after_opt_in() -> None:
    request = _request()
    output = canonical_json(
        TaskReviewPayload(findings=(), reviewed_criterion_ids=("criterion-fix",), limitations=())
    ).encode()
    prompts: list[bytes] = []
    executor = _Executor()
    reviewer = CliTaskResultReviewer(
        executor,
        lambda digest: output if digest == "9" * 64 else b"",
        _allow,
        run_id="review-run",
        strategy=request.reviewer_strategy,
        executable="ollama",
        cwd=".",
        prompt_writer=lambda value: (prompts.append(value), "8" * 64)[1],
    )

    result = reviewer.review(request)

    assert result.request_digest == request.content_digest
    assert len(prompts) == len(executor.requests) == 1
    prompt = json.loads(prompts[0])
    assert prompt["protocol"] == "fleet-task-result-review/2"
    assert prompt["request"]["worker_request"]["content_digest"] == (request.worker_request_digest)
    assert prompt["request"]["worker_result"]["content_digest"] == (request.worker_result_digest)
    assert prompt["request"]["artifact_descriptors"] == []
    assert prompt["response_schema"] == json.loads(task_review_schema_json())
    assert {"repository", "conversation_history", "artifact_bodies"}.isdisjoint(prompt)


def test_task_reviewer_sanitizes_artifact_descriptor_egress() -> None:
    canary = "SECRET-CANARY-MUST-NOT-EGRESS"
    descriptor = ArtifactDescriptor(
        id="artifact",
        run_id="review-run",
        created_at=NOW,
        artifact_digest="6" * 64,
        media_type="text/x-diff",
        size_bytes=123,
        logical_kind="workspace_patch",
        producer_action_id="worker-result",
        source=freeze_json({"secret": canary}),
        redaction_state="none",
        store_locator=f"private/{canary}",
    )
    request = _request(artifact_descriptors=(descriptor,))
    output = canonical_json(
        TaskReviewPayload(findings=(), reviewed_criterion_ids=("criterion-fix",), limitations=())
    ).encode()
    prompts: list[bytes] = []
    reviewer = CliTaskResultReviewer(
        _Executor(),
        lambda digest: output if digest == "9" * 64 else b"",
        _allow,
        run_id="review-run",
        strategy=request.reviewer_strategy,
        executable="ollama",
        cwd=".",
        prompt_writer=lambda value: (prompts.append(value), "8" * 64)[1],
    )

    reviewer.review(request)

    prompt_text = prompts[0].decode()
    exported = json.loads(prompt_text)["request"]["artifact_descriptors"][0]
    assert set(exported) == {
        "artifact_digest",
        "media_type",
        "size_bytes",
        "logical_kind",
        "producer_action_id",
        "redaction_state",
    }
    assert canary not in prompt_text
    assert "source" not in exported
    assert "store_locator" not in exported


def test_task_reviewer_argv_disables_tools_and_sessions() -> None:
    executor = _Executor()
    codex = CliTaskResultReviewer(
        executor,
        lambda _digest: b"",
        _allow,
        run_id="review-run",
        strategy=_strategy("codex_cli"),
        executable="codex",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
        output_schema_path="task-review.json",
    )
    claude = CliTaskResultReviewer(
        executor,
        lambda _digest: b"",
        _allow,
        run_id="review-run",
        strategy=_strategy("claude_code_cli"),
        executable="claude",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    assert "--ephemeral" in codex._argv()
    assert codex._argv()[codex._argv().index("--sandbox") + 1] == "read-only"
    assert codex._argv()[codex._argv().index("--ask-for-approval") + 1] == "never"
    assert "--ignore-rules" in codex._argv()
    disabled = tuple(
        value
        for index, value in enumerate(codex._argv())
        if index > 0 and codex._argv()[index - 1] == "--disable"
    )
    assert disabled == ("shell_tool", "unified_exec")
    assert "--tools=" in claude._argv()
    assert "--no-session-persistence" in claude._argv()


def test_task_reviewer_rejects_foreign_request() -> None:
    reviewer = CliTaskResultReviewer(
        _Executor(),
        lambda _digest: b"",
        _allow,
        run_id="other-run",
        strategy=_strategy(),
        executable="ollama",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )
    with pytest.raises(ValueError, match="another reviewer or run"):
        reviewer.review(_request())


def test_task_review_result_rejects_foreign_run_binding() -> None:
    request = _request()
    result = bind_task_review_payload(
        TaskReviewPayload(
            findings=(), reviewed_criterion_ids=request.criterion_ids, limitations=()
        ),
        request=request,
        record_id="result",
        run_id=request.run_id,
        created_at=NOW,
    ).model_copy(update={"run_id": "foreign-run"})

    with pytest.raises(ValueError, match="stale or foreign bindings"):
        validate_task_review_result(request, result)
