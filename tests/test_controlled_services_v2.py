from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_employee.domain.v2 import (
    ApprovalRecord,
    ApprovalRequest,
    ArtifactPutRequest,
    DecisionOutcome,
    DownloadRequest,
    EditIntentRequest,
    InstallRequest,
    PolicyDecision,
    ProcessRequest,
    WorkspaceRequest,
)
from ai_employee.services_v2 import (
    AtomicArtifactStore,
    DigestApprovalService,
    GitWorkspaceManager,
    LocalProcessExecutor,
    ProjectLocalInstaller,
    RestrictedDownloadClient,
    TransportResponse,
)
from ai_employee.storage import SQLiteStore

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


class NeverCancelled:
    def cancelled(self) -> bool:
        return False


def artifact_request(*, run_id: str = "run-1", redacted: bool = False) -> ArtifactPutRequest:
    return ArtifactPutRequest(
        id="put-1",
        run_id=run_id,
        created_at=NOW,
        media_type="text/plain",
        logical_kind="test_output",
        producer_action_id="action-1",
        source={"bounded": True},
        redacted=redacted,
    )


def allow(request_digest: str, *, reason: str = "policy_allowed") -> PolicyDecision:
    return PolicyDecision(
        id="decision-1",
        run_id="run-1",
        created_at=NOW,
        request_digest=request_digest,
        effective_policy_digest=ZERO,
        outcome=DecisionOutcome.ALLOW,
        reason_code=reason,
        limits={"max_wall_seconds": 10.0, "max_download_bytes": 1_000_000},
    )


