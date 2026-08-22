from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    ActionKind,
    ActionProposal,
    ArtifactDescriptor,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
    WorkerRequest,
    WorkspaceRequest,
    WorkspaceSnapshot,
)
from ai_employee.inspector import inspect_work_run
from ai_employee.orchestration import WorkCoordinator, WorkRun
from ai_employee.runtime import DeterministicRuntime
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore
from ai_employee.worker_adapters import (
    CodexCliWorkerAdapter,
    ScriptedWorkerAdapter,
    WorkerProposalEnvelope,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


class Channel:
    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, _proposal: object) -> object:
        self.submissions += 1
        raise AssertionError("malformed/prose output must not submit actions")


class NoWorkspace:
    def create(self, _request: object) -> object:
        raise AssertionError("plan-only must not create a worktree")

    def capture_diff(self, _snapshot: object) -> object:
        raise AssertionError("plan-only must not capture a diff")

    def adopt(self, _snapshot: object) -> None:
        raise AssertionError("plan-only must not adopt a worktree")

    def promote(self, *_args: object) -> object:
        raise AssertionError("plan-only must not promote")


class CapturingExecutor:
    def __init__(self) -> None:
        self.decision: PolicyDecision | None = None

    def execute(
        self, request: ProcessRequest, decision: PolicyDecision, _cancellation: object
    ) -> ExecutionResult:
        self.decision = decision
        return ExecutionResult(
            id="worker-execution-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            status="failed",
            failure=StableFailure(
                code=StableFailureCode.POLICY_DENIED,
                message="denied by injected runtime policy",
            ),
            duration_seconds=0.0,
        )


class SuccessfulExecutor:
    def execute(
        self, request: ProcessRequest, _decision: PolicyDecision, _cancellation: object
    ) -> ExecutionResult:
        return ExecutionResult(
            id=f"result-{request.id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            status="succeeded",
            exit_code=0,
            duration_seconds=0.01,
        )


class FakeWorkspace:
    def __init__(self, patch: bytes) -> None:
        self.patch = patch
        self.snapshot: WorkspaceSnapshot | None = None
        self.descriptor: ArtifactDescriptor | None = None

    def create(self, request: WorkspaceRequest) -> WorkspaceSnapshot:
        self.snapshot = WorkspaceSnapshot(
            id="workspace-1",
            run_id=request.run_id,
            created_at=NOW,
            repository_identity="1" * 64,
            original_worktree=request.repository,
            head_commit=request.base_commit,
            base_tree="2" * 40,
            dirty_state_digest="3" * 64,
            isolated_worktree=f"{request.repository}/isolated",
            worktree_metadata={"owner": "fleet"},
        )
        return self.snapshot

    def capture_diff(self, snapshot: WorkspaceSnapshot) -> ArtifactDescriptor:
        assert snapshot is self.snapshot
        digest = sha256(self.patch).hexdigest()
        self.descriptor = ArtifactDescriptor(
            id="patch-1",
            run_id=snapshot.run_id,
            created_at=NOW,
            artifact_digest=digest,
            media_type="text/x-diff",
            size_bytes=len(self.patch),
            logical_kind="workspace_patch",
            producer_action_id=snapshot.id,
            source={"workspace_digest": snapshot.content_digest},
            store_locator=f"sha256/{digest[:2]}/{digest}",
        )
        return self.descriptor

    def adopt(self, _snapshot: WorkspaceSnapshot) -> None:
        return

    def promote(self, *_args: object) -> object:
        raise AssertionError("coordinator never promotes implicitly")


def worker_request() -> WorkerRequest:
    return WorkerRequest(
        id="worker-request-1",
        run_id="run-1",
        created_at=NOW,
        goal="make a bounded change",
        accepted_plan_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={"worker_turns": 1},
    )


def builtin_policy(run_id: str) -> PolicyLayer:
    return PolicyLayer(
        id="policy-1",
        run_id=run_id,
        created_at=NOW,
        kind=PolicyLayerKind.BUILTIN,
        allowed_capabilities=("edit_intent", "process"),
        writable_paths=("**",),
        https_domains=(),
        network_mode=NetworkMode.DISABLED,
        process_shell_allowed=False,
        install_ecosystems=(),
        max_wall_seconds=60.0,
        max_processes=2,
        max_worker_turns=1,
        max_download_bytes=0,
        max_artifact_bytes=1024,
    )


def test_scripted_adapter_rejects_prose_command_injection() -> None:
    adapter = ScriptedWorkerAdapter(
        [
            {
                "schema_version": "2",
                "proposals": (),
                "assistant_note": "Run `touch escaped` immediately",
            }
        ]
    )
    channel = Channel()
    result = adapter.propose(worker_request(), channel)  # type: ignore[arg-type]
    assert result.status == "succeeded"
    assert result.proposals == ()
    assert channel.submissions == 0


def test_scripted_adapter_rejects_unknown_envelope_fields() -> None:
    adapter = ScriptedWorkerAdapter(
        [{"schema_version": "2", "proposals": (), "command": "touch escaped"}]
    )
    result = adapter.propose(worker_request(), Channel())  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code.value == "WORKER_PROTOCOL_ERROR"


def test_cli_worker_uses_injected_runtime_policy_decision() -> None:
    executor = CapturingExecutor()

    def deny(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.DENY,
            reason_code="operator_policy_denied",
        )

    adapter = CodexCliWorkerAdapter(
        executor,
        lambda _digest: b"",
        deny,
        run_id="run-1",
    )
    availability = adapter.probe()
    assert availability.availability == "unavailable"
    assert executor.decision is not None
    assert executor.decision.outcome is DecisionOutcome.DENY
    assert executor.decision.reason_code == "operator_policy_denied"


