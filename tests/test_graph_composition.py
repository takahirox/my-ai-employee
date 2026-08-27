from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ai_employee.domain import (
    AcceptedGraphRevision,
    Budget,
    Graph,
    Node,
    NodeKind,
    OutputContract,
)
from ai_employee.domain.v2 import (
    DecisionOutcome,
    EditIntentRequest,
    PolicyDecision,
    StableFailureCode,
    WorkspaceRequest,
)
from ai_employee.graph_composition import (
    GraphPatchComposer,
    GraphPatchCompositionRecord,
    GraphPatchCompositionRequest,
    NodePatchArtifact,
)
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
POLICY_DIGEST = "1" * 64
ZERO = "0" * 64


class NeverCancelled:
    def cancelled(self) -> bool:
        return False


def _node(node_id: str) -> Node:
    return Node(
        id=node_id,
        kind=NodeKind.FUNCTION,
        name=node_id,
        objective=f"produce the bounded patch for {node_id}",
        output_contract=OutputContract(id=f"contract-{node_id}"),
        required_capabilities=("edit_intent", "process"),
    )


def _accepted_graph() -> AcceptedGraphRevision:
    graph = Graph(
        id="composition-graph",
        nodes=(_node("node-b"), _node("node-a")),
        entry_node_ids=("node-b", "node-a"),
        terminal_node_ids=("node-b", "node-a"),
        budget=Budget(max_attempts=2, max_nodes=2, max_wall_seconds=30.0),
    )
    return AcceptedGraphRevision(revision_number=1, graph=graph)


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.test"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Fleet Test"),
        check=True,
    )
    (repository / "a.txt").write_text("a-before\n")
    (repository / "b.txt").write_text("b-before\n")
    (repository / "shared.txt").write_text("shared-before\n")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    return repository, head


def _node_patch(
    manager: GitWorkspaceManager,
    repository: Path,
    head: str,
    *,
    node_id: str,
    path: str,
    content: str,
) -> NodePatchArtifact:
    snapshot = manager.create(
        WorkspaceRequest(
            id=f"workspace-request-{node_id}",
            run_id="composition-run",
            created_at=NOW,
            repository=str(repository),
            base_commit=head,
        )
    )
    Path(snapshot.isolated_worktree, path).write_text(content)
    return NodePatchArtifact(
        node_id=node_id,
        graph_run_id="composition-run",
        accepted_graph_revision_digest=_accepted_graph().content_digest or ZERO,
        generation=0,
        attempt=0,
        worker_request_digest=ZERO,
        worker_result_digest=ZERO,
        acceptance_ledger_digest=ZERO,
        verification_result_digests=(),
        workspace=snapshot,
        patch=manager.capture_diff(snapshot),
    )


def _allow(request: EditIntentRequest) -> PolicyDecision:
    return PolicyDecision(
        id=f"decision-{request.id}",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "0" * 64,
        effective_policy_digest=POLICY_DIGEST,
        outcome=DecisionOutcome.ALLOW,
        reason_code="composition_edit_allowed",
    )


def _request(
    accepted: AcceptedGraphRevision,
    repository: Path,
    head: str,
    patches: tuple[NodePatchArtifact, ...],
) -> GraphPatchCompositionRequest:
    return GraphPatchCompositionRequest(
        id="composition-request",
        run_id="composition-run",
        created_at=NOW,
        accepted_revision=accepted,
        repository=str(repository),
        base_commit=head,
        effective_policy_digest=POLICY_DIGEST,
        node_patches=patches,
    )


def test_independent_node_patches_compose_in_deterministic_order_and_replay(
    tmp_path: Path,
) -> None:
    repository, head = _repository(tmp_path)
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    manager = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    patch_b = _node_patch(
        manager,
        repository,
        head,
        node_id="node-b",
        path="b.txt",
        content="b-after\n",
    )
    patch_a = _node_patch(
        manager,
        repository,
        head,
        node_id="node-a",
        path="a.txt",
        content="a-after\n",
    )
    accepted = _accepted_graph()

    with SQLiteStore(tmp_path / "fleet.db") as store:
        composer = GraphPatchComposer(store, manager, artifacts, _allow)
        request = _request(accepted, repository, head, (patch_b, patch_a))
        record = composer.compose(request, NeverCancelled())

        assert record.status == "succeeded"
        assert record.failure is None
        assert record.candidate_patch is not None
        assert record.composition_workspace is not None
        assert record.request_digest == request.content_digest
        assert record.accepted_graph_revision_digest == accepted.content_digest
        assert tuple(item.node_id for item in record.ordered_inputs) == (
            "node-a",
            "node-b",
        )
        assert tuple(item.patch_digest for item in record.ordered_inputs) == (
            patch_a.patch.artifact_digest,
            patch_b.patch.artifact_digest,
        )
        assert Path(record.composition_workspace.isolated_worktree, "a.txt").read_text() == (
            "a-after\n"
        )
        assert Path(record.composition_workspace.isolated_worktree, "b.txt").read_text() == (
            "b-after\n"
        )
        assert (repository / "a.txt").read_text() == "a-before\n"
        assert (repository / "b.txt").read_text() == "b-before\n"

        with artifacts.open_verified(record.candidate_patch) as stream:
            candidate_body = stream.read().decode()
        assert candidate_body.index("a/a.txt") < candidate_body.index("a/b.txt")
        persisted_candidate = store.get(
            "artifact_descriptor_v2",
            record.candidate_patch.id,
            type(record.candidate_patch),
        )
        assert persisted_candidate == record.candidate_patch
        persisted_record = store.get(
            "graph_patch_composition_v2",
            record.id,
            GraphPatchCompositionRecord,
        )
        assert persisted_record == record
        assert (
            store.get(
                "graph_patch_composition_request_v2",
                request.id,
                GraphPatchCompositionRequest,
            )
            == request
        )

        replay = composer.replay(record.id)
        assert replay.record == record
        assert replay.patch_applications == 0


def test_overlapping_node_patches_fail_closed_without_parent_candidate(
    tmp_path: Path,
) -> None:
    repository, head = _repository(tmp_path)
    artifacts = AtomicArtifactStore(tmp_path / "conflict-artifacts")
    manager = GitWorkspaceManager(tmp_path / "conflict-workspaces", artifacts)
    patch_a = _node_patch(
        manager,
        repository,
        head,
        node_id="node-a",
        path="shared.txt",
        content="node-a-value\n",
    )
    patch_b = _node_patch(
        manager,
        repository,
        head,
        node_id="node-b",
        path="shared.txt",
        content="node-b-value\n",
    )

    with SQLiteStore(tmp_path / "conflict.db") as store:
        composer = GraphPatchComposer(store, manager, artifacts, _allow)
        record = composer.compose(
            _request(_accepted_graph(), repository, head, (patch_a, patch_b)),
            NeverCancelled(),
        )

        assert record.status == "failed"
        assert record.failure is not None
        assert record.failure.code is StableFailureCode.WORKSPACE_CONFLICT
        assert "overlap" in record.failure.message
        assert record.candidate_patch is None
        assert record.composition_workspace is None
        assert (
            store.list_records(
                "artifact_descriptor_v2",
                type(patch_a.patch),
                run_id="composition-run",
            )
            == ()
        )
        assert composer.replay(record.id).record == record
        assert (repository / "shared.txt").read_text() == "shared-before\n"
