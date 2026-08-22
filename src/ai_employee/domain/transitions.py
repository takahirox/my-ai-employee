"""Deterministic, table-driven reducers for authoritative state changes."""

from __future__ import annotations

from collections.abc import Mapping, Set
from types import MappingProxyType
from typing import TypeVar

from .base import freeze_json
from .enums import FailureKind, NodeState, RunState, TaskState
from .models import Failure, Node, Run, StateTransition, Task, TransitionProvenance

StateT = TypeVar("StateT", RunState, TaskState, NodeState)


def _immutable_transition_table(
    table: Mapping[StateT, Set[StateT]],
) -> Mapping[StateT, Set[StateT]]:
    return MappingProxyType({source: frozenset(targets) for source, targets in table.items()})


RUN_TRANSITIONS: Mapping[RunState, Set[RunState]] = _immutable_transition_table(
    {
        RunState.CREATED: {RunState.READY, RunState.CANCELLED, RunState.FAILED},
        RunState.READY: {
            RunState.RUNNING,
            RunState.PAUSED,
            RunState.CANCELLED,
            RunState.FAILED,
            RunState.BLOCKED,
        },
        RunState.RUNNING: {
            RunState.PAUSED,
            RunState.CANCELLING,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.EXHAUSTED,
            RunState.BLOCKED,
        },
        RunState.PAUSED: {
            RunState.READY,
            RunState.RUNNING,
            RunState.CANCELLING,
            RunState.CANCELLED,
            RunState.BLOCKED,
        },
        RunState.CANCELLING: {RunState.CANCELLED, RunState.FAILED},
        RunState.BLOCKED: {RunState.READY, RunState.CANCELLED, RunState.FAILED},
        RunState.CANCELLED: set(),
        RunState.SUCCEEDED: set(),
        RunState.FAILED: set(),
        RunState.EXHAUSTED: set(),
    }
)

TASK_TRANSITIONS: Mapping[TaskState, Set[TaskState]] = _immutable_transition_table(
    {
        TaskState.PENDING: {TaskState.READY, TaskState.SKIPPED, TaskState.CANCELLED},
        TaskState.READY: {
            TaskState.RUNNING,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
            TaskState.BLOCKED,
        },
        TaskState.RUNNING: {
            TaskState.PAUSED,
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.EXHAUSTED,
            TaskState.CANCELLED,
            TaskState.BLOCKED,
        },
        TaskState.PAUSED: {TaskState.READY, TaskState.RUNNING, TaskState.CANCELLED},
        TaskState.BLOCKED: {TaskState.READY, TaskState.CANCELLED, TaskState.FAILED},
        TaskState.SUCCEEDED: set(),
        TaskState.FAILED: set(),
        TaskState.EXHAUSTED: set(),
        TaskState.CANCELLED: set(),
        TaskState.SKIPPED: set(),
    }
)

NODE_TRANSITIONS: Mapping[NodeState, Set[NodeState]] = _immutable_transition_table(
    {
        NodeState.PENDING: {NodeState.READY, NodeState.SKIPPED, NodeState.CANCELLED},
        NodeState.READY: {
            NodeState.RUNNING,
            NodeState.SKIPPED,
            NodeState.CANCELLED,
            NodeState.BLOCKED,
        },
        NodeState.RUNNING: {
            NodeState.WAITING,
            NodeState.SUCCEEDED,
            NodeState.FAILED,
            NodeState.EXHAUSTED,
            NodeState.CANCELLED,
            NodeState.BLOCKED,
        },
        NodeState.WAITING: {
            NodeState.READY,
            NodeState.RUNNING,
            NodeState.FAILED,
            NodeState.EXHAUSTED,
            NodeState.CANCELLED,
            NodeState.BLOCKED,
        },
        NodeState.BLOCKED: {NodeState.READY, NodeState.CANCELLED, NodeState.FAILED},
        NodeState.SUCCEEDED: set(),
        NodeState.FAILED: set(),
        NodeState.EXHAUSTED: set(),
        NodeState.CANCELLED: set(),
        NodeState.SKIPPED: set(),
    }
)

FailureState = RunState | TaskState | NodeState


