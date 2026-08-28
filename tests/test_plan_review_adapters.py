from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ai_employee.demo import demo_goal, demo_graph
from ai_employee.domain import ExecutionStrategy, RoutingMode
from ai_employee.domain.v2 import (
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from ai_employee.plan_review import (
    CliPlanReviewer,
    PlanReviewAction,
    PlanReviewPayload,
    decide_plan_review_action,
    plan_review_schema_json,
)
from ai_employee.serialization import canonical_digest, canonical_json
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 8, 28, tzinfo=UTC)
POLICY_DIGEST = "1" * 64
HARNESS_DIGEST = "2" * 64


def _strategy(backend: str = "ollama_cli") -> ExecutionStrategy:
    return ExecutionStrategy(
        id=f"reviewer-{backend}",
        routing_mode=RoutingMode.ADAPTIVE,
        backend=backend,
        model="reviewer-model",
        effort="low",
        capabilities=("process",),
    )


def _proposal() -> tuple[object, ProposedGraph]:
    goal, _requirement = demo_goal()
    proposal = ProposedGraph(
        id="adapter-proposal",
        run_id="adapter-run",
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=demo_graph(),
        planner_strategy=_strategy(),
        effective_policy_digest=POLICY_DIGEST,
        harness_digest=HARNESS_DIGEST,
    )
    return goal, proposal


class _ScriptedExecutor:
    def __init__(self, *, output_digest: str = "9" * 64) -> None:
        self.output_digest = output_digest
        self.requests: list[ProcessRequest] = []

    def execute(
        self,
        request: ProcessRequest,
        decision: PolicyDecision,
        _cancellation: object,
    ) -> ExecutionResult:
        assert decision.request_digest == request.content_digest
        self.requests.append(request)
        return ExecutionResult(
            id="review-execution",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "0" * 64,
            status="succeeded",
            exit_code=0,
            duration_seconds=0.01,
            stdout_artifact_digest=self.output_digest,
        )


def _decision(request: ProcessRequest, outcome: DecisionOutcome) -> PolicyDecision:
    return PolicyDecision(
        id="review-policy",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "0" * 64,
        effective_policy_digest=POLICY_DIGEST,
        outcome=outcome,
        reason_code="scripted_review_policy",
    )


def test_reviewer_process_is_fresh_bounded_and_receives_only_declared_context() -> None:
    goal, proposal = _proposal()
    output = canonical_json(PlanReviewPayload(findings=())).encode()
    prompts: list[bytes] = []
    executor = _ScriptedExecutor()
    reviewer = CliPlanReviewer(
        executor,
        lambda digest: output if digest == executor.output_digest else b"",
        lambda request: _decision(request, DecisionOutcome.ALLOW),
        run_id="adapter-run",
        strategy=_strategy(),
        executable="ollama",
        cwd=".",
        prompt_writer=lambda value: prompts.append(value) or "8" * 64,
    )

    review = reviewer.review(
        goal,  # type: ignore[arg-type]
        proposal,
        review_round=0,
        available_capabilities=("process", "process"),
        max_nodes=4,
        max_wall_seconds=30.0,
    )

    assert decide_plan_review_action(review) is PlanReviewAction.ACCEPT
    assert len(prompts) == len(executor.requests) == 1
    prompt = json.loads(prompts[0])
    assert prompt["protocol"] == "fleet-plan-review/2"
    assert prompt["goal"] == goal.model_dump(mode="json")  # type: ignore[union-attr]
    assert prompt["proposed_graph"] == proposal.graph.model_dump(mode="json")
    assert prompt["available_capabilities"] == ["process"]
    assert prompt["response_schema"] == json.loads(plan_review_schema_json())
    assert {"repository", "files", "tools", "worker_results"}.isdisjoint(prompt)
    request = executor.requests[0]
    assert request.purpose == "obtain a strict non-authoritative PlanReviewPayload"
    assert request.stdout_bytes == request.stderr_bytes == 100_000


def test_reviewer_argv_keeps_codex_and_claude_ephemeral_and_tool_restricted() -> None:
    executor = _ScriptedExecutor()

    def allow(request: ProcessRequest) -> PolicyDecision:
        return _decision(request, DecisionOutcome.ALLOW)

    denied_requests: list[ProcessRequest] = []

    def deny(request: ProcessRequest) -> PolicyDecision:
        denied_requests.append(request)
        return _decision(request, DecisionOutcome.DENY)

    codex = CliPlanReviewer(
        executor,
        lambda _digest: b"",
        allow,
        run_id="adapter-run",
        strategy=_strategy("codex_cli"),
        executable="codex",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
        output_schema_path="plan-review.json",
    )
    claude = CliPlanReviewer(
        executor,
        lambda _digest: b"",
        deny,
        run_id="adapter-run",
        strategy=_strategy("claude_code_cli"),
        executable="claude",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    codex_argv = codex._argv()
    claude_argv = claude._argv()
    assert "--ephemeral" in codex_argv
    assert codex_argv[codex_argv.index("--sandbox") + 1] == "read-only"
    assert codex_argv[codex_argv.index("--ask-for-approval") + 1] == "never"
    assert "--no-session-persistence" in claude_argv
    assert "--tools=" in claude_argv
    assert "" not in claude_argv

    goal, proposal = _proposal()
    with pytest.raises(ValueError, match="plan-review policy did not allow execution"):
        claude.review(
            goal,  # type: ignore[arg-type]
            proposal,
            review_round=0,
            available_capabilities=("process",),
            max_nodes=4,
            max_wall_seconds=30.0,
        )

    assert len(denied_requests) == 1
    assert denied_requests[0].inherit_environment == ("HOME", "USER")
    assert all(denied_requests[0].argv)


@pytest.mark.parametrize("mode", ["denied", "malformed"])
def test_reviewer_policy_denial_and_malformed_output_fail_without_fallback(mode: str) -> None:
    goal, proposal = _proposal()
    executor = _ScriptedExecutor()
    output = b"{not-json"
    outcome = DecisionOutcome.DENY if mode == "denied" else DecisionOutcome.ALLOW
    reviewer = CliPlanReviewer(
        executor,
        lambda digest: output if digest == executor.output_digest else b"",
        lambda request: _decision(request, outcome),
        run_id="adapter-run",
        strategy=_strategy(),
        executable="ollama",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    with pytest.raises(ValueError):
        reviewer.review(
            goal,  # type: ignore[arg-type]
            proposal,
            review_round=0,
            available_capabilities=("process",),
            max_nodes=4,
            max_wall_seconds=30.0,
        )
    assert len(executor.requests) == (0 if mode == "denied" else 1)
