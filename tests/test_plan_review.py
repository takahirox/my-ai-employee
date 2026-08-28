from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from ai_employee.demo import demo_goal, demo_graph
from ai_employee.domain import ExecutionStrategy, RoutingMode
from ai_employee.plan_review import (
    PlanReviewAction,
    PlanReviewFinding,
    PlanReviewFindingType,
    PlanReviewImpact,
    PlanReviewPayload,
    PlanReviewValidationError,
    bind_plan_review,
    decide_plan_review_action,
    validate_plan_review,
)
from ai_employee.task_planning import ProposedGraph

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


class PlanReviewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.created_at = datetime(2026, 8, 28, tzinfo=UTC)
        self.goal, _ = demo_goal()
        self.graph = demo_graph()
        self.strategy = ExecutionStrategy(
            id="reviewer-strategy",
            routing_mode=RoutingMode.FIXED,
            backend="codex_cli",
            model="reviewer",
        )
        self.proposed_graph = ProposedGraph(
            id="proposed-graph",
            run_id="review-run",
            created_at=self.created_at,
            goal_id=self.goal.id,
            goal_digest=self._goal_digest(),
            graph=self.graph,
            planner_strategy=self.strategy.model_copy(update={"id": "planner-strategy"}),
            effective_policy_digest=_DIGEST_A,
            harness_digest=_DIGEST_B,
        )

    def _goal_digest(self) -> str:
        from ai_employee.serialization import canonical_digest

        return canonical_digest(self.goal)

    def _finding(
        self,
        *,
        finding_id: str = "finding-a",
        blocking: bool = True,
        node_id: str | None = None,
    ) -> PlanReviewFinding:
        return PlanReviewFinding(
            id=finding_id,
            finding_type=PlanReviewFindingType.UNNECESSARY_TASK,
            impact=PlanReviewImpact.BLOCKING if blocking else PlanReviewImpact.ADVISORY,
            affected_node_ids=(node_id or self.graph.nodes[0].id,),
            goal_relation="No accepted-goal requirement or current safety need supports it.",
            smallest_correction="Remove the task and its incident dependencies.",
        )

    def _review(
        self,
        *,
        review_round: int,
        findings: tuple[PlanReviewFinding, ...],
    ):
        return bind_plan_review(
            PlanReviewPayload(findings=findings),
            record_id=f"review-{review_round}",
            run_id="review-run",
            created_at=self.created_at,
            review_round=review_round,  # type: ignore[arg-type]
            goal=self.goal,
            proposed_graph=self.proposed_graph,
            reviewer_strategy=self.strategy,
        )

    def test_contracts_are_strict_frozen_and_canonically_ordered(self) -> None:
        finding = self._finding()
        payload = PlanReviewPayload(findings=(finding,))
        with self.assertRaises(ValidationError):
            payload.findings = ()
        with self.assertRaises(ValidationError):
            PlanReviewPayload.model_validate(
                {
                    "schema_version": "2",
                    "findings": (finding,),
                    "unexpected": True,
                },
                strict=True,
            )
        with self.assertRaises(ValidationError):
            PlanReviewPayload(
                findings=(
                    self._finding(finding_id="finding-b"),
                    self._finding(finding_id="finding-a"),
                )
            )
        with self.assertRaises(ValidationError):
            PlanReviewFinding.model_validate(
                {
                    **self._finding().model_dump(),
                    "affected_node_ids": tuple(f"node-{index}" for index in range(17)),
                },
                strict=True,
            )
        self.assertTrue(
            {"verdict", "graph", "capability", "score"}.isdisjoint(PlanReviewPayload.model_fields)
        )

    def test_missing_coverage_may_target_the_goal_without_inventing_a_node(self) -> None:
        finding = PlanReviewFinding(
            id="finding-coverage",
            finding_type=PlanReviewFindingType.MISSING_GOAL_COVERAGE,
            impact=PlanReviewImpact.BLOCKING,
            affected_node_ids=(),
            goal_relation="No proposed node addresses one accepted Goal requirement.",
            smallest_correction="Add the smallest node that covers the requirement.",
        )
        self.assertEqual(PlanReviewPayload(findings=(finding,)).findings, (finding,))

    def test_cross_record_validator_returns_complete_deterministic_issues(self) -> None:
        finding = self._finding(node_id="unknown-node")
        issues = validate_plan_review(
            PlanReviewPayload(findings=(finding,)),
            goal=self.goal,
            proposed_graph=self.proposed_graph,
        )
        self.assertEqual(
            {issue.code for issue in issues},
            {"unknown_node_reference"},
        )
        with self.assertRaises(PlanReviewValidationError) as caught:
            bind_plan_review(
                PlanReviewPayload(findings=(finding,)),
                record_id="invalid-review",
                run_id="review-run",
                created_at=self.created_at,
                review_round=0,
                goal=self.goal,
                proposed_graph=self.proposed_graph,
                reviewer_strategy=self.strategy,
            )
        self.assertEqual(caught.exception.issues, issues)

    def test_binding_uses_only_trusted_canonical_inputs_and_is_immutable(self) -> None:
        payload = PlanReviewPayload(findings=(self._finding(),))
        first = bind_plan_review(
            payload,
            record_id="review-first",
            run_id="review-run",
            created_at=self.created_at,
            review_round=0,
            goal=self.goal,
            proposed_graph=self.proposed_graph,
            reviewer_strategy=self.strategy,
        )
        second = bind_plan_review(
            payload,
            record_id="review-second",
            run_id="another-run",
            created_at=self.created_at + timedelta(seconds=1),
            review_round=0,
            goal=self.goal,
            proposed_graph=self.proposed_graph,
            reviewer_strategy=self.strategy,
        )
        self.assertEqual(first.goal_digest, self._goal_digest())
        self.assertEqual(first.proposed_graph_digest, self.proposed_graph.content_digest)
        self.assertEqual(first.effective_policy_digest, _DIGEST_A)
        self.assertEqual(first.harness_digest, _DIGEST_B)
        self.assertEqual(first.content_digest, second.content_digest)
        with self.assertRaises(ValidationError):
            first.findings = ()

        mismatched = self.proposed_graph.model_copy(update={"goal_digest": "c" * 64})
        with self.assertRaises(PlanReviewValidationError) as caught:
            bind_plan_review(
                payload,
                record_id="mismatched-review",
                run_id="review-run",
                created_at=self.created_at,
                review_round=0,
                goal=self.goal,
                proposed_graph=mismatched,
                reviewer_strategy=self.strategy,
            )
        self.assertEqual(
            {issue.code for issue in caught.exception.issues},
            {"goal_digest_mismatch"},
        )

    def test_round_rule_is_pure_bounded_and_deterministic(self) -> None:
        for review_round, expected in (
            (0, PlanReviewAction.REQUEST_REVISION),
            (1, PlanReviewAction.REJECT),
        ):
            review = self._review(
                review_round=review_round,
                findings=(self._finding(),),
            )
            self.assertEqual(decide_plan_review_action(review), expected)
            self.assertEqual(decide_plan_review_action(review), expected)

        accepted = self._review(
            review_round=0,
            findings=(self._finding(blocking=False),),
        )
        empty = self._review(review_round=1, findings=())
        self.assertEqual(decide_plan_review_action(accepted), PlanReviewAction.ACCEPT)
        self.assertEqual(decide_plan_review_action(empty), PlanReviewAction.ACCEPT)

        with self.assertRaises(ValidationError):
            self._review(review_round=2, findings=())


if __name__ == "__main__":
    unittest.main()
