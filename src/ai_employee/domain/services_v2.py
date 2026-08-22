"""Public dependency-inversion contracts for v2 mediated services."""

from __future__ import annotations

from typing import BinaryIO, Literal, Protocol

from .base import Digest, Identifier
from .v2 import (
    ActionProposal,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    ArtifactPutRequest,
    DownloadRequest,
    DownloadResult,
    EditIntentRequest,
    ExecutionResult,
    InstallRequest,
    InstallResult,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
    WorkspaceRequest,
    WorkspaceSnapshot,
)


class Cancellation(Protocol):
    def cancelled(self) -> bool: ...


class MediatedActionChannel(Protocol):
    """The only path by which a worker may submit executable proposals."""

    def submit(self, proposal: ActionProposal) -> PolicyDecision: ...


class WorkspaceManager(Protocol):
    def create(self, request: WorkspaceRequest) -> WorkspaceSnapshot: ...

    def adopt(self, snapshot: WorkspaceSnapshot) -> None: ...

    def capture_diff(self, snapshot: WorkspaceSnapshot) -> ArtifactDescriptor: ...

    def apply_edit(
        self,
        snapshot: WorkspaceSnapshot,
        request: EditIntentRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> ExecutionResult: ...

    def promote(
        self,
        snapshot: WorkspaceSnapshot,
        reviewed_patch: ArtifactDescriptor,
        approval: ApprovalRecord,
    ) -> PromotionRecord: ...


class ProcessExecutor(Protocol):
    def execute(
        self,
        request: ProcessRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> ExecutionResult: ...


class DownloadClient(Protocol):
    def fetch(
        self,
        request: DownloadRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> DownloadResult: ...


class Installer(Protocol):
    def install(
        self,
        request: InstallRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> InstallResult: ...


class WorkerAdapter(Protocol):
    def probe(self) -> WorkerAvailability: ...

    def propose(
        self, request: WorkerRequest, mediated_channel: MediatedActionChannel
    ) -> WorkerResult: ...


class ArtifactStore(Protocol):
    def put(self, stream: BinaryIO, request: ArtifactPutRequest) -> ArtifactDescriptor: ...

    def open_verified(self, descriptor: ArtifactDescriptor) -> BinaryIO: ...


class ApprovalService(Protocol):
    def request(self, request: ApprovalRequest, decision: PolicyDecision) -> ApprovalRecord: ...

    def decide(
        self,
        approval_id: Identifier,
        request_digest: Digest,
        decision: Literal["approved", "denied"],
    ) -> ApprovalRecord: ...

    def apply(
        self, decision: PolicyDecision, approval: ApprovalRecord
    ) -> PolicyDecision: ...
