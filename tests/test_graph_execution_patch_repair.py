from __future__ import annotations

from datetime import UTC, datetime

import pytest

import ai_employee.graph_execution as graph_execution
from ai_employee.domain import (
    ExecutionStrategy,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.v2 import (
    ArtifactDescriptor,
    WorkerRequest,
    WorkerResult,
    WorkspaceSnapshot,
)
from ai_employee.graph_composition import NodePatchArtifact
from ai_employee.task_orchestration import NodeExecutionResult

NOW = datetime(2026, 1, 1, tzinfo=UTC)
REVISION_A = "a" * 64
REVISION_B = "b" * 64
ZERO = "0" * 64

STRATEGY = ExecutionStrategy(
    id="strategy",
    routing_mode=RoutingMode.ADAPTIVE,
    backend="fixture",
    model="fixture",
    capabilities=("edit_intent",),
)
NODE = Node(
    id="node",
    kind=NodeKind.FUNCTION,
    name="node",
    objective="produce a patch",
    output_contract=OutputContract(id="output"),
    required_capabilities=("edit_intent",),
    generation=2,
)


class _Store:
    def close(self) -> None:
        pass


class _Coordinator:
    def __init__(self, strategy: ExecutionStrategy) -> None:
        self.selected_strategy = strategy
        self.store = _Store()

    def execute_node(self, *_args: object, **_kwargs: object) -> object:
        return object()


def _patch(
    *,
    node_id: str = "node",
    revision: str = REVISION_A,
    generation: int = 2,
    attempt: int,
) -> NodePatchArtifact:
    inner_run_id = f"inner-{node_id}-{generation}-{attempt}"
    workspace = WorkspaceSnapshot(
        id=f"workspace-{node_id}-{generation}-{attempt}",
        run_id=inner_run_id,
        created_at=NOW,
        repository_identity="1" * 64,
        original_worktree="/repo",
        head_commit="c" * 40,
        base_tree="d" * 40,
        dirty_state_digest="2" * 64,
        isolated_worktree=f"/workspaces/{inner_run_id}",
        worktree_metadata={},
    )
    descriptor = ArtifactDescriptor(
        id=f"patch-{node_id}-{generation}-{attempt}",
        run_id=inner_run_id,
        created_at=NOW,
        artifact_digest="3" * 64,
        media_type="text/x-diff",
        size_bytes=1,
        logical_kind="workspace_patch",
        producer_action_id=workspace.id,
        source={},
        store_locator=f"sha256/{'3' * 64}",
    )
    return NodePatchArtifact(
        node_id=node_id,
        graph_run_id="graph-run",
        accepted_graph_revision_digest=revision,
        generation=generation,
        attempt=attempt,
        worker_request_digest=ZERO,
        worker_result_digest=ZERO,
        acceptance_ledger_digest=ZERO,
        workspace=workspace,
        patch=descriptor,
    )


def _result(patch: NodePatchArtifact) -> NodeExecutionResult:
    return NodeExecutionResult(
        worker_result=WorkerResult(
            id=f"result-{patch.node_id}-{patch.generation}-{patch.attempt}",
            run_id=patch.workspace.run_id,
            created_at=NOW,
            request_digest=ZERO,
            status="succeeded",
            duration_seconds=0.01,
        ),
        criterion_evidence=(),
        workspace_id=patch.workspace.id,
        node_patch=patch,
    )


def _request(patch: NodePatchArtifact) -> WorkerRequest:
    return WorkerRequest(
        id=f"request-{patch.node_id}-{patch.generation}-{patch.attempt}",
        run_id=patch.workspace.run_id,
        created_at=NOW,
        goal="produce a patch",
        accepted_plan_digest=patch.accepted_graph_revision_digest,
        node_id=NODE.id,
        accepted_graph_revision_digest=patch.accepted_graph_revision_digest,
        graph_run_id=patch.graph_run_id,
        generation=patch.generation,
        attempt=patch.attempt,
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={},
    )


def _session(
    monkeypatch: pytest.MonkeyPatch,
    results: tuple[NodeExecutionResult, ...],
) -> graph_execution._ExecutionSession:
    outputs = iter(results)
    monkeypatch.setattr(
        graph_execution,
        "_authoritative_node_result",
        lambda *_args: next(outputs),
    )

    def factory(
        _node: Node,
        _request: WorkerRequest,
        strategy: ExecutionStrategy,
    ) -> _Coordinator:
        return _Coordinator(strategy)

    return graph_execution._ExecutionSession(factory, "/repo", "c" * 40)


def _run(
    session: graph_execution._ExecutionSession,
    patch: NodePatchArtifact,
) -> NodeExecutionResult:
    node = NODE.model_copy(update={"generation": patch.generation, "attempt": patch.attempt})
    return session.run_node(node, _request(patch), STRATEGY)


def test_strictly_newer_repair_replaces_authoritative_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _result(_patch(attempt=0))
    repaired = _result(_patch(attempt=1))
    session = _session(monkeypatch, (original, repaired))

    _run(session, original.node_patch)
    _run(session, repaired.node_patch)

    assert set(session.node_patches) == {"node"}
    assert session.node_patches["node"] is repaired.node_patch


@pytest.mark.parametrize(
    ("node_id", "revision", "generation", "attempt"),
    (
        pytest.param("node", REVISION_A, 2, 1, id="duplicate-attempt"),
        pytest.param("node", REVISION_A, 2, 0, id="lower-attempt"),
        pytest.param("node", REVISION_A, 3, 2, id="different-generation"),
        pytest.param("node", REVISION_B, 2, 2, id="different-graph-revision"),
        pytest.param("other-node", REVISION_A, 2, 2, id="mismatched-node"),
    ),
)
def test_invalid_repair_patch_is_rejected_without_replacing_authority(
    monkeypatch: pytest.MonkeyPatch,
    node_id: str,
    revision: str,
    generation: int,
    attempt: int,
) -> None:
    original = _result(_patch(attempt=1))
    replacement = _result(
        _patch(
            node_id=node_id,
            revision=revision,
            generation=generation,
            attempt=attempt,
        )
    )
    session = _session(monkeypatch, (original, replacement))

    _run(session, original.node_patch)
    with pytest.raises(ValueError, match="node patch"):
        _run(session, replacement.node_patch)

    assert session.node_patches["node"] is original.node_patch
