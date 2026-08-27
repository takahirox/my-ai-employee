"""Deterministic composition of accepted-graph node patch artifacts."""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, Self

from pydantic import ConfigDict, Field, model_validator
from pydantic.main import BaseModel

from .domain.base import Digest, Identifier
from .domain.models import AcceptedGraphRevision, Graph
from .domain.services_v2 import ArtifactStore, Cancellation, WorkspaceManager
from .domain.v2 import (
    ArtifactDescriptor,
    DigestedRecordV2,
    EditIntentRequest,
    PolicyDecision,
    StableFailure,
    StableFailureCode,
    WorkspaceRequest,
    WorkspaceSnapshot,
)
from .services_v2._common import identifier, now
from .storage import SQLiteStore

PolicyDecider = Callable[[EditIntentRequest], PolicyDecision]


class NodePatchArtifact(BaseModel):
    """One node's verified patch and the isolated workspace that produced it."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    node_id: Identifier
    graph_run_id: Identifier
    accepted_graph_revision_digest: Digest
    generation: int = Field(ge=0)
    attempt: int = Field(ge=0)
    worker_request_digest: Digest
    worker_result_digest: Digest
    acceptance_ledger_digest: Digest
    verification_result_digests: tuple[Digest, ...] = ()
    workspace: WorkspaceSnapshot
    patch: ArtifactDescriptor

    @model_validator(mode="after")
    def _node_run_is_internally_bound(self) -> Self:
        if self.workspace.run_id != self.patch.run_id:
            raise ValueError("node workspace and patch must belong to one inner run")
        if self.patch.producer_action_id != self.workspace.id:
            raise ValueError("node patch must be produced by its bound workspace")
        return self


class GraphPatchCompositionRequest(DigestedRecordV2):
    """All authority and artifacts required for one bounded composition attempt."""

    schema_name: ClassVar[str] = "graph_patch_composition_request"
    accepted_revision: AcceptedGraphRevision
    repository: str = Field(min_length=1, max_length=4_096)
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    effective_policy_digest: Digest
    node_patches: tuple[NodePatchArtifact, ...] = Field(min_length=1)


class CompositionInputBinding(BaseModel):
    """Exact node, workspace, artifact, and mediated-edit facts used in order."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    node_id: Identifier
    node_run_id: Identifier
    worker_request_digest: Digest
    worker_result_digest: Digest
    acceptance_ledger_digest: Digest
    verification_result_digests: tuple[Digest, ...]
    workspace_id: Identifier
    workspace_digest: Digest
    patch_artifact_id: Identifier
    patch_descriptor_digest: Digest
    patch_digest: Digest
    paths: tuple[str, ...] = Field(min_length=1)
    edit_request_digest: Digest
    edit_result_digest: Digest


class GraphPatchCompositionRecord(DigestedRecordV2):
    """Persisted outcome; the request digest binds every supplied input."""

    schema_name: ClassVar[str] = "graph_patch_composition_record"
    request_digest: Digest
    accepted_graph_revision_digest: Digest
    base_commit: str
    base_tree: str | None = None
    ordered_inputs: tuple[CompositionInputBinding, ...] = ()
    composition_workspace: WorkspaceSnapshot | None = None
    candidate_patch: ArtifactDescriptor | None = None
    status: Literal["succeeded", "failed"]
    failure: StableFailure | None = None

    @model_validator(mode="after")
    def _outcome_is_complete(self) -> Self:
        if self.status == "succeeded":
            if (
                self.composition_workspace is None
                or self.candidate_patch is None
                or self.failure is not None
            ):
                raise ValueError("successful composition requires a workspace and candidate")
        elif self.candidate_patch is not None or self.failure is None:
            raise ValueError("failed composition must contain no candidate and one failure")
        return self


