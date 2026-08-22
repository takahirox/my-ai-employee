from __future__ import annotations

import unittest

from ai_employee.domain import Budget, Failure, FailureKind
from ai_employee.escalation import EscalationAction, decide_escalation


class EscalationTests(unittest.TestCase):
    def test_retry_replan_and_exhaustion_are_budgeted(self) -> None:
        budget = Budget(max_retries=1, max_replans=1)
        retryable = Failure(
            id="retry", kind=FailureKind.EXECUTION, code="temporary",
            message="temporary", retryable=True,
        )
        self.assertEqual(
            decide_escalation(
                retryable, attempt=0, replan_count=0, budget=budget, node_retry_limit=2,
            ).action,
            EscalationAction.RETRY,
        )
        graph_failure = retryable.model_copy(update={"kind": FailureKind.GRAPH, "retryable": False})
        self.assertEqual(
            decide_escalation(
                graph_failure, attempt=1, replan_count=0, budget=budget, node_retry_limit=2,
            ).action,
            EscalationAction.REPLAN,
        )
        self.assertEqual(
            decide_escalation(
                graph_failure, attempt=1, replan_count=1, budget=budget, node_retry_limit=2,
            ).action,
            EscalationAction.EXHAUST,
        )

    def test_external_blockers_and_cancellation_do_not_retry(self) -> None:
        for kind, action in (
            (FailureKind.EXTERNAL_BLOCKER, EscalationAction.BLOCK),
            (FailureKind.CANCELLATION, EscalationAction.CANCEL),
        ):
            failure = Failure(
                id=f"failure-{kind.value}", kind=kind, code=kind.value,
                message=kind.value, retryable=True,
            )
            self.assertEqual(
                decide_escalation(
                    failure, attempt=0, replan_count=0, budget=Budget(max_retries=5),
                    node_retry_limit=5,
                ).action,
                action,
            )


if __name__ == "__main__":
    unittest.main()
