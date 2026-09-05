"""Opt-in first milestone: one Codex candidate and fresh, offline Fleet verification."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .domain import GoalTaskKind, ProjectHarnessV2
from .domain.base import freeze_json
from .domain.services_v2 import Cancellation, MediatedActionChannel
from .domain.v2 import (
    ActionKind,
    ActionProposal,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
)
from .isolated_worker import DockerCandidate, IsolatedWorkerProfile
from .serialization import canonical_json
from .services_v2._common import identifier, now
from .services_v2.process import LocalProcessExecutor


def codex_isolated_permission_args() -> tuple[str, ...]:
    """One profile for native execution and its credential-free OS preflight."""
    return (
        "--permission-profile",
        "fleet-isolated",
        "-c",
        'permissions.fleet-isolated.extends=":workspace"',
        "-c",
        "permissions.fleet-isolated.network.enabled=false",
        "-c",
        'permissions.fleet-isolated.filesystem={"/home/fleet/.codex/auth.json"="deny"}',
    )


CODEX_SANDBOX_PROBE = (
    "from pathlib import Path\n"
    "p=Path('/work/.fleet-native-probe'); p.write_text('ok'); p.unlink()\n"
    "for name in ['/work/.git/config','/etc/fleet-native-deny']:\n"
    " try: Path(name).write_text('must-deny')\n"
    " except OSError: pass\n"
    " else: raise AssertionError('protected write allowed')\n"
    "try: Path('/home/fleet/.codex/auth.json').read_bytes()\n"
    "except OSError: pass\n"
    "else: raise AssertionError('scoped authentication readable by a command')\n"
)


def validate_isolated_contract(
    harness: ProjectHarnessV2,
    *,
    routing: str,
    backend: str,
    task_kind: GoalTaskKind,
    processes_authorized: bool,
) -> None:
    """Refuse unsupported authority combinations before any model invocation."""
    if (
        routing != "fixed"
        or backend != "codex_cli"
        or task_kind is not GoalTaskKind.MUTATING
        or not processes_authorized
        or not harness.worker.isolated_workspace_tools
        or harness.provisional
        or harness.network.mode.value != "disabled"
        or harness.install.ecosystems
        or harness.verification.review.independent_task_review
        or harness.verification.review.parent_semantic_review
        or any(e.provider_id != "process.harness" for e in harness.evaluators)
    ):
        raise ValueError(
            "ISOLATION_CONTRACT_UNSUPPORTED: first milestone requires fixed Codex, mutating "
            "process-authorized Goal, explicit worker.isolated_workspace_tools=true, offline "
            "Harness, no installs or model/browser reviewers; no host fallback"
        )
    for command in harness.commands.values():
        if (
            command.cwd != "."
            or command.inherit_environment
            or command.argv[0] not in {"python", "python3"}
        ):
            raise ValueError(
                "ISOLATION_CONTRACT_UNSUPPORTED: declare container python/python3 at cwd '.' "
                "with no inherited environment; absolute host interpreter paths are not remapped"
            )


class DockerProcessExecutor(LocalProcessExecutor):
    """Reuse policy/output contracts, never LocalProcessExecutor's host execution."""

    profile: IsolatedWorkerProfile

    def execute(
        self, request: ProcessRequest, decision: PolicyDecision, cancellation: Cancellation
    ) -> ExecutionResult:
        started = time.monotonic()
        rejection = self._validate_policy(request, decision)
        if rejection:
            return self._result(request, started, failure=rejection)
        try:
            if (
                len(self.roots) != 1
                or request.cwd != "."
                or request.argv[0] not in {"python", "python3"}
                or request.environment
                or request.inherit_environment
                or request.secret_bindings
                or request.stdin_artifact_digest
            ):
                raise ValueError("unsupported isolated verification process contract")
            # No authentication and no model gateway in independent verification.
            offline = self.profile.model_copy(update={"auth_file": None})
            with DockerCandidate(
                offline,
                self.roots[0],
                seconds=request.timeout_seconds,
                cancellation=cancellation,
                output_limit=request.stdout_bytes + request.stderr_bytes,
                include_untracked=True,
            ) as candidate:
                code, stdout, stderr = candidate.run(request.argv)
            if len(stdout) > request.stdout_bytes or len(stderr) > request.stderr_bytes:
                raise ValueError("isolated process output exceeded its per-stream budget")
            execution_id = identifier("execution")
            return ExecutionResult(
                id=execution_id,
                run_id=request.run_id,
                created_at=now(),
                request_digest=request.content_digest or "",
                exit_code=code,
                candidate_patch_digest=request.candidate_patch_digest,
                verification_workspace_digest=request.verification_workspace_digest,
                status="succeeded" if code in request.expected_exit_codes else "failed",
                failure=None
                if code in request.expected_exit_codes
                else StableFailure(
                    code=StableFailureCode.PROCESS_FAILED,
                    message=f"isolated check exited with code {code}",
                ),
                duration_seconds=time.monotonic() - started,
                stdout_artifact_digest=self._store_output(
                    request, execution_id, stdout, "process_stdout"
                ),
                stderr_artifact_digest=self._store_output(
                    request, execution_id, stderr, "process_stderr"
                ),
                resource_usage=freeze_json(
                    {
                        "isolation": offline.backend,
                        "image": offline.image,
                        "container_cleanup": "confirmed",
                        "stdout_bytes": len(stdout),
                        "stderr_bytes": len(stderr),
                    }
                ),
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            failure_code = (
                StableFailureCode.CANCELLED
                if cancellation.cancelled()
                else (
                    StableFailureCode.TIMEOUT
                    if isinstance(error, TimeoutError)
                    else StableFailureCode.INVALID_REQUEST
                )
            )
            return self._result(
                request,
                started,
                failure=self._failure(failure_code, str(error)),
                status="cancelled" if cancellation.cancelled() else "failed",
            )


class IsolatedCodexWorker:
    adapter = "codex_cli"

    def __init__(
        self,
        root: Path,
        profile: IsolatedWorkerProfile,
        *,
        model: str,
        effort: str,
        cancellation: Cancellation,
        seconds: float,
        commands: tuple[tuple[str, ...], ...],
        persist: Callable[[bytes, str], str],
        generated_paths: tuple[str, ...] = (),
        on_usage_limit: Callable[[], None] = lambda: None,
    ) -> None:
        self.root, self.profile, self.model, self.effort = root, profile, model, effort
        self.cancellation, self.seconds, self.commands, self.persist = (
            cancellation,
            seconds,
            commands,
            persist,
        )
        self.generated_paths, self.on_usage_limit = generated_paths, on_usage_limit

    def probe(self) -> WorkerAvailability:
        # Actual OS/backend validation occurs inside propose before a model is started.
        return WorkerAvailability(
            id=identifier("worker-probe"),
            run_id="probe",
            created_at=now(),
            adapter=self.adapter,
            availability="unknown",
            auth="unknown",
            version=self.profile.backend,
        )

    def propose(
        self, request: WorkerRequest, mediated_channel: MediatedActionChannel
    ) -> WorkerResult:
        started = time.monotonic()
        native_usage: dict[str, object] = {}
        activity: list[dict[str, object]] = []
        stdout_digest = stderr_digest = None
        usage_limit = False
        try:
            if request.task_kind is not GoalTaskKind.MUTATING or not request.processes_authorized:
                raise ValueError("isolated iteration requires authorized mutating processes")
            if not isinstance(request.remaining_budgets, Mapping):
                raise ValueError("isolated execution requires explicit budgets")
            artifact_limit = min(1_000_000, int(request.remaining_budgets.get("artifact_bytes", 0)))
            if artifact_limit < 1 or not self.profile.auth_file:
                raise ValueError(
                    "isolated worker requires artifact budget and an explicit scoped auth_file"
                )
            prompt = canonical_json(
                {
                    "protocol": "fleet-isolated-candidate/1",
                    "request": request,
                    "checks": self.commands,
                    "instructions": "Edit actual files in /work. Run checks, observe failures, "
                    "and repair within this invocation. Do not emit a serialized patch. "
                    "Fleet captures Git changes and independently verifies them. Never change "
                    "acceptance checks, .git, permissions or budgets. Never redeem usage-reset "
                    "tickets or purchase extra usage. Stop if allowance is exhausted. "
                    "No network except configured model transport.",
                }
            ).encode()
            self.persist(prompt, "worker_request")

            def observe(event: dict[str, object]) -> None:
                nonlocal usage_limit
                kind = str(event.get("type", ""))
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "command_execution":
                    # Do not duplicate arbitrary output or credentials in activity metadata.
                    activity.append(
                        {"type": kind, "id": item.get("id"), "exit_code": item.get("exit_code")}
                    )
                usage = event.get("usage")
                if kind == "turn.completed" and isinstance(usage, dict):
                    native_usage.update(
                        {
                            name: value
                            for name, value in usage.items()
                            if name in {"input_tokens", "cached_input_tokens", "output_tokens"}
                            and type(value) is int
                            and value >= 0
                        }
                    )
                if kind in {"error", "turn.failed"}:
                    detail = json.dumps(event).lower()
                    usage_limit = any(
                        marker in detail
                        for marker in (
                            "usage_limit",
                            "usage limit",
                            "rate_limit",
                            "rate limit",
                            "insufficient_quota",
                        )
                    )
                    if usage_limit:
                        self.on_usage_limit()
                    raise RuntimeError(
                        "USAGE_LIMIT: stopped without reset or purchase"
                        if usage_limit
                        else "native worker reported terminal failure; "
                        "no native retry or allowance reset"
                    )

            with DockerCandidate(
                self.profile,
                self.root,
                seconds=self.seconds,
                cancellation=self.cancellation,
                output_limit=artifact_limit,
                include_untracked=True,
            ) as candidate:
                code, _, _ = candidate.run(("codex", "--version"))
                if code:
                    raise ValueError("configured runtime image does not contain Codex")
                code, _, _ = candidate.run(
                    (
                        "codex",
                        "sandbox",
                        *codex_isolated_permission_args(),
                        "--",
                        "python",
                        "-I",
                        "-c",
                        CODEX_SANDBOX_PROBE,
                    )
                )
                if code:
                    raise ValueError("ISOLATION_PREFLIGHT_FAILED: native permissions unavailable")
                argv = (
                    "codex",
                    "exec",
                    "--ignore-user-config",
                    "--ephemeral",
                    "--json",
                    *codex_isolated_permission_args(),
                    "-m",
                    self.model,
                    "-c",
                    f"model_reasoning_effort={json.dumps(self.effort)}",
                    "-",
                )
                code, stdout, stderr = candidate.run(argv, stdin=prompt, observe=observe)
                # Native free-form logs may echo scoped credentials; persist normalized activity.
                stdout_digest = self.persist(
                    canonical_json(
                        {
                            "activity": activity,
                            "usage": native_usage,
                            "exit_code": code,
                            "stdout_bytes": len(stdout),
                            "stderr_bytes": len(stderr),
                        }
                    ).encode(),
                    "worker_activity",
                )
                if code:
                    raise RuntimeError(f"native worker exited with code {code}; no automatic retry")
                paths, patch = candidate.capture(self.generated_paths)
            if self.cancellation.cancelled():
                raise TimeoutError("isolated execution cancelled before candidate submission")
            if not paths or not patch:
                raise ValueError("isolated worker produced no candidate changes")
            edit = EditIntentRequest(
                id=identifier("captured-edit"),
                run_id=request.run_id,
                created_at=now(),
                paths=paths,
                summary="Runtime-captured isolated candidate",
                unified_diff=patch.decode(),
            )
            proposal = ActionProposal(
                id=identifier("captured-proposal"),
                run_id=request.run_id,
                created_at=now(),
                worker_id="isolated-codex",
                kind=ActionKind.EDIT_INTENT,
                payload=edit,
                reason="Captured from actual Git changes; requires independent Fleet verification",
                expected_artifact_kinds=("workspace_patch",),
            )
            mediated_channel.submit(proposal)
            return WorkerResult(
                id=identifier("worker-result"),
                run_id=request.run_id,
                created_at=now(),
                request_digest=request.content_digest or "",
                status="succeeded",
                proposals=(proposal,),
                duration_seconds=time.monotonic() - started,
                stdout_artifact_digest=stdout_digest,
                usage=freeze_json(native_usage or None),
                resource_usage=freeze_json(
                    {
                        "isolation": self.profile.backend,
                        "image": self.profile.image,
                        "container_cleanup": "confirmed",
                        "local_activity": activity,
                    }
                ),
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            failure_code = (
                StableFailureCode.CANCELLED
                if self.cancellation.cancelled()
                else (
                    StableFailureCode.TIMEOUT
                    if isinstance(error, TimeoutError)
                    else StableFailureCode.WORKER_PROTOCOL_ERROR
                )
            )
            if usage_limit:
                failure_code = StableFailureCode.BUDGET_EXCEEDED
            return WorkerResult(
                id=identifier("worker-result"),
                run_id=request.run_id,
                created_at=now(),
                request_digest=request.content_digest or "",
                status="cancelled" if self.cancellation.cancelled() else "failed",
                failure=StableFailure(code=failure_code, message=str(error)),
                duration_seconds=time.monotonic() - started,
                stdout_artifact_digest=stdout_digest,
                stderr_artifact_digest=stderr_digest,
                usage=freeze_json(native_usage or None),
                resource_usage=freeze_json({"local_activity": activity}),
            )
