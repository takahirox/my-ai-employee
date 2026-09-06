from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ai_employee.domain import AcceptedGraphRevision, Budget, Edge, Graph
from ai_employee.domain.services_v2 import WorkspacePreflightError
from ai_employee.domain.v2 import DecisionOutcome, WorkerRequest, WorkspaceRequest
from ai_employee.graph_composition import GraphPatchComposer, NodePatchArtifact
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore
from ai_employee.workspace_lineage import (
    DependentWorkspaceManager,
    WorkspaceInputRecord,
    WorkspaceOutputRecord,
    input_record,
    output_record,
)
from tests.test_graph_composition import (
    GENERATED_PATHS,
    HARNESS_DIGEST,
    NOW,
    POLICY_DIGEST,
    NeverCancelled,
    _allow,
    _node,
    _node_patch,
    _repository,
    _request,
)
from tests.test_work_orchestration_v2 import worker_request


def accepted_chain():
    return AcceptedGraphRevision(
        revision_number=1,
        graph=Graph(
            id="serial",
            nodes=tuple(_node(name) for name in ("a", "b", "c")),
            edges=(
                Edge(id="a-b", source_id="a", target_id="b"),
                Edge(id="b-c", source_id="b", target_id="c"),
            ),
            entry_node_ids=("a",),
            terminal_node_ids=("c",),
            budget=Budget(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
        ),
    )


def dependent(
    manager, store, accepted, parent, repository, head, name, *, deny=False, cancel=False
):
    request = WorkerRequest.model_validate(
        {
            **worker_request().model_dump(mode="python", exclude={"content_digest"}),
            "run_id": "worker-" + name,
            "node_id": name,
            "graph_run_id": "composition-run",
            "accepted_graph_revision_digest": accepted.content_digest,
            "accepted_plan_digest": accepted.content_digest,
            "harness_digest": HARNESS_DIGEST,
            "effective_policy_digest": POLICY_DIGEST,
        }
    )

    def decide(proposal):
        decision = _allow(proposal.payload)
        return decision.model_copy(update={"outcome": DecisionOutcome.DENY}) if deny else decision

    class Cancellation:
        def cancelled(self):
            return cancel

    seeded = DependentWorkspaceManager(
        manager, store, request, (parent,), decide, GENERATED_PATHS, Cancellation()
    )
    snapshot = seeded.create(
        WorkspaceRequest(
            id="workspace-" + name,
            run_id=request.run_id,
            created_at=NOW,
            repository=str(repository),
            base_commit=head,
        )
    )
    return seeded, snapshot, request


def accepted_patch(manager, snapshot, request, name):
    return NodePatchArtifact(
        node_id=name,
        graph_run_id=request.graph_run_id,
        accepted_graph_revision_digest=request.accepted_graph_revision_digest,
        generation=request.generation,
        attempt=request.attempt,
        worker_request_digest=request.content_digest,
        worker_result_digest=name * 64,
        acceptance_ledger_digest="9" * 64,
        workspace=snapshot,
        patch=manager.capture_diff(
            snapshot, generated_paths=GENERATED_PATHS, harness_digest=HARNESS_DIGEST
        ),
    )


@pytest.mark.parametrize("same_file", [False, True])
def test_serial_inputs_incremental_composition_and_read_only_replay(
    tmp_path, monkeypatch, same_file
):
    repository, head = _repository(tmp_path)
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    manager = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    accepted = accepted_chain()
    parent = _node_patch(
        manager,
        repository,
        head,
        node_id="a",
        path="api.py",
        content="def answer():\n    return 41\n",
    ).model_copy(update={"accepted_graph_revision_digest": accepted.content_digest})
    with SQLiteStore(tmp_path / "fleet.db") as store:
        second, second_snapshot, request_b = dependent(
            manager, store, accepted, parent, repository, head, "b"
        )
        root_b = Path(second_snapshot.isolated_worktree)
        assert (root_b / "api.py").read_text() == "def answer():\n    return 41\n"
        subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import runpy; assert runpy.run_path('api.py')['answer']() == 41",
            ),
            cwd=root_b,
            check=True,
        )
        if same_file:
            (root_b / "api.py").write_text("def answer():\n    return 42\n")
        else:
            (root_b / "consumer.py").write_text("from api import answer\nRESULT = answer() + 1\n")
        patch_b = accepted_patch(second, second_snapshot, request_b, "b")
        lineage_b = input_record(store, second_snapshot)
        output_b = output_record(store, second_snapshot, patch_b.patch.artifact_digest)
        incremental = artifacts.open_verified(output_b.incremental_patch).read().decode()
        assert "return 42" in incremental if same_file else "def answer" not in incremental
        assert lineage_b.input_tree != output_b.output_tree
        # Adoption after restart retains the original input; repeated capture is idempotent.
        second.adopt(second_snapshot)
        assert (
            second.capture_diff(
                second_snapshot, generated_paths=GENERATED_PATHS, harness_digest=HARNESS_DIGEST
            ).artifact_digest
            == patch_b.patch.artifact_digest
        )
        assert output_record(store, second_snapshot, patch_b.patch.artifact_digest) == output_b
        third, third_snapshot, request_c = dependent(
            manager, store, accepted, patch_b, repository, head, "c"
        )
        root_c = Path(third_snapshot.isolated_worktree)
        assert (root_c / "api.py").read_text() == (root_b / "api.py").read_text()
        if same_file:
            (root_c / "api.py").write_text("def answer():\n    return 43\n")
        else:
            assert (root_c / "consumer.py").read_text() == (root_b / "consumer.py").read_text()
            (root_c / "consumer.py").write_text("from api import answer\nRESULT = answer() + 2\n")
        patch_c = accepted_patch(third, third_snapshot, request_c, "c")
        assert {item.node_id for item in input_record(store, third_snapshot).sources} == {"a", "b"}
        composer = GraphPatchComposer(store, manager, artifacts, _allow)
        composition = composer.compose(
            _request(accepted, repository, head, (parent, patch_b, patch_c)), NeverCancelled()
        )
        assert composition.status == "succeeded", composition.failure
        root = Path(composition.composition_workspace.isolated_worktree)
        subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import runpy; assert runpy.run_path('api.py')['answer']() == 43"
                if same_file
                else "import sys; sys.path.insert(0, '.'); "
                "from consumer import RESULT; assert RESULT == 43",
            ),
            cwd=root,
            check=True,
        )
        final_patch = artifacts.open_verified(composition.candidate_patch).read().decode()
        assert final_patch.count("+def answer():") == 1
        assert not (repository / "api.py").exists()
        assert (
            subprocess.check_output(("git", "-C", str(repository), "status", "--porcelain")) == b""
        )
        assert tuple(binding.node_id for binding in composition.ordered_inputs) == ("a", "b", "c")
        # Matching bytes do not authorize reuse after an upstream result binding changes.
        stale = parent.model_copy(update={"worker_result_digest": "8" * 64})
        rejected = composer.compose(
            _request(accepted, repository, head, (stale, patch_b, patch_c)), NeverCancelled()
        )
        assert rejected.status == "failed" and rejected.candidate_patch is None
        original_output = output_record(store, third_snapshot, patch_c.patch.artifact_digest)
        corrupted = WorkspaceOutputRecord.model_validate(
            {
                **original_output.model_dump(mode="python", exclude={"content_digest"}),
                "input_record_digest": "0" * 64,
            }
        )
        store.put("workspace_output_v2", corrupted, run_id=original_output.run_id)
        rejected = composer.compose(
            _request(accepted, repository, head, (parent, patch_b, patch_c)), NeverCancelled()
        )
        assert rejected.status == "failed" and rejected.candidate_patch is None
        store.put("workspace_output_v2", original_output, run_id=original_output.run_id)
        monkeypatch.setattr(manager, "adopt", lambda *_: pytest.fail("replay mutated a workspace"))
        monkeypatch.setattr(manager, "apply_edit", lambda *_: pytest.fail("replay applied a patch"))
        assert composer.replay(composition.id).record == composition
    with SQLiteStore(tmp_path / "fleet.db") as store:
        assert (
            len(
                store.list_records(
                    "workspace_input_v2", WorkspaceInputRecord, run_id="composition-run"
                )
            )
            == 2
        )
        assert (
            len(
                store.list_records(
                    "workspace_output_v2", WorkspaceOutputRecord, run_id="composition-run"
                )
            )
            == 2
        )


@pytest.mark.parametrize("failure", ["policy", "stale_workspace", "foreign_generation", "cancel"])
def test_input_materialization_fails_closed(tmp_path, failure):
    repository, head = _repository(tmp_path)
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    manager = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    accepted = accepted_chain()
    parent = _node_patch(
        manager, repository, head, node_id="a", path="a.txt", content="accepted\n"
    ).model_copy(update={"accepted_graph_revision_digest": accepted.content_digest})
    if failure == "stale_workspace":
        Path(parent.workspace.isolated_worktree, "a.txt").write_text("unaccepted\n")
    if failure == "foreign_generation":
        parent = parent.model_copy(update={"generation": 1})
    with SQLiteStore(tmp_path / "fleet.db") as store:
        with pytest.raises(WorkspacePreflightError):
            dependent(
                manager,
                store,
                accepted,
                parent,
                repository,
                head,
                "b",
                deny=failure == "policy",
                cancel=failure == "cancel",
            )
        assert store.list_records("workspace_input_v2", WorkspaceInputRecord) == ()
    assert (repository / "a.txt").read_text() == "a-before\n"