def test_artifact_store_deduplicates_verifies_and_cleans_failed_put(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts", maximum_bytes=4)
    first = store.put(io.BytesIO(b"same"), artifact_request())
    second = store.put(io.BytesIO(b"same"), artifact_request())
    assert first.artifact_digest == second.artifact_digest
    assert store.open_verified(first).read() == b"same"
    with pytest.raises(ValueError, match="byte limit"):
        store.put(io.BytesIO(b"large"), artifact_request())
    assert not tuple((tmp_path / "artifacts" / "tmp").iterdir())
    content = tmp_path / "artifacts" / first.store_locator
    content.write_bytes(b"evil")
    with pytest.raises(OSError, match="verification"):
        store.open_verified(first)
    with pytest.raises(OSError, match="content address"):
        store.put(io.BytesIO(b"same"), artifact_request())


def test_approval_is_digest_bound_persistent_and_single_use(tmp_path: Path) -> None:
    request = ApprovalRequest(
        id="approval-request-1",
        run_id="run-1",
        created_at=NOW,
        request_digest="1" * 64,
        policy_digest="2" * 64,
        approval_classes=("process",),
        expires_at=NOW + timedelta(hours=1),
    )
    decision = PolicyDecision(
        id="decision-1",
        run_id="run-1",
        created_at=NOW,
        request_digest=request.request_digest,
        effective_policy_digest=request.policy_digest,
        outcome=DecisionOutcome.APPROVAL_REQUIRED,
        reason_code="approval_required",
        required_approval_classes=("process",),
    )
    with SQLiteStore(tmp_path / "fleet.db") as database:
        service = DigestApprovalService(database, operator_label="operator", clock=lambda: NOW)
        record = service.request(request, decision)
        approved = service.decide(record.id, request.request_digest, "approved")
        assert service.authorize(decision, approved)
        assert service.apply(decision, approved).outcome is DecisionOutcome.ALLOW
        assert not service.authorize(decision, approved)
        with pytest.raises(ValueError, match="stale"):
            service.apply(decision, approved)
        with pytest.raises(ValueError, match="already"):
            service.decide(record.id, request.request_digest, "denied")
        with pytest.raises(ValueError, match="mismatch"):
            service.decide(record.id, "3" * 64, "denied")
    with SQLiteStore(tmp_path / "fleet.db") as database:
        restarted = DigestApprovalService(database, operator_label="operator", clock=lambda: NOW)
        assert not restarted.authorize(decision, approved)
        with pytest.raises(ValueError, match="stale"):
            restarted.apply(decision, approved)


def test_process_executor_filters_environment_and_bounds_output(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor(
        (tmp_path,), store, inherited_environment={"VISIBLE": "yes", "API_TOKEN": "no"}
    )
    request = ProcessRequest(
        id="process-1",
        run_id="run-1",
        created_at=NOW,
        argv=("/bin/sh", "-c", 'printf "%s:%s" "$VISIBLE" "${API_TOKEN-unset}"'),
        inherit_environment=("VISIBLE",),
        timeout_seconds=10.0,
        stdout_bytes=100,
        stderr_bytes=100,
        purpose="verify filtered environment",
    )
    result = executor.execute(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.status == "succeeded"
    descriptor_path = (
        store.content_root
        / (result.stdout_artifact_digest or "")[:2]
        / (result.stdout_artifact_digest or "")
    )
    assert descriptor_path.read_bytes() == b"yes:unset"

    noisy = request.model_copy(
        update={
            "id": "process-2",
            "argv": ("/bin/sh", "-c", "printf 12345"),
            "stdout_bytes": 4,
            "content_digest": None,
        }
    )
    noisy = ProcessRequest.model_validate(noisy.model_dump(), strict=True)
    failed = executor.execute(noisy, allow(noisy.content_digest or ""), NeverCancelled())
    assert failed.failure is not None
    assert failed.failure.code.value == "BUDGET_EXCEEDED"


def test_process_executor_reads_only_descriptor_bound_stdin(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts")
    descriptor = store.put(io.BytesIO(b"bounded input"), artifact_request())
    executor = LocalProcessExecutor(
        (tmp_path,),
        store,
        stdin_resolver=lambda digest: (
            store.open_verified(descriptor)
            if digest == descriptor.artifact_digest
            else (_ for _ in ()).throw(KeyError(digest))
        ),
    )
    request = ProcessRequest(
        id="process-stdin-1",
        run_id="run-1",
        created_at=NOW,
        argv=("/bin/sh", "-c", "cat"),
        stdin_artifact_digest=descriptor.artifact_digest,
        timeout_seconds=10.0,
        stdout_bytes=100,
        stderr_bytes=100,
        purpose="verify descriptor-bound stdin",
    )
    result = executor.execute(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.status == "succeeded"
    output = (
        store.content_root
        / (result.stdout_artifact_digest or "")[:2]
        / (result.stdout_artifact_digest or "")
    )
    assert output.read_bytes() == b"bounded input"


def test_process_timeout_terminates_child_process_group(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor((tmp_path,), store, terminate_grace_seconds=0.1)
    request = ProcessRequest(
        id="process-tree-1",
        run_id="run-1",
        created_at=NOW,
        argv=("/bin/sh", "-c", "sleep 10 & echo $! > child.pid; wait"),
        timeout_seconds=0.1,
        purpose="verify process group cleanup",
    )
    result = executor.execute(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.failure is not None
    assert result.failure.code.value == "TIMEOUT"
    assert (tmp_path / "child.pid").read_text().strip().isdigit()
    # The child inherits the captured pipes. Returning promptly therefore proves that
    # group cleanup closed the child's descriptors instead of leaving `sleep` running.
    assert result.duration_seconds < 2.0


def test_process_rejects_policy_budget_before_spawn(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor((tmp_path,), store)
    request = ProcessRequest(
        id="process-budget-1",
        run_id="run-1",
        created_at=NOW,
        argv=("/bin/sh", "-c", "touch should-not-exist"),
        timeout_seconds=11.0,
        purpose="verify pre-spawn budget enforcement",
    )
    result = executor.execute(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.failure is not None
    assert result.failure.code.value == "BUDGET_EXCEEDED"
    assert not (tmp_path / "should-not-exist").exists()


def test_download_revalidates_redirect_and_checksum_with_fake_transport(tmp_path: Path) -> None:
    store = AtomicArtifactStore(tmp_path / "artifacts")
    calls: list[str] = []

    def transport(url: str, peer: str, _connect: float, _read: float) -> TransportResponse:
        calls.append(url)
        if len(calls) == 1:
            return TransportResponse(
                302,
                {"location": "https://cdn.example.test/file"},
                io.BytesIO(),
                peer,
            )
        return TransportResponse(200, {"content-type": "text/plain"}, io.BytesIO(b"payload"), peer)

    request = DownloadRequest(
        id="download-1",
        run_id="run-1",
        created_at=NOW,
        url="https://example.test/start",
        purpose="test deterministic download",
        expected_media_type="text/plain",
        maximum_bytes=100,
        timeout_seconds=5.0,
        expected_sha256="239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
        destination_kind="download",
    )
    client = RestrictedDownloadClient(
        store,
        enabled=True,
        allowed_domains=("example.test", ".example.test"),
        resolver=lambda _host, _port: ("93.184.216.34",),
        transport=transport,
    )
    result = client.fetch(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.status == "succeeded"
    assert result.final_url == "https://cdn.example.test/file"
    assert len(calls) == 2

    blocked = request.model_copy(
        update={
            "id": "download-2",
            "url": "https://127.0.0.1/",
            "content_digest": None,
        }
    )
    blocked = DownloadRequest.model_validate(blocked.model_dump(), strict=True)
    denied = client.fetch(blocked, allow(blocked.content_digest or ""), NeverCancelled())
    assert denied.failure is not None
    assert denied.failure.code.value == "NETWORK_BLOCKED"

    fragment = request.model_copy(
        update={
            "id": "download-3",
            "url": "https://example.test/file#secret",
            "content_digest": None,
        }
    )
    fragment = DownloadRequest.model_validate(fragment.model_dump(), strict=True)
    rejected = client.fetch(fragment, allow(fragment.content_digest or ""), NeverCancelled())
    assert rejected.failure is not None
    assert rejected.failure.code.value == "NETWORK_BLOCKED"


def test_git_workspace_captures_and_promotes_exact_patch(tmp_path: Path) -> None:
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
    store = AtomicArtifactStore(tmp_path / "artifacts")
    manager = GitWorkspaceManager(tmp_path / "state", store)
    request = WorkspaceRequest(
        id="workspace-request-1",
        run_id="run-1",
        created_at=NOW,
        repository=str(repository),
        base_commit=head,
    )
    snapshot = manager.create(request)
    isolated = Path(snapshot.isolated_worktree)
    (isolated / "file.txt").write_text("after\n")
    (isolated / "new.txt").write_text("new\n")
    patch = manager.capture_diff(snapshot)
    approval = ApprovalRecord(
        id="approval-1",
        run_id="run-1",
        created_at=NOW,
        request_digest="1" * 64,
        policy_digest="2" * 64,
        scope=(patch.artifact_digest,),
        decision="approved",
        operator_label="operator",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        decided_at=NOW,
    )
    promoted = manager.promote(snapshot, patch, approval)
    assert (repository / "file.txt").read_text() == "after\n"
    assert (repository / "new.txt").read_text() == "new\n"
    assert manager.promote(snapshot, patch, approval) == promoted
    current_head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    assert current_head == head
    manager.cleanup(snapshot)
    assert not isolated.exists()


def test_git_workspace_rejects_state_root_inside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.test"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repository), "config", "user.name", "Test"), check=True)
    (repository / "file.txt").write_text("base\n")
    subprocess.run(("git", "-C", str(repository), "add", "file.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    manager = GitWorkspaceManager(
        repository / ".fleet", AtomicArtifactStore(tmp_path / "artifacts")
    )
    request = WorkspaceRequest(
        id="workspace-request-unsafe",
        run_id="run-1",
        created_at=NOW,
        repository=str(repository),
        base_commit=head,
    )
    with pytest.raises(ValueError, match="outside"):
        manager.create(request)


def test_git_workspace_applies_only_exact_declared_edit_patch(tmp_path: Path) -> None:
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
    manager = GitWorkspaceManager(tmp_path / "state", AtomicArtifactStore(tmp_path / "artifacts"))
    snapshot = manager.create(
        WorkspaceRequest(
            id="workspace-edit-request",
            run_id="run-1",
            created_at=NOW,
            repository=str(repository),
            base_commit=head,
        )
    )
    patch = (
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )
    mismatched = EditIntentRequest(
        id="edit-mismatch",
        run_id="run-1",
        created_at=NOW,
        paths=("other.txt",),
        summary="attempt a mismatched edit",
        unified_diff=patch,
    )
    rejected = manager.apply_edit(
        snapshot,
        mismatched,
        allow(mismatched.content_digest or ""),
        NeverCancelled(),
    )
    assert rejected.failure is not None
    assert rejected.failure.code.value == "INVALID_REQUEST"
    assert Path(snapshot.isolated_worktree, "file.txt").read_text() == "before\n"

    request = EditIntentRequest(
        id="edit-1",
        run_id="run-1",
        created_at=NOW,
        paths=("file.txt",),
        summary="apply an exact bounded patch",
        unified_diff=patch,
    )
    result = manager.apply_edit(
        snapshot, request, allow(request.content_digest or ""), NeverCancelled()
    )
    assert result.status == "succeeded"
    assert Path(snapshot.isolated_worktree, "file.txt").read_text() == "after\n"
    assert (repository / "file.txt").read_text() == "before\n"


def test_git_workspace_recounts_worker_hunk_lengths(tmp_path: Path) -> None:
    repository = tmp_path / "repo-recount"
    repository.mkdir()
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "fleet@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repository), "config", "user.name", "Fleet Test"), check=True)
    (repository / "base.txt").write_text("base\n")
    subprocess.run(("git", "-C", str(repository), "add", "base.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    manager = GitWorkspaceManager(
        tmp_path / "state-recount", AtomicArtifactStore(tmp_path / "artifacts-recount")
    )
    snapshot = manager.create(
        WorkspaceRequest(
            id="workspace-recount-request",
            run_id="run-1",
            created_at=NOW,
            repository=str(repository),
            base_commit=head,
        )
    )
    request = EditIntentRequest(
        id="edit-recount",
        run_id="run-1",
        created_at=NOW,
        paths=("new.txt",),
        summary="accept a structurally valid diff with a stale hunk count",
        unified_diff=(
            "diff --git a/new.txt b/new.txt\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.txt\n"
            "@@ -0,0 +1,99 @@\n"
            "+one\n"
            "+two\n"
        ),
    )

    result = manager.apply_edit(
        snapshot, request, allow(request.content_digest or ""), NeverCancelled()
    )

    assert result.status == "succeeded"
    assert Path(snapshot.isolated_worktree, "new.txt").read_text() == "one\ntwo\n"


def test_installer_denies_global_and_runs_local_fake_manager(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    manifest.write_text("[project]\n")
    lock.write_text("lock\n")
    manager = tmp_path / "manager"
    manager.write_text("#!/bin/sh\nmkdir -p .venv\nprintf installed > .venv/result\n")
    manager.chmod(0o755)
    store = AtomicArtifactStore(tmp_path / "artifacts")
    executor = LocalProcessExecutor((tmp_path,), store)
    installer = ProjectLocalInstaller(tmp_path, executor, store)
    request = InstallRequest(
        id="install-1",
        run_id="run-1",
        created_at=NOW,
        ecosystem="python_venv",
        operation="existing_lock",
        manifest_path="pyproject.toml",
        lock_path="uv.lock",
        manifest_digest=__import__("hashlib").sha256(manifest.read_bytes()).hexdigest(),
        lock_digest=__import__("hashlib").sha256(lock.read_bytes()).hexdigest(),
        manager_executable="manager",
        manager_version="fake-1",
        argv=("install",),
        target=".venv",
    )
    result = installer.install(request, allow(request.content_digest or ""), NeverCancelled())
    assert result.status == "succeeded"
    assert (tmp_path / ".venv" / "result").read_text() == "installed"

    global_request = request.model_copy(
        update={"id": "install-2", "operation": "host_global", "content_digest": None}
    )
    global_request = InstallRequest.model_validate(global_request.model_dump(), strict=True)
    denied = installer.install(
        global_request, allow(global_request.content_digest or ""), NeverCancelled()
    )
    assert denied.failure is not None
    assert denied.failure.code.value == "HOST_INSTALL_DENIED"

    escaped_target = request.model_copy(
        update={"id": "install-3", "target": ".", "content_digest": None}
    )
    escaped_target = InstallRequest.model_validate(escaped_target.model_dump(), strict=True)
    denied_target = installer.install(
        escaped_target, allow(escaped_target.content_digest or ""), NeverCancelled()
    )
    assert denied_target.failure is not None
    assert denied_target.failure.code.value == "HOST_INSTALL_DENIED"


def test_installer_restricts_node_existing_lock_arguments(tmp_path: Path) -> None:
    manifest = tmp_path / "package.json"
    lock = tmp_path / "package-lock.json"
    manager = tmp_path / "manager"
    manifest.write_text("{}\n")
    lock.write_text("{}\n")
    manager.write_text("#!/bin/sh\nexit 0\n")
    manager.chmod(0o755)
    store = AtomicArtifactStore(tmp_path / "artifacts-node-argv")
    installer = ProjectLocalInstaller(
        tmp_path,
        LocalProcessExecutor((tmp_path,), store),
        store,
    )
    request = InstallRequest(
        id="install-node-argv",
        run_id="run-1",
        created_at=NOW,
        ecosystem="node_project",
        operation="existing_lock",
        manifest_path="package.json",
        lock_path="package-lock.json",
        manifest_digest=__import__("hashlib").sha256(manifest.read_bytes()).hexdigest(),
        lock_digest=__import__("hashlib").sha256(lock.read_bytes()).hexdigest(),
        manager_executable="manager",
        manager_version="fake-1",
        argv=("install", "surprise"),
        target="node_modules",
    )

    result = installer.install(request, allow(request.content_digest or ""), NeverCancelled())

    assert result.failure is not None
    assert result.failure.code.value == "INVALID_REQUEST"
    assert "ci --ignore-scripts" in result.failure.message