class GraphPatchCompositionReplay(BaseModel):
    """A replay result that explicitly performs no patch applications."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    record: GraphPatchCompositionRecord
    patch_applications: Literal[0] = 0


@dataclass(frozen=True)
class _PreparedPatch:
    artifact: NodePatchArtifact
    body: str
    paths: tuple[str, ...]


class _CompositionRejected(ValueError):
    def __init__(self, code: StableFailureCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class GraphPatchComposer:
    """Compose exact node patches through existing workspace and edit boundaries."""

    def __init__(
        self,
        store: SQLiteStore,
        workspace: WorkspaceManager,
        artifacts: ArtifactStore,
        policy_decider: PolicyDecider,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.artifacts = artifacts
        self.policy_decider = policy_decider

    def compose(
        self,
        request: GraphPatchCompositionRequest,
        cancellation: Cancellation,
    ) -> GraphPatchCompositionRecord:
        """Compose once, rejecting ambiguity instead of merging or resolving it."""

        self.store.put(
            "graph_patch_composition_request_v2",
            request,
            run_id=request.run_id,
        )
        try:
            prepared = self._prepare(request)
        except _CompositionRejected as error:
            return self._failed(request, error.code, str(error))

        expected_tree = prepared[0].artifact.workspace.base_tree
        composition_workspace: WorkspaceSnapshot | None = None
        bindings: list[CompositionInputBinding] = []
        try:
            workspace_request = WorkspaceRequest(
                id=identifier("composition-workspace-request"),
                run_id=request.run_id,
                created_at=now(),
                repository=request.repository,
                base_commit=request.base_commit,
            )
            self.store.put("workspace_request_v2", workspace_request, run_id=request.run_id)
            composition_workspace = self.workspace.create(workspace_request)
            self.store.put("workspace_v2", composition_workspace, run_id=request.run_id)
            if (
                composition_workspace.head_commit != request.base_commit
                or composition_workspace.base_tree != expected_tree
            ):
                raise _CompositionRejected(
                    StableFailureCode.WORKSPACE_CONFLICT,
                    "fresh composition workspace does not match the exact node patch base",
                )

            for item in prepared:
                if cancellation.cancelled():
                    raise _CompositionRejected(
                        StableFailureCode.CANCELLED,
                        "graph patch composition was cancelled",
                    )
                edit = EditIntentRequest(
                    id=identifier("composition-edit"),
                    run_id=request.run_id,
                    created_at=now(),
                    paths=item.paths,
                    summary=f"compose exact patch for graph node {item.artifact.node_id}",
                    unified_diff=item.body,
                )
                self.store.put("edit_intent_request_v2", edit, run_id=request.run_id)
                decision = self.policy_decider(edit)
                self.store.put("policy_decision_v2", decision, run_id=request.run_id)
                if (
                    decision.run_id != request.run_id
                    or decision.request_digest != edit.content_digest
                    or decision.effective_policy_digest != request.effective_policy_digest
                ):
                    raise _CompositionRejected(
                        StableFailureCode.POLICY_DENIED,
                        "composition edit policy decision has stale or mismatched bindings",
                    )
                result = self.workspace.apply_edit(
                    composition_workspace,
                    edit,
                    decision,
                    cancellation,
                )
                self.store.put("execution_result_v2", result, run_id=request.run_id)
                if result.status != "succeeded":
                    failure = result.failure
                    raise _CompositionRejected(
                        (StableFailureCode.INVALID_REQUEST if failure is None else failure.code),
                        (
                            "node patch is not applicable to the deterministic composition"
                            if failure is None
                            else failure.message
                        ),
                    )
                bindings.append(
                    CompositionInputBinding(
                        node_id=item.artifact.node_id,
                        node_run_id=item.artifact.workspace.run_id,
                        worker_request_digest=item.artifact.worker_request_digest,
                        worker_result_digest=item.artifact.worker_result_digest,
                        acceptance_ledger_digest=item.artifact.acceptance_ledger_digest,
                        verification_result_digests=item.artifact.verification_result_digests,
                        workspace_id=item.artifact.workspace.id,
                        workspace_digest=_required_digest(item.artifact.workspace.content_digest),
                        patch_artifact_id=item.artifact.patch.id,
                        patch_descriptor_digest=_required_digest(
                            item.artifact.patch.content_digest
                        ),
                        patch_digest=item.artifact.patch.artifact_digest,
                        paths=item.paths,
                        edit_request_digest=_required_digest(edit.content_digest),
                        edit_result_digest=_required_digest(result.content_digest),
                    )
                )

            candidate = self.workspace.capture_diff(composition_workspace)
            source = candidate.source
            if (
                candidate.logical_kind != "workspace_patch"
                or candidate.media_type != "text/x-diff"
                or candidate.size_bytes == 0
                or not isinstance(source, Mapping)
                or source.get("base_tree") != composition_workspace.base_tree
                or source.get("workspace_digest") != composition_workspace.content_digest
            ):
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    "captured parent candidate is not bound to the composition workspace",
                )
            self.store.put("artifact_descriptor_v2", candidate, run_id=request.run_id)
            record = GraphPatchCompositionRecord(
                id=identifier("graph-patch-composition"),
                run_id=request.run_id,
                created_at=now(),
                request_digest=_required_digest(request.content_digest),
                accepted_graph_revision_digest=_required_digest(
                    request.accepted_revision.content_digest
                ),
                base_commit=request.base_commit,
                base_tree=composition_workspace.base_tree,
                ordered_inputs=tuple(bindings),
                composition_workspace=composition_workspace,
                candidate_patch=candidate,
                status="succeeded",
            )
            self._persist(record)
            return record
        except _CompositionRejected as error:
            return self._failed(
                request,
                error.code,
                str(error),
                base_tree=expected_tree,
                ordered_inputs=tuple(bindings),
                composition_workspace=composition_workspace,
            )
        except (OSError, ValueError) as error:
            return self._failed(
                request,
                StableFailureCode.WORKSPACE_CONFLICT,
                f"composition boundary rejected workspace state: {error}",
                base_tree=expected_tree,
                ordered_inputs=tuple(bindings),
                composition_workspace=composition_workspace,
            )

    def replay(self, composition_id: Identifier) -> GraphPatchCompositionReplay:
        """Load the exact stored outcome without adopting workspaces or applying patches."""

        record = self.store.get(
            "graph_patch_composition_v2",
            composition_id,
            GraphPatchCompositionRecord,
        )
        return GraphPatchCompositionReplay(record=record)

    def _prepare(self, request: GraphPatchCompositionRequest) -> tuple[_PreparedPatch, ...]:
        graph = request.accepted_revision.graph
        order = tuple(
            node_id
            for node_id in _topological_node_order(graph)
            if "edit_intent"
            in next(node.required_capabilities for node in graph.nodes if node.id == node_id)
        )
        expected_nodes = set(order)
        by_node: dict[str, NodePatchArtifact] = {}
        for item in request.node_patches:
            if item.node_id in by_node:
                raise _CompositionRejected(
                    StableFailureCode.INVALID_REQUEST,
                    f"duplicate node patch input: {item.node_id}",
                )
            by_node[item.node_id] = item
        supplied_nodes = set(by_node)
        if supplied_nodes != expected_nodes:
            missing = sorted(expected_nodes - supplied_nodes)
            unexpected = sorted(supplied_nodes - expected_nodes)
            raise _CompositionRejected(
                StableFailureCode.INVALID_REQUEST,
                f"node patch set mismatch; missing={missing}, unexpected={unexpected}",
            )

        seen_artifact_ids: set[str] = set()
        seen_patch_digests: set[str] = set()
        seen_workspace_ids: set[str] = set()
        seen_paths: dict[str, str] = {}
        repository_identity: str | None = None
        base_tree: str | None = None
        prepared: list[_PreparedPatch] = []
        repository = Path(request.repository).resolve()

        for node_id in order:
            item = by_node[node_id]
            snapshot = item.workspace
            patch = item.patch
            if (
                item.graph_run_id != request.run_id
                or item.accepted_graph_revision_digest != request.accepted_revision.content_digest
            ):
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    f"node patch {node_id} has stale graph bindings",
                )
            if snapshot.id in seen_workspace_ids:
                raise _CompositionRejected(
                    StableFailureCode.INVALID_REQUEST,
                    "each node patch must come from a distinct isolated workspace",
                )
            if patch.id in seen_artifact_ids or patch.artifact_digest in seen_patch_digests:
                raise _CompositionRejected(
                    StableFailureCode.INVALID_REQUEST,
                    "duplicate node patch artifact",
                )
            seen_workspace_ids.add(snapshot.id)
            seen_artifact_ids.add(patch.id)
            seen_patch_digests.add(patch.artifact_digest)

            source = patch.source
            if (
                snapshot.run_id != patch.run_id
                or snapshot.head_commit != request.base_commit
                or Path(snapshot.original_worktree).resolve() != repository
                or patch.logical_kind != "workspace_patch"
                or patch.media_type != "text/x-diff"
                or patch.redaction_state != "none"
                or patch.producer_action_id != snapshot.id
                or not isinstance(source, Mapping)
                or source.get("base_tree") != snapshot.base_tree
                or source.get("workspace_digest") != snapshot.content_digest
            ):
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    f"node patch {node_id} has stale, wrong-base, or mismatched provenance",
                )
            if repository_identity is None:
                repository_identity = snapshot.repository_identity
                base_tree = snapshot.base_tree
            elif (
                snapshot.repository_identity != repository_identity
                or snapshot.base_tree != base_tree
            ):
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    "node patches do not share one exact repository base",
                )

            try:
                self.workspace.adopt(snapshot)
                current = self.workspace.capture_diff(snapshot)
            except (OSError, ValueError) as error:
                raise _CompositionRejected(
                    StableFailureCode.WORKSPACE_CONFLICT,
                    f"node workspace {node_id} is stale or unowned: {error}",
                ) from error
            if current.artifact_digest != patch.artifact_digest:
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    f"node patch {node_id} is stale relative to its isolated workspace",
                )
            try:
                with self.artifacts.open_verified(patch) as stream:
                    body = stream.read().decode("utf-8")
            except (OSError, UnicodeDecodeError, ValueError) as error:
                raise _CompositionRejected(
                    StableFailureCode.INTEGRITY_FAILED,
                    f"node patch {node_id} failed artifact verification: {error}",
                ) from error
            paths = _patch_paths(body)
            for path in paths:
                owner = seen_paths.get(path)
                if owner is not None:
                    raise _CompositionRejected(
                        StableFailureCode.WORKSPACE_CONFLICT,
                        f"node patches {owner} and {node_id} overlap at {path}",
                    )
                seen_paths[path] = node_id
            prepared.append(_PreparedPatch(artifact=item, body=body, paths=paths))
        return tuple(prepared)

    def _failed(
        self,
        request: GraphPatchCompositionRequest,
        code: StableFailureCode,
        message: str,
        *,
        base_tree: str | None = None,
        ordered_inputs: tuple[CompositionInputBinding, ...] = (),
        composition_workspace: WorkspaceSnapshot | None = None,
    ) -> GraphPatchCompositionRecord:
        record = GraphPatchCompositionRecord(
            id=identifier("graph-patch-composition"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=_required_digest(request.content_digest),
            accepted_graph_revision_digest=_required_digest(
                request.accepted_revision.content_digest
            ),
            base_commit=request.base_commit,
            base_tree=base_tree,
            ordered_inputs=ordered_inputs,
            composition_workspace=composition_workspace,
            status="failed",
            failure=StableFailure(code=code, message=message[:2_000] or "composition failed"),
        )
        self._persist(record)
        return record

    def _persist(self, record: GraphPatchCompositionRecord) -> None:
        self.store.put(
            "graph_patch_composition_v2",
            record,
            run_id=record.run_id,
        )


def _topological_node_order(graph: Graph) -> tuple[str, ...]:
    indegree = {node.id: 0 for node in graph.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        indegree[edge.target_id] += 1
        outgoing[edge.source_id].append(edge.target_id)
    ready = [node_id for node_id, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        node_id = heapq.heappop(ready)
        ordered.append(node_id)
        for target_id in sorted(outgoing[node_id]):
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                heapq.heappush(ready, target_id)
    if len(ordered) != len(indegree):
        raise _CompositionRejected(
            StableFailureCode.INVALID_REQUEST,
            "accepted graph revision is not a dependency DAG",
        )
    return tuple(ordered)


def _patch_paths(body: str) -> tuple[str, ...]:
    if not body.startswith("diff --git ") or "\x00" in body:
        raise _CompositionRejected(
            StableFailureCode.INVALID_REQUEST,
            "node artifact is not a textual Git unified diff",
        )
    paths: set[str] = set()
    for line in body.splitlines():
        expected_prefix: str | None = None
        if line.startswith("--- "):
            expected_prefix = "a/"
        elif line.startswith("+++ "):
            expected_prefix = "b/"
        if expected_prefix is None:
            continue
        value = line[4:]
        if value == "/dev/null":
            continue
        if not value.startswith(expected_prefix):
            raise _CompositionRejected(
                StableFailureCode.INVALID_REQUEST,
                "node patch contains a non-canonical path header",
            )
        relative = value[2:]
        path = PurePosixPath(relative)
        if (
            not relative
            or relative.startswith("/")
            or "\\" in relative
            or ".." in path.parts
            or path.as_posix() != relative
        ):
            raise _CompositionRejected(
                StableFailureCode.INVALID_REQUEST,
                "node patch path escapes or is not canonical for the workspace",
            )
        paths.add(relative)
    if not paths:
        raise _CompositionRejected(
            StableFailureCode.INVALID_REQUEST,
            "node patch contains no applicable workspace paths",
        )
    return tuple(sorted(paths))


def _required_digest(value: str | None) -> str:
    if value is None:
        raise ValueError("digested composition input is missing its content digest")
    return value
