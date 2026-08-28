"""Deterministic validation and acceptance at the graph trust boundary."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .domain import AcceptedGraphRevision, ExecutionPolicy, Graph, NodeKind


@dataclass(frozen=True, order=True)
class GraphValidationIssue:
    code: str
    subject_id: str
    message: str


class GraphValidationError(ValueError):
    def __init__(self, issues: Iterable[GraphValidationIssue]) -> None:
        self.issues = tuple(sorted(issues))
        super().__init__("; ".join(f"{item.code}:{item.subject_id}" for item in self.issues))


def validate_graph(
    graph: Graph,
    policy: ExecutionPolicy,
    *,
    available_capabilities: Iterable[str] = (),
) -> tuple[GraphValidationIssue, ...]:
    """Return the complete, deterministic issue set for a candidate graph."""

    issues: list[GraphValidationIssue] = []
    nodes = {node.id: node for node in graph.nodes}
    available = set(available_capabilities)
    denied = set(policy.denied_capabilities)
    if len(nodes) > min(graph.budget.max_nodes, policy.max_nodes):
        issues.append(GraphValidationIssue("node_budget_exceeded", graph.id, "too many nodes"))
    if graph.budget.max_attempts > policy.max_attempts:
        issues.append(
            GraphValidationIssue("attempt_budget_exceeded", graph.id, "attempt cap exceeds policy")
        )
    totals = {
        "worker_turns": sum(node.resource_budget.worker_turns for node in graph.nodes),
        "processes": sum(node.resource_budget.processes for node in graph.nodes),
        "wall_seconds": sum(node.resource_budget.wall_seconds for node in graph.nodes),
        "artifact_bytes": sum(node.resource_budget.artifact_bytes for node in graph.nodes),
    }
    limits = {
        "worker_turns": graph.budget.max_worker_turns,
        "processes": graph.budget.max_processes,
        "wall_seconds": graph.budget.max_wall_seconds,
        "artifact_bytes": graph.budget.max_artifact_bytes,
    }
    for resource in sorted(totals):
        if totals[resource] > limits[resource]:
            issues.append(
                GraphValidationIssue(
                    "aggregate_resource_budget_insufficient",
                    graph.id,
                    f"declared {resource} reservations exceed the graph budget",
                )
            )
    if graph.budget.max_wall_seconds > policy.max_wall_seconds:
        issues.append(
            GraphValidationIssue("time_budget_exceeded", graph.id, "time cap exceeds policy")
        )

    adjacency: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source_id].append(edge.target_id)
        reverse[edge.target_id].append(edge.source_id)
        if (
            edge.loop
            and edge.max_traversals is not None
            and edge.max_traversals > graph.budget.max_loop_iterations
        ):
            issues.append(
                GraphValidationIssue("loop_budget_exceeded", edge.id, "loop exceeds graph budget")
            )
    for node in graph.nodes:
        if node.kind not in set(NodeKind):
            issues.append(GraphValidationIssue("unsupported_node_kind", node.id, str(node.kind)))
        if node.retry_limit > graph.budget.max_retries:
            issues.append(
                GraphValidationIssue("retry_budget_exceeded", node.id, "retry exceeds graph budget")
            )
        if node.max_iterations > graph.budget.max_loop_iterations:
            issues.append(
                GraphValidationIssue(
                    "iteration_budget_exceeded", node.id, "iteration exceeds graph budget"
                )
            )
        for capability in sorted(set(node.required_capabilities)):
            if capability in denied:
                issues.append(GraphValidationIssue("capability_denied", node.id, capability))
            elif capability not in available:
                issues.append(GraphValidationIssue("capability_unavailable", node.id, capability))

    reachable = _walk(graph.entry_node_ids, adjacency)
    for node_id in sorted(set(nodes) - reachable):
        issues.append(
            GraphValidationIssue("unreachable_node", node_id, "not reachable from an entry")
        )
    can_reach_terminal = _walk(graph.terminal_node_ids, reverse)
    for node_id in sorted(reachable - can_reach_terminal):
        issues.append(GraphValidationIssue("no_terminal_path", node_id, "cannot reach a terminal"))
    for component in _cyclic_components(nodes, adjacency):
        internal = [
            edge
            for edge in graph.edges
            if edge.source_id in component and edge.target_id in component
        ]
        bounded_loop_edges = [
            edge for edge in internal if edge.loop and edge.max_traversals is not None
        ]
        # Declared loop edges are the only repeatable back-edges. Removing them
        # must break the component's cycle, making the bound enforceable.
        without_loops: dict[str, list[str]] = defaultdict(list)
        for edge in internal:
            if not edge.loop:
                without_loops[edge.source_id].append(edge.target_id)
        residual_cycle = bool(
            _cyclic_components({node_id: object() for node_id in component}, without_loops)
        )
        if not bounded_loop_edges or residual_cycle:
            issues.append(
                GraphValidationIssue(
                    "unbounded_cycle", min(component), "cycle edges require bounds"
                )
            )
    return tuple(sorted(set(issues)))


def validate_task_graph(
    graph: Graph,
    policy: ExecutionPolicy,
    *,
    available_capabilities: Iterable[str] = (),
) -> tuple[GraphValidationIssue, ...]:
    """Validate the initial task-orchestration subset: a bounded dependency DAG."""

    issues = list(
        validate_graph(
            graph,
            policy,
            available_capabilities=available_capabilities,
        )
    )
    incoming: dict[str, int] = {node.id: 0 for node in graph.nodes}
    outgoing: dict[str, int] = {node.id: 0 for node in graph.nodes}
    for edge in graph.edges:
        incoming[edge.target_id] += 1
        outgoing[edge.source_id] += 1
        if edge.loop or edge.max_traversals is not None or edge.condition is not None:
            issues.append(
                GraphValidationIssue(
                    "unsupported_edge_semantics",
                    edge.id,
                    "task orchestration initially supports required dependencies only",
                )
            )
    expected_entries = tuple(sorted(node_id for node_id, count in incoming.items() if count == 0))
    expected_terminals = tuple(sorted(node_id for node_id, count in outgoing.items() if count == 0))
    if tuple(sorted(graph.entry_node_ids)) != expected_entries:
        issues.append(
            GraphValidationIssue("invalid_entry_set", graph.id, "entries must be all DAG roots")
        )
    if tuple(sorted(graph.terminal_node_ids)) != expected_terminals:
        issues.append(
            GraphValidationIssue(
                "invalid_terminal_set", graph.id, "terminals must be all DAG leaves"
            )
        )
    if graph.budget.max_attempts < len(graph.nodes):
        issues.append(
            GraphValidationIssue(
                "attempt_budget_insufficient", graph.id, "every DAG node needs one claim"
            )
        )
    for node in graph.nodes:
        if node.objective is None or not node.objective.strip():
            issues.append(GraphValidationIssue("missing_objective", node.id, "objective required"))
        if not node.completion_criteria:
            issues.append(
                GraphValidationIssue("missing_completion_criteria", node.id, "criteria required")
            )
        if node.max_iterations != 1 or node.generation or node.attempt:
            issues.append(
                GraphValidationIssue(
                    "unsupported_execution_fence",
                    node.id,
                    "loops and pre-advanced execution fences are out of scope",
                )
            )
    return tuple(sorted(set(issues)))


def _walk(starts: Iterable[str], adjacency: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(sorted(starts))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(sorted(adjacency[current]))
    return seen


def _cyclic_components(
    nodes: Mapping[str, object], adjacency: Mapping[str, list[str]]
) -> list[set[str]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    result: list[set[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = low[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for target in sorted(adjacency[node_id]):
            if target not in indices:
                visit(target)
                low[node_id] = min(low[node_id], low[target])
            elif target in on_stack:
                low[node_id] = min(low[node_id], indices[target])
        if low[node_id] == indices[node_id]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.add(member)
                if member == node_id:
                    break
            if len(component) > 1 or node_id in adjacency[node_id]:
                result.append(component)

    for node_id in sorted(nodes):
        if node_id not in indices:
            visit(node_id)
    return sorted(result, key=lambda item: min(item))


def accept_graph(
    candidate: Graph,
    policy: ExecutionPolicy,
    *,
    previous: AcceptedGraphRevision | None = None,
    available_capabilities: Iterable[str] = (),
) -> AcceptedGraphRevision:
    """Validate and defensively snapshot a candidate as the next revision."""

    issues = validate_graph(candidate, policy, available_capabilities=available_capabilities)
    if issues:
        raise GraphValidationError(issues)
    revision = 1 if previous is None else previous.revision_number + 1
    return AcceptedGraphRevision(revision_number=revision, graph=candidate)


def accept_task_graph(
    candidate: Graph,
    policy: ExecutionPolicy,
    *,
    previous: AcceptedGraphRevision | None = None,
    available_capabilities: Iterable[str] = (),
) -> AcceptedGraphRevision:
    """Accept one strict dependency-DAG revision deterministically."""

    issues = validate_task_graph(
        candidate,
        policy,
        available_capabilities=available_capabilities,
    )
    if issues:
        raise GraphValidationError(issues)
    return AcceptedGraphRevision(
        revision_number=1 if previous is None else previous.revision_number + 1,
        graph=candidate,
    )


def replan(
    accepted: AcceptedGraphRevision,
    candidate: Graph,
    policy: ExecutionPolicy,
    *,
    available_capabilities: Iterable[str] = (),
) -> AcceptedGraphRevision:
    """Create a new accepted revision; the previous value remains immutable."""

    return accept_graph(
        candidate, policy, previous=accepted, available_capabilities=available_capabilities
    )
