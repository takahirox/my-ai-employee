"""Accepted predecessor state materialization using existing mediated Git edits."""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import Field

from .domain.base import Digest, Identifier, freeze_json
from .domain.services_v2 import Cancellation, WorkspacePreflightError
from .domain.v2 import (
    ActionKind,
    ActionProposal,
    ArtifactDescriptor,
    ArtifactPutRequest,
    DigestedRecordV2,
    EditIntentRequest,
    PolicyDecision,
    StableFailure,
    StableFailureCode,
    WorkerRequest,
    WorkerResult,
    WorkspaceRequest,
    WorkspaceSnapshot,
)
from .graph_composition import NodePatchArtifact
from .serialization import canonical_digest
from .services_v2._common import identifier, now
from .services_v2.workspace import GitWorkspaceManager
from .storage import SQLiteStore

if TYPE_CHECKING:
    from .domain import Graph
    from .orchestration import WorkCoordinator


class WorkspaceInputRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "workspace_input"
    workspace_digest: Digest
    worker_request_digest: Digest
    accepted_graph_revision_digest: Digest
    node_id: Identifier
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    original_base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    input_tree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    input_patch_digest: Digest
    sources: tuple[NodePatchArtifact, ...] = Field(min_length=1, max_length=64)


class WorkspaceOutputRecord(DigestedRecordV2):
    schema_name: ClassVar[str] = "workspace_output"
    input_record_digest: Digest
    workspace_digest: Digest
    cumulative_patch_digest: Digest
    output_tree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    incremental_patch: ArtifactDescriptor


def input_record(store: SQLiteStore, snapshot: WorkspaceSnapshot) -> WorkspaceInputRecord | None:
    try:
        result = store.get("workspace_input_v2", "input-" + snapshot.id, WorkspaceInputRecord)
    except KeyError:
        return None
    if result.workspace_digest != snapshot.content_digest:
        raise ValueError("workspace input lineage has a foreign snapshot")
    return result


def output_record(
    store: SQLiteStore,
    snapshot: WorkspaceSnapshot,
    patch_digest: str,
) -> WorkspaceOutputRecord:
    result = store.get(
        "workspace_output_v2",
        "output-" + canonical_digest((snapshot.id, patch_digest)),
        WorkspaceOutputRecord,
    )
    source = input_record(store, snapshot)
    if source is None or (
        result.input_record_digest != source.content_digest
        or result.workspace_digest != snapshot.content_digest
        or result.cumulative_patch_digest != patch_digest
    ):
        raise ValueError("workspace output lineage has stale input or output bindings")
    return result


def _body(manager: GitWorkspaceManager, descriptor: ArtifactDescriptor) -> bytes:
    with manager.artifacts.open_verified(descriptor) as stream:
        return stream.read()


