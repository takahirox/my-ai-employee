from __future__ import annotations

import unittest

from ai_employee.domain import (
    NODE_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    Failure,
    FailureKind,
    NodeState,
    RunState,
    StateTransition,
    TaskState,
    TransitionError,
    transition_node,
    transition_run,
    transition_task,
)
from ai_employee.serialization import canonical_json, loads_model

from tests.helpers import node, provenance, run, task


def transition_failure(target: object) -> Failure | None:
    if target in {RunState.EXHAUSTED, TaskState.EXHAUSTED, NodeState.EXHAUSTED}:
        kind = FailureKind.RESOURCE_EXHAUSTION
    elif target in {RunState.BLOCKED, TaskState.BLOCKED, NodeState.BLOCKED}:
        kind = FailureKind.EXTERNAL_BLOCKER
    elif target in {RunState.FAILED, TaskState.FAILED, NodeState.FAILED}:
        kind = FailureKind.EXECUTION
    else:
        return None
    return Failure(
        id="failure.transition",
        kind=kind,
        code="transition_reason",
        message="structured transition reason",
    )


class TransitionTests(unittest.TestCase):
    def test_run_transition_table_is_enforced_exhaustively(self) -> None:
        base = run()
        for source, allowed in RUN_TRANSITIONS.items():
            for target in RunState:
                candidate = base.model_copy(update={"state": source, "transitions": ()})
                operation = lambda: transition_run(  # noqa: E731
                    candidate,
                    target,
                    provenance(),
                    expected_generation=0,
                    expected_graph_revision=1,
                    failure=transition_failure(target),
                )
                with self.subTest(source=source, target=target):
                    if target in allowed:
                        changed = operation()
                        self.assertEqual(changed.state, target)
                        self.assertEqual(len(changed.transitions), 1)
                    else:
                        with self.assertRaises(TransitionError):
                            operation()

    def test_task_transition_table_is_enforced_exhaustively(self) -> None:
        base = task()
        for source, allowed in TASK_TRANSITIONS.items():
            for target in TaskState:
                candidate = base.model_copy(update={"state": source, "transitions": ()})
                operation = lambda: transition_task(  # noqa: E731
                    candidate,
                    target,
                    provenance(),
                    expected_generation=0,
                    expected_graph_revision=1,
                    failure=transition_failure(target),
                )
                with self.subTest(source=source, target=target):
                    if target in allowed:
                        self.assertEqual(operation().state, target)
                    else:
                        with self.assertRaises(TransitionError):
                            operation()

    def test_node_transition_table_is_enforced_exhaustively(self) -> None:
        base = node()
        for source, allowed in NODE_TRANSITIONS.items():
            for target in NodeState:
                candidate = base.model_copy(update={"state": source, "transitions": ()})
                operation = lambda: transition_node(  # noqa: E731
                    candidate,
                    target,
                    provenance(),
                    expected_generation=0,
                    expected_graph_revision=1,
                    failure=transition_failure(target),
                )
                with self.subTest(source=source, target=target):
                    if target in allowed:
                        self.assertEqual(operation().state, target)
                    else:
                        with self.assertRaises(TransitionError):
                            operation()

    def test_generation_and_revision_fences_reject_stale_results(self) -> None:
        candidate = node()
        with self.assertRaises(TransitionError) as generation:
            transition_node(
                candidate,
                NodeState.READY,
                provenance(),
                expected_generation=1,
                expected_graph_revision=1,
            )
        self.assertEqual(generation.exception.failure.code, "stale_generation")
        with self.assertRaises(TransitionError) as revision:
            transition_node(
                candidate,
                NodeState.READY,
                provenance(),
                expected_generation=0,
                expected_graph_revision=2,
            )
        self.assertEqual(revision.exception.failure.code, "stale_graph_revision")

    def test_exhausted_is_distinct_from_failed(self) -> None:
        running = node().model_copy(update={"state": NodeState.RUNNING})
        exhausted = transition_node(
            running,
            NodeState.EXHAUSTED,
            provenance(),
            expected_generation=0,
            expected_graph_revision=1,
            failure=transition_failure(NodeState.EXHAUSTED),
        )
        self.assertEqual(exhausted.state, NodeState.EXHAUSTED)
        self.assertNotEqual(exhausted.state.value, NodeState.FAILED.value)
        with self.assertRaises(TransitionError):
            transition_node(
                running,
                NodeState.EXHAUSTED,
                provenance(),
                expected_generation=0,
                expected_graph_revision=1,
                failure=transition_failure(NodeState.FAILED),
            )

    def test_non_failure_transition_rejects_structured_failure(self) -> None:
        with self.assertRaises(TransitionError) as raised:
            transition_node(
                node(),
                NodeState.READY,
                provenance(),
                expected_generation=0,
                expected_graph_revision=1,
                failure=Failure(
                    id="failure.unexpected",
                    kind=FailureKind.EXECUTION,
                    code="unexpected_failure",
                    message="must not survive a successful transition",
                ),
            )
        self.assertEqual(raised.exception.failure.code, "unexpected_transition_failure")

    def test_provenance_is_recorded_completely(self) -> None:
        changed = transition_node(
            node(),
            NodeState.READY,
            provenance(),
            expected_generation=0,
            expected_graph_revision=1,
        )
        record = changed.transitions[0]
        self.assertEqual(record.entity_kind, "node")
        self.assertEqual(record.provenance.rule_version, "transition.v1")
        self.assertEqual(len(record.provenance.evidence_digest), 64)
        restored = loads_model(canonical_json(record), StateTransition)
        self.assertIsInstance(restored.from_state, NodeState)
        self.assertIsInstance(restored.to_state, NodeState)


if __name__ == "__main__":
    unittest.main()