class TransitionError(ValueError):
    """Rejected transition carrying a stable, serializable failure."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _reject(kind: FailureKind, code: str, message: str, details: object) -> TransitionError:
    return TransitionError(
        Failure(
            id="transition_failure",
            kind=kind,
            code=code,
            message=message,
            retryable=False,
            details=freeze_json(details),
        )
    )


def _validate_fences(
    *,
    actual_generation: int,
    expected_generation: int,
    actual_graph_revision: int,
    expected_graph_revision: int,
) -> None:
    if actual_generation != expected_generation:
        raise _reject(
            FailureKind.VALIDATION,
            "stale_generation",
            "transition rejected by the generation fence",
            {"actual": actual_generation, "expected": expected_generation},
        )
    if actual_graph_revision != expected_graph_revision:
        raise _reject(
            FailureKind.GRAPH,
            "stale_graph_revision",
            "transition rejected by the accepted graph revision fence",
            {"actual": actual_graph_revision, "expected": expected_graph_revision},
        )


def _validate_transition(
    *,
    current: StateT,
    target: StateT,
    table: Mapping[StateT, Set[StateT]],
    failure: Failure | None,
) -> None:
    if target not in table[current]:
        raise _reject(
            FailureKind.VALIDATION,
            "invalid_state_transition",
            f"transition from {current.value!r} to {target.value!r} is not allowed",
            {"from": current.value, "to": target.value},
        )
    failure_targets: set[FailureState] = {
        RunState.FAILED,
        RunState.EXHAUSTED,
        RunState.BLOCKED,
        TaskState.FAILED,
        TaskState.EXHAUSTED,
        TaskState.BLOCKED,
        NodeState.FAILED,
        NodeState.EXHAUSTED,
        NodeState.BLOCKED,
    }
    if target in failure_targets and failure is None:
        raise _reject(
            FailureKind.INVALID_OUTPUT,
            "missing_transition_failure",
            f"transition to {target.value!r} requires a structured failure",
            {"to": target.value},
        )
    cancellation_targets: set[FailureState] = {
        RunState.CANCELLED,
        TaskState.CANCELLED,
        NodeState.CANCELLED,
    }
    if failure is not None and target not in failure_targets | cancellation_targets:
        raise _reject(
            FailureKind.INVALID_OUTPUT,
            "unexpected_transition_failure",
            f"transition to {target.value!r} cannot retain a structured failure",
            {"to": target.value, "failure_kind": failure.kind.value},
        )
    if (
        target in cancellation_targets
        and failure is not None
        and failure.kind is not FailureKind.CANCELLATION
    ):
        raise _reject(
            FailureKind.INVALID_OUTPUT,
            "invalid_cancellation_failure",
            "CANCELLED only accepts a cancellation failure",
            {"failure_kind": failure.kind.value},
        )
    exhaustion_targets: set[FailureState] = {
        RunState.EXHAUSTED,
        TaskState.EXHAUSTED,
        NodeState.EXHAUSTED,
    }
    if (
        target in exhaustion_targets
        and failure is not None
        and failure.kind is not FailureKind.RESOURCE_EXHAUSTION
    ):
        raise _reject(
            FailureKind.INVALID_OUTPUT,
            "invalid_exhaustion_failure",
            "EXHAUSTED requires a resource_exhaustion failure",
            {"failure_kind": failure.kind.value},
        )


def transition_run(
    run: Run,
    target: RunState,
    provenance: TransitionProvenance,
    *,
    expected_generation: int,
    expected_graph_revision: int,
    failure: Failure | None = None,
) -> Run:
    """Return a new Run after applying an authorized, fenced transition."""

    graph_revision = run.accepted_graph.revision_number
    _validate_fences(
        actual_generation=run.generation,
        expected_generation=expected_generation,
        actual_graph_revision=graph_revision,
        expected_graph_revision=expected_graph_revision,
    )
    _validate_transition(current=run.state, target=target, table=RUN_TRANSITIONS, failure=failure)
    transition = StateTransition(
        entity_kind="run",
        entity_id=run.id,
        from_state=run.state,
        to_state=target,
        generation=run.generation,
        graph_revision=graph_revision,
        provenance=provenance,
    )
    return run.model_copy(
        update={
            "state": target,
            "failure": failure,
            "transitions": (*run.transitions, transition),
        }
    )


def transition_task(
    task: Task,
    target: TaskState,
    provenance: TransitionProvenance,
    *,
    expected_generation: int,
    expected_graph_revision: int,
    failure: Failure | None = None,
) -> Task:
    """Return a new Task after applying an authorized, fenced transition."""

    _validate_fences(
        actual_generation=task.generation,
        expected_generation=expected_generation,
        actual_graph_revision=task.graph_revision,
        expected_graph_revision=expected_graph_revision,
    )
    _validate_transition(current=task.state, target=target, table=TASK_TRANSITIONS, failure=failure)
    transition = StateTransition(
        entity_kind="task",
        entity_id=task.id,
        from_state=task.state,
        to_state=target,
        generation=task.generation,
        graph_revision=task.graph_revision,
        provenance=provenance,
    )
    return task.model_copy(
        update={
            "state": target,
            "failure": failure,
            "transitions": (*task.transitions, transition),
        }
    )


def transition_node(
    node: Node,
    target: NodeState,
    provenance: TransitionProvenance,
    *,
    expected_generation: int,
    expected_graph_revision: int,
    failure: Failure | None = None,
) -> Node:
    """Return a new Node after applying an authorized, fenced transition."""

    _validate_fences(
        actual_generation=node.generation,
        expected_generation=expected_generation,
        actual_graph_revision=node.graph_revision,
        expected_graph_revision=expected_graph_revision,
    )
    _validate_transition(current=node.state, target=target, table=NODE_TRANSITIONS, failure=failure)
    transition = StateTransition(
        entity_kind="node",
        entity_id=node.id,
        from_state=node.state,
        to_state=target,
        generation=node.generation,
        graph_revision=node.graph_revision,
        provenance=provenance,
    )
    return node.model_copy(
        update={
            "state": target,
            "failure": failure,
            "transitions": (*node.transitions, transition),
        }
    )