def _patch_tree(
    manager: GitWorkspaceManager,
    snapshot: WorkspaceSnapshot,
    patch: bytes,
    *,
    base: str | None = None,
) -> str:
    handle, filename = tempfile.mkstemp(prefix="lineage-index-", dir=manager.state_root)
    os.close(handle)
    index = Path(filename)
    index.unlink()
    environment = {**os.environ, "GIT_INDEX_FILE": filename}
    root = snapshot.isolated_worktree
    try:
        for args, data in (
            (("read-tree", base or snapshot.base_tree), None),
            (
                (
                    "apply",
                    "--cached",
                    "--binary",
                    "--recount",
                    "--unidiff-zero",
                    "--whitespace=nowarn",
                    "-",
                ),
                patch,
            ),
            (("write-tree",), None),
        ):
            if args[0] == "apply" and not patch:
                continue
            result = subprocess.run(
                ("git", "-C", root, *args),
                input=data,
                env=environment,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise ValueError("candidate tree reconstruction rejected the recorded patch")
        return result.stdout.decode().strip()
    finally:
        index.unlink(missing_ok=True)


class DependentWorkspaceManager(GitWorkspaceManager):
    def __init__(
        self,
        original: GitWorkspaceManager,
        store: SQLiteStore,
        worker_request: WorkerRequest,
        sources: tuple[NodePatchArtifact, ...],
        decide: Callable[[ActionProposal], PolicyDecision],
        generated_paths: tuple[str, ...],
        cancellation: Cancellation,
    ) -> None:
        super().__init__(original.state_root, original.artifacts)
        self.store, self.worker_request, self.sources = store, worker_request, sources
        self.decide, self.generated_paths, self.cancellation = decide, generated_paths, cancellation

    def create(self, request: WorkspaceRequest) -> WorkspaceSnapshot:
        from .orchestration import bind_service_decision

        snapshot = super().create(request)
        worker = self.worker_request
        try:
            flattened = {source.node_id: source for source in self.sources}
            covered: set[str] = set()
            for source in self.sources:
                previous = input_record(self.store, source.workspace)
                if previous is not None:
                    for ancestor in previous.sources:
                        if (
                            ancestor.node_id in flattened
                            and flattened[ancestor.node_id] != ancestor
                        ):
                            raise ValueError("predecessors have conflicting accepted ancestry")
                        flattened[ancestor.node_id] = ancestor
                        covered.add(ancestor.node_id)
            frontier = tuple(source for source in self.sources if source.node_id not in covered)
            paths_seen: set[str] = set()
            for source in frontier:
                if (
                    source.workspace.head_commit != snapshot.head_commit
                    or source.workspace.repository_identity != snapshot.repository_identity
                    or source.accepted_graph_revision_digest
                    != worker.accepted_graph_revision_digest
                    or source.graph_run_id != worker.graph_run_id
                    or source.generation > worker.generation
                ):
                    raise ValueError(
                        "predecessor code state is foreign, stale or uses another base"
                    )
                self.adopt(source.workspace)
                current = super().capture_diff(
                    source.workspace,
                    generated_paths=self.generated_paths,
                    harness_digest=worker.harness_digest,
                )
                if current.artifact_digest != source.patch.artifact_digest:
                    raise ValueError("predecessor workspace changed after acceptance")
                body = _body(self, source.patch)
                paths = tuple(sorted(self._patch_paths(body.decode())))
                if paths_seen.intersection(paths):
                    raise ValueError(
                        "dependent input branches overlap; explicit integration is required"
                    )
                paths_seen.update(paths)
                edit = EditIntentRequest(
                    id=identifier("predecessor-edit"),
                    run_id=request.run_id,
                    created_at=now(),
                    paths=paths,
                    summary="materialize accepted predecessor state",
                    unified_diff=body.decode(),
                )
                proposal = ActionProposal(
                    id=identifier("predecessor-proposal"),
                    run_id=request.run_id,
                    created_at=now(),
                    worker_id="runtime-lineage",
                    kind=ActionKind.EDIT_INTENT,
                    payload=edit,
                    reason="exact accepted dependency input, not promotion",
                )
                decision = bind_service_decision(edit, self.decide(proposal))
                if decision.effective_policy_digest != worker.effective_policy_digest:
                    raise ValueError("predecessor materialization policy is stale")
                result = super().apply_edit(snapshot, edit, decision, self.cancellation)
                self.store.put("edit_intent_request_v2", edit, run_id=request.run_id)
                self.store.put("policy_decision_v2", decision, run_id=request.run_id)
                self.store.put("execution_result_v2", result, run_id=request.run_id)
                if result.status != "succeeded":
                    raise ValueError(
                        "predecessor materialization was rejected by existing edit policy"
                    )
            patch = super().capture_diff(
                snapshot, generated_paths=self.generated_paths, harness_digest=worker.harness_digest
            )
            record = WorkspaceInputRecord(
                id="input-" + snapshot.id,
                run_id=worker.graph_run_id or worker.run_id,
                created_at=now(),
                workspace_digest=snapshot.content_digest or "",
                worker_request_digest=worker.content_digest or "",
                accepted_graph_revision_digest=worker.accepted_graph_revision_digest or "",
                node_id=worker.node_id or "",
                generation=worker.generation,
                attempt=worker.attempt,
                original_base_commit=snapshot.head_commit,
                input_tree=_patch_tree(self, snapshot, _body(self, patch)),
                input_patch_digest=patch.artifact_digest,
                sources=tuple(flattened[key] for key in sorted(flattened)),
            )
            self.store.put_once("workspace_input_v2", record, run_id=record.run_id)
            return snapshot
        except (KeyError, ValueError, OSError) as error:
            raise WorkspacePreflightError(
                StableFailure(code=StableFailureCode.WORKSPACE_CONFLICT, message=str(error))
            ) from error

    def capture_diff(
        self,
        snapshot: WorkspaceSnapshot,
        *,
        generated_paths: tuple[str, ...] = (),
        harness_digest: Digest | None = None,
    ) -> ArtifactDescriptor:
        patch = super().capture_diff(
            snapshot, generated_paths=generated_paths, harness_digest=harness_digest
        )
        source = input_record(self.store, snapshot)
        if source is None:
            raise ValueError("dependent workspace is missing its immutable input lineage")
        if source.worker_request_digest != self.worker_request.content_digest:
            raise ValueError("dependent workspace belongs to another worker attempt")
        incremental = self._diff(
            Path(snapshot.isolated_worktree), snapshot.id, generated_paths, base=source.input_tree
        )
        descriptor = self.artifacts.put(
            io.BytesIO(incremental),
            ArtifactPutRequest(
                id=identifier("incremental-patch"),
                run_id=snapshot.run_id,
                created_at=now(),
                media_type="text/x-diff",
                logical_kind="workspace_incremental_patch",
                producer_action_id=snapshot.id,
                source=freeze_json(
                    {
                        "input_record_digest": source.content_digest,
                        "input_tree": source.input_tree,
                        "cumulative_patch_digest": patch.artifact_digest,
                    }
                ),
            ),
        )
        record = WorkspaceOutputRecord(
            id="output-" + canonical_digest((snapshot.id, patch.artifact_digest)),
            run_id=source.run_id,
            created_at=now(),
            input_record_digest=source.content_digest or "",
            workspace_digest=snapshot.content_digest or "",
            cumulative_patch_digest=patch.artifact_digest,
            output_tree=_patch_tree(self, snapshot, _body(self, patch)),
            incremental_patch=descriptor,
        )
        self.store.put_once("workspace_output_v2", record, run_id=source.run_id)
        self.store.put("artifact_descriptor_v2", descriptor, run_id=snapshot.run_id)
        return patch


def materialize_node_inputs(coordinator: WorkCoordinator, request: WorkerRequest) -> None:
    from .task_orchestration import NodePatchRecord

    if not request.predecessor_outputs:
        return
    store = coordinator.store
    candidates = store.list_records("node_patch_v2", NodePatchRecord, run_id=request.graph_run_id)
    sources: dict[str, NodePatchArtifact] = {}

    def add_source(source: NodePatchArtifact) -> None:
        if source.node_id in sources and sources[source.node_id] != source:
            raise ValueError("predecessors have conflicting accepted ancestry")
        sources[source.node_id] = source

    for predecessor in request.predecessor_outputs:
        patches = [
            item.node_patch
            for item in candidates
            if item.node_patch.node_id == predecessor.node_id
            and item.node_patch.worker_result_digest == predecessor.worker_result_digest
            and item.node_patch.accepted_graph_revision_digest
            == predecessor.accepted_graph_revision_digest
            and item.node_patch.generation == predecessor.result_generation
            and item.node_patch.attempt == predecessor.attempt
        ]
        references = tuple(
            item
            for item in predecessor.artifact_descriptors
            if item.logical_kind == "workspace_patch"
        )
        if references:
            if (
                len(patches) != 1
                or len(references) != 1
                or patches[0].patch.id != references[0].descriptor_id
                or patches[0].patch.content_digest != references[0].descriptor_digest
                or patches[0].patch.artifact_digest != references[0].artifact_digest
            ):
                raise ValueError("accepted predecessor lacks an exact code artifact binding")
            add_source(patches[0])
        else:
            if predecessor.worker_result_id is None:
                raise ValueError("predecessor result identity is missing")
            result = store.get("worker_result_v2", predecessor.worker_result_id, WorkerResult)
            if result.content_digest != predecessor.worker_result_digest:
                raise ValueError("predecessor result identity is stale")
            run = store.get_work_run(result.run_id)
            if run.workspace_id is not None:
                snapshot = store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
                inherited = input_record(store, snapshot)
                if inherited is not None:
                    if inherited.worker_request_digest != result.request_digest:
                        raise ValueError("non-writing predecessor input is stale")
                    for item in inherited.sources:
                        add_source(item)
    if not sources:
        return
    if not isinstance(coordinator.workspace, GitWorkspaceManager):
        raise ValueError("dependent code inputs require the supported Git workspace backend")

    class Cancellation:
        def cancelled(self) -> bool:
            return any(
                store.control(run_id) == "cancel"
                for run_id in (
                    request.graph_run_id or request.run_id,
                    request.run_id,
                )
            )

    coordinator.workspace = DependentWorkspaceManager(
        coordinator.workspace,
        store,
        request,
        tuple(sources[key] for key in sorted(sources)),
        coordinator._decide,
        coordinator.generated_paths,
        Cancellation(),
    )


def composition_increment(
    store: SQLiteStore,
    manager: GitWorkspaceManager,
    graph: Graph,
    node_patch: NodePatchArtifact,
    patches: Mapping[str, NodePatchArtifact],
) -> tuple[bytes, frozenset[str]] | None:
    source = input_record(store, node_patch.workspace)
    if source is None:
        return None
    if (
        source.worker_request_digest != node_patch.worker_request_digest
        or source.node_id != node_patch.node_id
        or source.generation != node_patch.generation
        or source.attempt != node_patch.attempt
        or source.accepted_graph_revision_digest != node_patch.accepted_graph_revision_digest
        or source.run_id != node_patch.graph_run_id
    ):
        raise ValueError("composition input lineage is stale or foreign")
    inbound = {
        node.id: {edge.source_id for edge in graph.edges if edge.target_id == node.id}
        for node in graph.nodes
    }

    def ancestors(node_id: str) -> set[str]:
        result: set[str] = set()
        pending = list(inbound[node_id])
        while pending:
            current = pending.pop()
            if current not in result:
                result.add(current)
                pending.extend(inbound[current])
        return result

    expected = {ancestor for ancestor in ancestors(node_patch.node_id) if ancestor in patches}
    by_id = {item.node_id: item for item in source.sources}
    if set(by_id) != expected or len(by_id) != len(source.sources):
        raise ValueError("composition lineage does not contain the exact writing ancestors")
    for node_id, item in by_id.items():
        if item != patches[node_id]:
            raise ValueError("upstream output changed since dependent execution")
    covered = set().union(*(ancestors(node_id) for node_id in by_id))
    frontier = tuple(by_id[key] for key in sorted(set(by_id) - covered))
    body = b"".join(_body(manager, item.patch) for item in frontier)
    if _patch_tree(manager, node_patch.workspace, body) != source.input_tree:
        raise ValueError("composition input tree is not the exact predecessor state")
    output = output_record(store, node_patch.workspace, node_patch.patch.artifact_digest)
    incremental = _body(manager, output.incremental_patch)
    if (
        output.incremental_patch.run_id != node_patch.workspace.run_id
        or output.incremental_patch.producer_action_id != node_patch.workspace.id
        or output.incremental_patch.logical_kind != "workspace_incremental_patch"
        or _patch_tree(manager, node_patch.workspace, incremental, base=source.input_tree)
        != output.output_tree
        or _patch_tree(manager, node_patch.workspace, _body(manager, node_patch.patch))
        != output.output_tree
    ):
        raise ValueError("incremental patch does not reconstruct the accepted output state")
    return incremental, frozenset(by_id)
