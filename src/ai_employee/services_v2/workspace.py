from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from ai_employee.domain.base import freeze_json
from ai_employee.domain.services_v2 import ArtifactStore, Cancellation
from ai_employee.domain.v2 import (
    ApprovalRecord,
    ArtifactDescriptor,
    ArtifactPutRequest,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    PromotionRecord,
    StableFailure,
    StableFailureCode,
    WorkspaceRequest,
    WorkspaceSnapshot,
)

from ._common import identifier, now, run_git, sha256_bytes


class GitWorkspaceManager:
    """Creates detached Fleet-owned sibling worktrees and safely promotes exact patches."""

    def __init__(self, state_root: str | Path, artifacts: ArtifactStore) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self._owned: set[Path] = set()
        self._promotions: dict[tuple[str, str], PromotionRecord] = {}

    def create(self, request: WorkspaceRequest) -> WorkspaceSnapshot:
        repository = Path(request.repository).resolve()
        if not (repository / ".git").exists():
            try:
                run_git(repository, "rev-parse", "--is-inside-work-tree")
            except ValueError as error:
                raise ValueError("repository must be a Git worktree") from error
        common_dir = self._git_path(repository, "--git-common-dir")
        if self.state_root == repository or repository in self.state_root.parents:
            raise ValueError("Fleet state root must be outside the source repository")
        head = run_git(repository, "rev-parse", "HEAD").decode().strip()
        requested = (
            run_git(repository, "rev-parse", f"{request.base_commit}^{{commit}}").decode().strip()
        )
        if requested != head:
            raise ValueError("base_commit must match the source worktree HEAD")
        status = run_git(repository, "status", "--porcelain=v2", "--untracked-files=all")
        if status:
            raise ValueError("source worktree is dirty")
        ignored = run_git(
            repository,
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
            "--ignored=matching",
        )
        tree = run_git(repository, "rev-parse", "HEAD^{tree}").decode().strip()
        index = self._git_path(repository, "--git-path", "index")
        identity_payload = b"\0".join(
            (str(common_dir).encode(), str(repository).encode(), head.encode(), tree.encode())
        )
        repository_identity = sha256_bytes(identity_payload)
        dirty_digest = sha256_bytes(status)
        destination = self.state_root / f"worktree-{request.run_id}-{request.id}"
        if self.state_root not in destination.resolve().parents or destination.exists():
            raise ValueError("isolated worktree destination is unsafe or already exists")
        completed = subprocess.run(
            ("git", "-C", str(repository), "worktree", "add", "--detach", str(destination), head),
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise ValueError(completed.stderr.decode("utf-8", "replace").strip())
        self._owned.add(destination)
        return WorkspaceSnapshot(
            id=identifier("workspace"),
            run_id=request.run_id,
            created_at=now(),
            repository_identity=repository_identity,
            original_worktree=str(repository),
            head_commit=head,
            base_tree=tree,
            dirty_state_digest=dirty_digest,
            isolated_worktree=str(destination),
            worktree_metadata=freeze_json(
                {
                    "common_dir": str(common_dir),
                    "index_digest": self._file_digest(index),
                    "ignored_state_digest": sha256_bytes(ignored),
                    "owner": "fleet",
                    "request_digest": request.content_digest,
                }
            ),
        )

    def adopt(self, snapshot: WorkspaceSnapshot) -> None:
        """Rehydrate ownership after a CLI restart without trusting an arbitrary path."""
        path = Path(snapshot.isolated_worktree).resolve()
        metadata = snapshot.worktree_metadata
        owner = metadata.get("owner") if isinstance(metadata, Mapping) else None
        if owner != "fleet" or self.state_root not in path.parents or not path.is_dir():
            raise ValueError("persisted workspace ownership is invalid")
        if run_git(path, "rev-parse", "HEAD").decode().strip() != snapshot.head_commit:
            raise ValueError("persisted workspace HEAD changed")
        self._owned.add(path)

    def capture_diff(self, snapshot: WorkspaceSnapshot) -> ArtifactDescriptor:
        isolated = self._require_owned(snapshot)
        patch_bytes = self._diff(isolated, snapshot.id)
        return self.artifacts.put(
            io.BytesIO(patch_bytes),
            ArtifactPutRequest(
                id=identifier("artifact-request"),
                run_id=snapshot.run_id,
                created_at=now(),
                media_type="text/x-diff",
                logical_kind="workspace_patch",
                producer_action_id=snapshot.id,
                source=freeze_json(
                    {"base_tree": snapshot.base_tree, "workspace_digest": snapshot.content_digest}
                ),
            ),
        )

    def apply_edit(
        self,
        snapshot: WorkspaceSnapshot,
        request: EditIntentRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> ExecutionResult:
        """Apply an exact declared unified diff; never interpret prose as edits."""

        if decision.request_digest != request.content_digest:
            return self._edit_failure(request, StableFailureCode.POLICY_DENIED, "digest mismatch")
        if decision.outcome is not DecisionOutcome.ALLOW:
            code = (
                StableFailureCode.APPROVAL_REQUIRED
                if decision.outcome is DecisionOutcome.APPROVAL_REQUIRED
                else StableFailureCode.POLICY_DENIED
            )
            return self._edit_failure(request, code, "edit is not allowed by policy")
        if cancellation.cancelled():
            return self._edit_failure(
                request, StableFailureCode.CANCELLED, "edit was cancelled", status="cancelled"
            )
        isolated = self._require_owned(snapshot)
        patch = request.unified_diff.encode("utf-8")
        declared = set(request.paths)
        observed = self._patch_paths(request.unified_diff)
        if not observed or observed != declared:
            return self._edit_failure(
                request,
                StableFailureCode.INVALID_REQUEST,
                "unified diff paths must exactly match declared edit paths",
            )
        check = subprocess.run(
            (
                "git",
                "-C",
                str(isolated),
                "apply",
                "--check",
                "--recount",
                "--unidiff-zero",
                "--whitespace=nowarn",
                "-",
            ),
            input=patch,
            capture_output=True,
            check=False,
        )
        if check.returncode:
            return self._edit_failure(
                request,
                StableFailureCode.PATCH_PREFLIGHT_FAILED,
                check.stderr.decode("utf-8", "replace")[:2_000] or "patch preflight failed",
            )
        applied = subprocess.run(
            (
                "git",
                "-C",
                str(isolated),
                "apply",
                "--recount",
                "--unidiff-zero",
                "--whitespace=nowarn",
                "-",
            ),
            input=patch,
            capture_output=True,
            check=False,
        )
        if applied.returncode:
            return self._edit_failure(
                request,
                StableFailureCode.PROCESS_FAILED,
                applied.stderr.decode("utf-8", "replace")[:2_000] or "patch application failed",
            )
        return ExecutionResult(
            id=identifier("edit-result"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status="succeeded",
            exit_code=0,
            duration_seconds=0.0,
            resource_usage=freeze_json(
                {"patch_sha256": sha256_bytes(patch), "changed_paths": tuple(sorted(observed))}
            ),
        )

    def _diff(self, worktree: Path, nonce: str) -> bytes:
        index = self._git_path(worktree, "--git-path", "index")
        temporary_index = self.state_root / f"index-{nonce}"
        shutil.copyfile(index, temporary_index)
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(temporary_index)
        try:
            add = subprocess.run(
                ("git", "-C", str(worktree), "add", "--intent-to-add", "--", "."),
                env=environment,
                capture_output=True,
                check=False,
            )
            if add.returncode:
                raise ValueError(add.stderr.decode("utf-8", "replace"))
            patch = subprocess.run(
                ("git", "-C", str(worktree), "diff", "--binary", "--no-ext-diff", "HEAD", "--"),
                env=environment,
                capture_output=True,
                check=False,
            )
            if patch.returncode:
                raise ValueError(patch.stderr.decode("utf-8", "replace"))
        finally:
            temporary_index.unlink(missing_ok=True)
        return patch.stdout

    def promote(
        self,
        snapshot: WorkspaceSnapshot,
        reviewed_patch: ArtifactDescriptor,
        approval: ApprovalRecord,
    ) -> PromotionRecord:
        key = (approval.id, reviewed_patch.artifact_digest)
        if key in self._promotions:
            return self._promotions[key]
        current_patch = self.capture_diff(snapshot)
        if current_patch.artifact_digest != reviewed_patch.artifact_digest:
            raise ValueError("reviewed patch no longer matches the isolated workspace")
        if approval.decision != "approved" or approval.expires_at <= now():
            raise ValueError("promotion requires a current approval")
        if reviewed_patch.artifact_digest not in approval.scope:
            raise ValueError("approval scope does not contain the reviewed patch")
        original = Path(snapshot.original_worktree).resolve()
        if self._identity(original) != snapshot.repository_identity:
            raise ValueError("source repository identity changed")
        metadata = snapshot.worktree_metadata
        expected_index = metadata.get("index_digest") if isinstance(metadata, Mapping) else None
        current_index = self._file_digest(self._git_path(original, "--git-path", "index"))
        if current_index != expected_index:
            raise ValueError("source worktree index changed")
        current_status = run_git(original, "status", "--porcelain=v2", "--untracked-files=all")
        if sha256_bytes(current_status) != snapshot.dirty_state_digest:
            raise ValueError("source worktree dirty state changed")
        with self.artifacts.open_verified(reviewed_patch) as stream:
            patch = stream.read()
        check = subprocess.run(
            ("git", "-C", str(original), "apply", "--check", "--whitespace=nowarn", "-"),
            input=patch,
            capture_output=True,
            check=False,
        )
        if check.returncode:
            raise ValueError(f"patch preflight failed: {check.stderr.decode('utf-8', 'replace')}")
        preflight = sha256_bytes(check.stdout + check.stderr + b"ok")
        applied = subprocess.run(
            ("git", "-C", str(original), "apply", "--whitespace=nowarn", "-"),
            input=patch,
            capture_output=True,
            check=False,
        )
        if applied.returncode:
            message = applied.stderr.decode("utf-8", "replace")
            raise ValueError(f"patch application failed: {message}")
        resulting = self._diff(original, approval.id)
        if sha256_bytes(resulting) != reviewed_patch.artifact_digest:
            raise ValueError("applied diff digest does not match reviewed patch")
        record = PromotionRecord(
            id=identifier("promotion"),
            run_id=snapshot.run_id,
            created_at=now(),
            base_identity=snapshot.repository_identity,
            reviewed_patch_digest=reviewed_patch.artifact_digest,
            verification_digest=approval.request_digest,
            review_digest=approval.policy_digest,
            preflight_result_digest=preflight,
            applied_tree_digest=sha256_bytes(run_git(original, "diff", "--raw", "HEAD")),
            applied_diff_digest=sha256_bytes(resulting),
        )
        self._promotions[key] = record
        return record

    def cleanup(self, snapshot: WorkspaceSnapshot) -> None:
        path = self._require_owned(snapshot)
        original = Path(snapshot.original_worktree)
        run_git(original, "worktree", "remove", "--force", str(path))
        self._owned.remove(path)

    @staticmethod
    def _patch_paths(patch: str) -> set[str]:
        paths: set[str] = set()
        for line in patch.splitlines():
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                value = line[6:]
                if value != "/dev/null":
                    paths.add(value)
        return paths

    @staticmethod
    def _edit_failure(
        request: EditIntentRequest,
        code: StableFailureCode,
        message: str,
        *,
        status: Literal["failed", "cancelled"] = "failed",
    ) -> ExecutionResult:
        return ExecutionResult.model_validate(
            {
                "id": identifier("edit-result"),
                "run_id": request.run_id,
                "created_at": now(),
                "request_digest": request.content_digest,
                "status": status,
                "failure": StableFailure(code=code, message=message),
                "duration_seconds": 0.0,
            },
            strict=True,
        )

    def _require_owned(self, snapshot: WorkspaceSnapshot) -> Path:
        path = Path(snapshot.isolated_worktree).resolve()
        if path not in self._owned or self.state_root not in path.parents:
            raise ValueError("worktree is not owned by this manager")
        return path

    def _identity(self, worktree: Path) -> str:
        common = self._git_path(worktree, "--git-common-dir")
        head = run_git(worktree, "rev-parse", "HEAD").decode().strip()
        tree = run_git(worktree, "rev-parse", "HEAD^{tree}").decode().strip()
        payload = b"\0".join(
            (str(common).encode(), str(worktree).encode(), head.encode(), tree.encode())
        )
        return sha256_bytes(payload)

    @staticmethod
    def _git_path(worktree: Path, *args: str) -> Path:
        value = run_git(worktree, "rev-parse", *args).decode().strip()
        path = Path(value)
        return path.resolve() if path.is_absolute() else (worktree / path).resolve()

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
