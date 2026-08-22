from __future__ import annotations

import io
import time
from collections.abc import Mapping
from pathlib import Path

from ai_employee.domain.base import freeze_json
from ai_employee.domain.services_v2 import ArtifactStore, Cancellation, ProcessExecutor
from ai_employee.domain.v2 import (
    ArtifactPutRequest,
    DecisionOutcome,
    InstallRequest,
    InstallResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
)

from ._common import identifier, now, sha256_file


class ProjectLocalInstaller:
    """Runs only explicitly described Python venv or Node project installations."""

    def __init__(
        self,
        project_root: str | Path,
        executor: ProcessExecutor,
        artifacts: ArtifactStore,
        *,
        network_mediated: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.executor = executor
        self.artifacts = artifacts
        self.network_mediated = network_mediated

    def install(
        self,
        request: InstallRequest,
        decision: PolicyDecision,
        cancellation: Cancellation,
    ) -> InstallResult:
        started = time.monotonic()
        failure = self._validate(request, decision)
        if failure is not None:
            return self._failed(request, started, failure)
        try:
            manifest = self._contained(request.manifest_path)
            lock = self._contained(request.lock_path)
            target = self._contained(request.target)
            manager = self._contained(request.manager_executable)
        except ValueError as error:
            return self._failed(
                request,
                started,
                StableFailure(code=StableFailureCode.INVALID_REQUEST, message=str(error)),
            )
        expected_target = ".venv" if request.ecosystem == "python_venv" else "node_modules"
        if request.target != expected_target:
            return self._failed(
                request,
                started,
                StableFailure(
                    code=StableFailureCode.HOST_INSTALL_DENIED,
                    message=f"{request.ecosystem} installs require project-local {expected_target}",
                ),
            )
        if not manifest.is_file() or not lock.is_file() or not manager.is_file():
            return self._failed(
                request,
                started,
                StableFailure(
                    code=StableFailureCode.INVALID_REQUEST,
                    message="install input is missing",
                ),
            )
        if (
            sha256_file(manifest) != request.manifest_digest
            or sha256_file(lock) != request.lock_digest
        ):
            return self._failed(
                request,
                started,
                StableFailure(
                    code=StableFailureCode.INTEGRITY_FAILED,
                    message="manifest or lock digest changed",
                ),
            )
        before = self._inventory(target)
        limits = decision.limits if isinstance(decision.limits, Mapping) else {}
        process_timeout = float(limits.get("max_wall_seconds", 300.0))
        process_request = ProcessRequest(
            id=identifier("install-process"),
            run_id=request.run_id,
            created_at=now(),
            argv=(f"./{request.manager_executable}", *request.argv),
            cwd=".",
            timeout_seconds=process_timeout,
            stdout_bytes=1_000_000,
            stderr_bytes=1_000_000,
            purpose=f"project-local {request.ecosystem} install",
        )
        process_decision = PolicyDecision(
            id=identifier("install-process-decision"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=process_request.content_digest or "",
            effective_policy_digest=decision.effective_policy_digest,
            outcome=DecisionOutcome.ALLOW,
            reason_code="install_subprocess_allowed",
            limits=decision.limits,
        )
        executed = self.executor.execute(process_request, process_decision, cancellation)
        after = self._inventory(target)
        post_manifest = sha256_file(manifest)
        post_lock = sha256_file(lock)
        changed_inputs = {
            path
            for path, old, new in (
                (request.manifest_path, request.manifest_digest, post_manifest),
                (request.lock_path, request.lock_digest, post_lock),
            )
            if old != new
        }
        if not changed_inputs.issubset(request.expected_mutations):
            return self._failed(
                request,
                started,
                StableFailure(
                    code=StableFailureCode.INTEGRITY_FAILED,
                    message="installer made an unexpected manifest or lock mutation",
                ),
            )
        inventory = self.artifacts.put(
            io.BytesIO("\n".join(after).encode()),
            ArtifactPutRequest(
                id=identifier("artifact-request"),
                run_id=request.run_id,
                created_at=now(),
                media_type="text/plain",
                logical_kind="install_inventory",
                producer_action_id=request.id,
                source=freeze_json(
                    {
                        "ecosystem": request.ecosystem,
                        "operation": request.operation,
                        "manager_version": request.manager_version,
                        "manifest_before": request.manifest_digest,
                        "manifest_after": post_manifest,
                        "lock_before": request.lock_digest,
                        "lock_after": post_lock,
                        "inventory_before_count": len(before),
                        "inventory_after_count": len(after),
                        "expected_mutations": request.expected_mutations,
                        "network_mediated": self.network_mediated,
                        "policy_digest": decision.effective_policy_digest,
                    }
                ),
            ),
        )
        return InstallResult(
            id=identifier("install"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status=executed.status,
            failure=executed.failure,
            exit_code=executed.exit_code,
            duration_seconds=time.monotonic() - started,
            resource_usage=freeze_json(
                {
                    "inventory_before": len(before),
                    "inventory_after": len(after),
                    "process_result_digest": executed.content_digest,
                }
            ),
            stdout_artifact_digest=executed.stdout_artifact_digest,
            stderr_artifact_digest=executed.stderr_artifact_digest,
            inventory_artifact_digest=inventory.artifact_digest,
        )

    def _validate(
        self, request: InstallRequest, decision: PolicyDecision
    ) -> StableFailure | None:
        forbidden = {"-g", "--global", "--user", "sudo"}
        if request.operation == "host_global" or any(item in forbidden for item in request.argv):
            return StableFailure(
                code=StableFailureCode.HOST_INSTALL_DENIED,
                message="host/global package installation is always denied",
            )
        if (
            decision.request_digest != request.content_digest
            or decision.outcome is DecisionOutcome.DENY
        ):
            return StableFailure(
                code=StableFailureCode.INSTALL_DENIED,
                message="install denied by policy",
            )
        if decision.outcome is DecisionOutcome.APPROVAL_REQUIRED:
            return StableFailure(
                code=StableFailureCode.APPROVAL_REQUIRED,
                message="install requires approval",
            )
        sensitive = request.operation in {
            "new_dependency",
            "manifest_lock_mutation",
            "lifecycle_scripts",
            "new_registry_domain",
        }
        if sensitive and decision.reason_code != "approved":
            return StableFailure(
                code=StableFailureCode.APPROVAL_REQUIRED,
                message="sensitive install requires approval",
            )
        needs_network = request.network_required or request.operation == "new_registry_domain"
        if needs_network and not self.network_mediated:
            return StableFailure(
                code=StableFailureCode.NETWORK_BLOCKED,
                message="package-manager network mediation is unavailable",
            )
        if request.lifecycle_scripts and request.operation != "lifecycle_scripts":
            return StableFailure(
                code=StableFailureCode.APPROVAL_REQUIRED,
                message="lifecycle scripts require an explicit classified operation",
            )
        return None

    def _contained(self, relative: str) -> Path:
        value = (self.project_root / relative).resolve()
        if value != self.project_root and self.project_root not in value.parents:
            raise ValueError("install path escapes project root")
        return value

    @staticmethod
    def _inventory(target: Path) -> tuple[str, ...]:
        if not target.exists():
            return ()
        if target.is_file():
            return (target.name,)
        return tuple(
            sorted(
                str(path.relative_to(target).as_posix())
                for path in target.rglob("*")
                if path.is_file()
            )
        )

    def _failed(
        self, request: InstallRequest, started: float, failure: StableFailure
    ) -> InstallResult:
        return InstallResult(
            id=identifier("install"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status="failed",
            failure=failure,
            duration_seconds=time.monotonic() - started,
        )
