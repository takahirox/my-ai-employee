"""Normalized, tool-disabled worker adapters for supported local CLIs."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field
from pydantic.main import BaseModel

from .domain.base import freeze_json
from .domain.services_v2 import Cancellation, MediatedActionChannel, ProcessExecutor
from .domain.v2 import (
    ActionProposal,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
)
from .serialization import canonical_json
from .services_v2._common import identifier, now


class _NeverCancelled:
    def cancelled(self) -> bool:
        return False


class WorkerProposalEnvelope(BaseModel):
    """The only accepted worker response; prose is never interpreted as an action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["2"] = "2"
    proposals: tuple[ActionProposal, ...] = ()
    assistant_note: str | None = Field(default=None, max_length=20_000)
    usage: Mapping[str, object] | None = None


class ScriptedWorkerAdapter:
    """Deterministic adapter used by offline tests through the same mediation channel."""

    def __init__(
        self,
        results: Sequence[WorkerProposalEnvelope | Mapping[str, object]],
        *,
        adapter: str = "scripted",
    ) -> None:
        self.adapter = adapter
        self._results = list(results)
        self._turn = 0

    def probe(self) -> WorkerAvailability:
        return WorkerAvailability(
            id=identifier("worker-probe"),
            run_id="probe",
            created_at=now(),
            adapter=self.adapter,
            availability="available",
            auth="available",
            version="scripted-1",
        )

    def propose(
        self, request: WorkerRequest, mediated_channel: MediatedActionChannel
    ) -> WorkerResult:
        started = time.monotonic()
        if self._turn >= len(self._results):
            return _worker_failure(
                request,
                started,
                StableFailureCode.BUDGET_EXCEEDED,
                "scripted worker turn budget exhausted",
            )
        raw = self._results[self._turn]
        self._turn += 1
        try:
            envelope = (
                raw
                if isinstance(raw, WorkerProposalEnvelope)
                else WorkerProposalEnvelope.model_validate(raw, strict=True)
            )
        except ValueError as error:
            return _worker_failure(
                request,
                started,
                StableFailureCode.WORKER_PROTOCOL_ERROR,
                f"invalid worker proposal envelope: {error}",
            )
        for proposal in envelope.proposals:
            mediated_channel.submit(proposal)
        return WorkerResult(
            id=identifier("worker-result"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status="succeeded",
            duration_seconds=time.monotonic() - started,
            proposals=envelope.proposals,
            assistant_note=envelope.assistant_note,
            usage=freeze_json(dict(envelope.usage or {})),
        )


class CliWorkerAdapter:
    """Base adapter whose probing and proposal calls use ProcessExecutor exclusively."""

    adapter: ClassVar[str]
    default_executable: ClassVar[str]
    noninteractive_flag: ClassVar[str]

    def __init__(
        self,
        executor: ProcessExecutor,
        output_reader: Callable[[str], bytes],
        policy_decider: Callable[[ProcessRequest], PolicyDecision],
        *,
        run_id: str,
        executable: str | None = None,
        cwd: str = ".",
        prompt_writer: Callable[[bytes], str] | None = None,
        cancellation: Cancellation | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.executor = executor
        self.output_reader = output_reader
        self.policy_decider = policy_decider
        self.run_id = run_id
        selected_executable = executable or type(self).default_executable
        if not selected_executable or "\x00" in selected_executable:
            raise ValueError("worker executable must be non-empty and NUL-free")
        self.executable = selected_executable
        self.cwd = cwd
        self.prompt_writer = prompt_writer
        self.cancellation = cancellation or _NeverCancelled()
        self.timeout_seconds = timeout_seconds

    def probe(self) -> WorkerAvailability:
        version = self._execute((self.executable, "--version"), "probe worker version", 10.0)
        if version.status != "succeeded" or version.stdout_artifact_digest is None:
            return WorkerAvailability(
                id=identifier("worker-probe"),
                run_id=self.run_id,
                created_at=now(),
                adapter=self.adapter,
                executable=self.executable,
                availability="unavailable",
                auth="unknown",
                failure=version.failure
                or StableFailure(
                    code=StableFailureCode.WORKER_UNAVAILABLE,
                    message="worker executable is unavailable",
                ),
            )
        help_result = self._execute((self.executable, "--help"), "probe worker help", 10.0)
        help_text = self._output(help_result.stdout_artifact_digest)
        if help_result.status != "succeeded" or self.noninteractive_flag not in help_text:
            return WorkerAvailability(
                id=identifier("worker-probe"),
                run_id=self.run_id,
                created_at=now(),
                adapter=self.adapter,
                executable=self.executable,
                availability="unavailable",
                auth="unknown",
                version=self._output(version.stdout_artifact_digest).strip()[:200],
                failure=StableFailure(
                    code=StableFailureCode.WORKER_UNAVAILABLE,
                    message="safe non-interactive mode was not found in CLI help",
                ),
            )
        return WorkerAvailability(
            id=identifier("worker-probe"),
            run_id=self.run_id,
            created_at=now(),
            adapter=self.adapter,
            executable=self.executable,
            availability="auth_unknown",
            auth="unknown",
            version=self._output(version.stdout_artifact_digest).strip()[:200],
        )

    def propose(
        self, request: WorkerRequest, mediated_channel: MediatedActionChannel
    ) -> WorkerResult:
        started = time.monotonic()
        prompt = _bounded_prompt(request)
        stdin_digest = self.prompt_writer(prompt) if self.prompt_writer else None
        argv = self._proposal_argv(None if stdin_digest else prompt.decode("utf-8"))
        process = self._execute(
            argv,
            "obtain strict worker proposal envelope",
            self.timeout_seconds,
            stdin_digest=stdin_digest,
        )
        if process.status != "succeeded" or process.stdout_artifact_digest is None:
            return _worker_failure(
                request,
                started,
                StableFailureCode.WORKER_UNAVAILABLE,
                process.failure.message if process.failure else "worker invocation failed",
            )
        try:
            payload = self._extract_payload(self._output(process.stdout_artifact_digest))
            envelope = WorkerProposalEnvelope.model_validate_json(payload, strict=True)
        except ValueError as error:
            return _worker_failure(
                request,
                started,
                StableFailureCode.WORKER_PROTOCOL_ERROR,
                f"invalid worker proposal envelope: {error}",
            )
        for proposal in envelope.proposals:
            mediated_channel.submit(proposal)
        return WorkerResult(
            id=identifier("worker-result"),
            run_id=request.run_id,
            created_at=now(),
            request_digest=request.content_digest or "",
            status="succeeded",
            duration_seconds=time.monotonic() - started,
            stdout_artifact_digest=process.stdout_artifact_digest,
            stderr_artifact_digest=process.stderr_artifact_digest,
            proposals=envelope.proposals,
            assistant_note=envelope.assistant_note,
            usage=freeze_json(dict(envelope.usage or {})),
        )

    def _execute(
        self,
        argv: tuple[str, ...],
        purpose: str,
        timeout: float,
        *,
        stdin_digest: str | None = None,
    ) -> ExecutionResult:
        request = ProcessRequest(
            id=identifier("worker-process"),
            run_id=self.run_id,
            created_at=now(),
            argv=argv,
            cwd=self.cwd,
            stdin_artifact_digest=stdin_digest,
            timeout_seconds=timeout,
            stdout_bytes=1_000_000,
            stderr_bytes=200_000,
            budget_class="worker",
            purpose=purpose,
        )
        decision = self.policy_decider(request)
        return self.executor.execute(request, decision, self.cancellation)

    def _output(self, digest: str | None) -> str:
        return "" if digest is None else self.output_reader(digest).decode("utf-8", "replace")

    def _proposal_argv(self, inline_prompt: str | None) -> tuple[str, ...]:
        raise NotImplementedError

    def _extract_payload(self, output: str) -> str:
        return output.strip()


class CodexCliWorkerAdapter(CliWorkerAdapter):
    adapter = "codex_cli"
    default_executable = "codex"
    noninteractive_flag = "exec"

    def _proposal_argv(self, inline_prompt: str | None) -> tuple[str, ...]:
        argv = [
            self.executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
        ]
        if inline_prompt is not None:
            argv.append(inline_prompt)
        return tuple(argv)


class ClaudeCodeCliWorkerAdapter(CliWorkerAdapter):
    adapter = "claude_code_cli"
    default_executable = "claude"
    noninteractive_flag = "--print"

    def _proposal_argv(self, inline_prompt: str | None) -> tuple[str, ...]:
        argv = [
            self.executable,
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_envelope_schema(), separators=(",", ":")),
            "--tools",
            "",
            "--no-session-persistence",
        ]
        if inline_prompt is not None:
            argv.append(inline_prompt)
        return tuple(argv)

    def _extract_payload(self, output: str) -> str:
        wrapper = json.loads(output)
        if isinstance(wrapper, dict) and "structured_output" in wrapper:
            return json.dumps(wrapper["structured_output"], separators=(",", ":"))
        return output


def _bounded_prompt(request: WorkerRequest) -> bytes:
    payload = {
        "protocol": "fleet-worker-proposal/2",
        "goal": request.goal,
        "accepted_plan_digest": request.accepted_plan_digest,
        "workspace_context": request.workspace_context,
        "harness_digest": request.harness_digest,
        "effective_policy_digest": request.effective_policy_digest,
        "remaining_budgets": request.remaining_budgets,
        "prior_result_digests": request.prior_result_digests,
        "instruction": "Return only the strict JSON envelope. Do not run tools or commands.",
    }
    value = canonical_json(payload).encode()
    if len(value) > 64_000:
        raise ValueError("bounded worker request exceeds 64000 bytes")
    return value


def _envelope_schema() -> dict[str, object]:
    return WorkerProposalEnvelope.model_json_schema()


def _worker_failure(
    request: WorkerRequest,
    started: float,
    code: StableFailureCode,
    message: str,
) -> WorkerResult:
    return WorkerResult(
        id=identifier("worker-result"),
        run_id=request.run_id,
        created_at=now(),
        request_digest=request.content_digest or "",
        status="failed",
        failure=StableFailure(code=code, message=message[:2_000]),
        duration_seconds=time.monotonic() - started,
    )
