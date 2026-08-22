from __future__ import annotations

import unittest

from ai_employee.demo import demo_graph
from ai_employee.domain import Budget, ExecutionPolicy
from ai_employee.graph import GraphValidationError, accept_graph, replan, validate_graph


class GraphBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)

    def test_demo_graph_is_valid_and_acceptance_is_immutable(self) -> None:
        graph = demo_graph()
        self.assertEqual(validate_graph(graph, self.policy), ())
        accepted = accept_graph(graph, self.policy)
        self.assertEqual(accepted.revision_number, 1)
        self.assertEqual(len(accepted.content_digest), 64)
        with self.assertRaises(ValueError):
            accepted.graph.nodes[0].name = "changed"

    def test_replanning_creates_next_revision(self) -> None:
        first = accept_graph(demo_graph(), self.policy)
        second = replan(first, demo_graph().model_copy(update={"id": "demo-graph-2"}), self.policy)
        self.assertEqual(first.revision_number, 1)
        self.assertEqual(second.revision_number, 2)
        self.assertEqual(first.graph.id, "demo-graph")

    def test_unbounded_cycle_and_policy_budget_are_reported_together(self) -> None:
        base = demo_graph()
        edges = tuple(
            edge.model_copy(update={"loop": False, "max_traversals": None})
            if edge.id == "repair-gate"
            else edge
            for edge in base.edges
        )
        graph = base.model_copy(
            update={
                "edges": edges,
                "budget": Budget(
                    max_attempts=8,
                    max_retries=1,
                    max_nodes=10,
                    max_wall_seconds=120.0,
                    max_loop_iterations=2,
                ),
            }
        )
        codes = {item.code for item in validate_graph(graph, self.policy)}
        self.assertEqual(codes, {"time_budget_exceeded", "unbounded_cycle"})
        with self.assertRaises(GraphValidationError) as caught:
            accept_graph(graph, self.policy)
        self.assertEqual({item.code for item in caught.exception.issues}, codes)


if __name__ == "__main__":
    unittest.main()