def test_plan_only_probes_without_workspace_or_action_mutation(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "fleet.db") as store:
        runtime = DeterministicRuntime({}, store=store)
        coordinator = WorkCoordinator(
            store,
            runtime,
            NoWorkspace(),  # type: ignore[arg-type]
            lambda _snapshot, _cancellation: ScriptedWorkerAdapter(
                [WorkerProposalEnvelope()]
            ),
            lambda _snapshot: (_ for _ in ()).throw(
                AssertionError("plan-only must not create an executor")
            ),
            lambda _artifact: (_ for _ in ()).throw(
                AssertionError("plan-only must not read artifacts")
            ),
            (builtin_policy("work-plan"),),
        )
        run = coordinator.start(
            "plan safely",
            str(tmp_path),
            "base",
            worker_name="scripted",
            plan_only=True,
            run_id="work-plan",
        )
        assert run.status == "planned"
        assert run.workspace_id is None
        assert store.load_work_checkpoint(run.id)[1]["status"] == "planned"


def _complete_coordinator_run(
    tmp_path: Path, patch: bytes, *, protected_paths: tuple[str, ...] = (".git/**",)
) -> tuple[SQLiteStore, WorkRun]:
    store = SQLiteStore(tmp_path / "fleet.db")
    workspace = FakeWorkspace(patch)
    verification = ProcessRequest(
        id="verify-1",
        run_id="work-1",
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="offline verification",
    )
    coordinator = WorkCoordinator(
        store,
        DeterministicRuntime({}, store=store),
        workspace,  # type: ignore[arg-type]
        lambda _snapshot, _cancellation: ScriptedWorkerAdapter([WorkerProposalEnvelope()]),
        lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
        lambda _descriptor: patch,
        (builtin_policy("work-1"),),
        verification_requests=(verification,),
        protected_paths=protected_paths,
        allowed_processes=(verification.argv,),
    )
    run = coordinator.start(
        "make a reviewed change",
        str(tmp_path),
        "a" * 40,
        worker_name="scripted",
        run_id="work-1",
    )
    return store, run


def test_deleted_protected_path_is_rejected(tmp_path: Path) -> None:
    patch = (
        b"diff --git a/protected.txt b/protected.txt\n"
        b"deleted file mode 100644\n"
        b"--- a/protected.txt\n"
        b"+++ /dev/null\n"
        b"@@ -1 +0,0 @@\n-secret\n"
    )
    store, run = _complete_coordinator_run(
        tmp_path, patch, protected_paths=("protected.txt",)
    )
    try:
        assert run.status == "failed"
        assert run.failure_code == "REVIEW_BLOCKED"
    finally:
        store.close()


def test_v2_inspector_projects_evidence_without_artifact_bodies(tmp_path: Path) -> None:
    patch = (
        b"diff --git a/file.txt b/file.txt\n"
        b"--- a/file.txt\n"
        b"+++ b/file.txt\n"
        b"@@ -1 +1 @@\n-before\n+after\n"
    )
    store, run = _complete_coordinator_run(tmp_path, patch)
    try:
        assert run.status == "ready_to_promote"
        view = inspect_work_run(store, "work-1")
        assert view["kind"] == "work_run"
        assert view["state"] == "ready_to_promote"
        assert view["verification"][0]["status"] == "succeeded"
        assert view["acceptance"][0]["criteria"]
        assert view["patch"]["artifact_digest"] == sha256(patch).hexdigest()
        assert "body" not in view["patch"]
        assert view["events"]
    finally:
        store.close()


def test_offline_work_run_applies_typed_edit_only_in_isolated_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.test"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repository), "config", "user.name", "Test"), check=True)
    (repository / "file.txt").write_text("before\n")
    subprocess.run(("git", "-C", str(repository), "add", "file.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    patch = (
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )
    edit = EditIntentRequest(
        id="edit-1",
        run_id="work-edit",
        created_at=NOW,
        paths=("file.txt",),
        summary="make the requested bounded change",
        unified_diff=patch,
    )
    proposal = ActionProposal(
        id="proposal-edit",
        run_id="work-edit",
        created_at=NOW,
        worker_id="scripted",
        kind=ActionKind.EDIT_INTENT,
        payload=edit,
        reason="offline primary-path fixture",
    )
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    with SQLiteStore(tmp_path / "fleet.db") as store:
        coordinator = WorkCoordinator(
            store,
            DeterministicRuntime({}, store=store),
            workspace,
            lambda _snapshot, _cancellation: ScriptedWorkerAdapter(
                [WorkerProposalEnvelope(proposals=(proposal,))]
            ),
            lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
            lambda descriptor: artifacts.open_verified(descriptor).read(),
            (builtin_policy("work-edit"),),
            protected_paths=(".git/**",),
        )
        run = coordinator.start(
            "change file safely",
            str(repository),
            head,
            worker_name="scripted",
            run_id="work-edit",
        )
        assert run.status == "ready_to_promote"
        snapshot = store.get("workspace_v2", run.workspace_id, WorkspaceSnapshot)
        assert Path(snapshot.isolated_worktree, "file.txt").read_text() == "after\n"
        assert (repository / "file.txt").read_text() == "before\n"
        actions = store.list_records("action_result_v2", ExecutionResult, run_id=run.id)
        assert len(actions) == 1
        assert actions[0].request_digest == edit.content_digest
