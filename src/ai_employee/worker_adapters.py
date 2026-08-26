"""Normalized, tool-disabled worker adapters for supported local CLIs."""

from __future__ import annotations

import json
import re
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
    uses_codex_edit_transport: ClassVar[bool] = False

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
        scratch_directory: str | None = None,
        output_schema_path: str | None = None,
        model: str | None = None,
        inherit_environment: tuple[str, ...] = (),
        include_response_schema: bool = False,
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
        if scratch_directory is not None and ("\x00" in scratch_directory or not scratch_directory):
            raise ValueError("worker scratch directory must be non-empty and NUL-free")
        self.scratch_directory = scratch_directory
        if output_schema_path is not None and (
            "\x00" in output_schema_path or not output_schema_path
        ):
            raise ValueError("worker output schema path must be non-empty and NUL-free")
        self.output_schema_path = output_schema_path
        if model is not None and ("\x00" in model or not model):
            raise ValueError("worker model must be non-empty and NUL-free")
        self.model = model
        self.inherit_environment = inherit_environment
        self.include_response_schema = include_response_schema
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
        prompt = _bounded_prompt(
            request,
            scratch_directory=self.scratch_directory,
            include_response_schema=self.include_response_schema,
            codex_edit_transport=self.uses_codex_edit_transport,
        )
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
                stdout_artifact_digest=process.stdout_artifact_digest,
                stderr_artifact_digest=process.stderr_artifact_digest,
            )
        try:
            payload = self._extract_payload(self._output(process.stdout_artifact_digest))
            envelope = _validate_worker_envelope(payload)
        except ValueError as error:
            return _worker_failure(
                request,
                started,
                StableFailureCode.WORKER_PROTOCOL_ERROR,
                f"invalid worker proposal envelope: {error}",
                stdout_artifact_digest=process.stdout_artifact_digest,
                stderr_artifact_digest=process.stderr_artifact_digest,
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
            inherit_environment=self.inherit_environment,
            stdin_artifact_digest=stdin_digest,
            timeout_seconds=timeout,
            stdout_bytes=1_000_000,
            stderr_bytes=1_000_000,
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
    uses_codex_edit_transport = True

    def _proposal_argv(self, inline_prompt: str | None) -> tuple[str, ...]:
        argv = [self.executable]
        if self.model is not None:
            argv.extend(("--model", self.model))
        argv.extend(
            (
                "--ask-for-approval",
                "never",
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--sandbox",
            )
        )
        if self.scratch_directory is not None:
            argv.extend(
                (
                    "workspace-write",
                    "--cd",
                    self.scratch_directory,
                    "--skip-git-repo-check",
                )
            )
        else:
            argv.append("read-only")
        if self.output_schema_path is not None:
            argv.extend(("--output-schema", self.output_schema_path))
        if inline_prompt is not None:
            argv.append(inline_prompt)
        return tuple(argv)

    def _extract_payload(self, output: str) -> str:
        """Decode the small Codex-compatible transport into the domain envelope."""

        wrapper = json.loads(output)
        if not isinstance(wrapper, dict):
            raise ValueError("Codex transport must be a JSON object")
        expected = {"schema_version", "proposals", "assistant_note", "usage_json"}
        if set(wrapper) != expected or wrapper.get("schema_version") != "2":
            raise ValueError("Codex transport has invalid or unknown fields")
        proposals = wrapper["proposals"]
        if not isinstance(proposals, list):
            raise ValueError("Codex proposals must be a JSON array")
        proposals.sort(
            key=lambda proposal: (
                0 if isinstance(proposal, dict) and proposal.get("kind") == "install" else 1
            )
        )
        for proposal in proposals:
            if not isinstance(proposal, dict):
                raise ValueError("Codex proposal entries must be JSON objects")
            # Runtime-owned attribution and scope binding must not depend on model text.
            proposal["worker_id"] = self.adapter
            proposal["run_id"] = self.run_id
            payload = proposal.get("payload")
            if isinstance(payload, dict):
                payload["run_id"] = self.run_id
        usage_json = wrapper["usage_json"]
        if not isinstance(usage_json, str):
            raise ValueError("Codex usage_json must be JSON text")
        usage = json.loads(usage_json)
        if usage is not None and not isinstance(usage, dict):
            raise ValueError("Codex usage_json must encode a JSON object")
        return json.dumps(
            {
                "schema_version": "2",
                "proposals": proposals,
                "assistant_note": wrapper["assistant_note"],
                "usage": usage,
            },
            separators=(",", ":"),
        )


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


class OllamaCliWorkerAdapter(CliWorkerAdapter):
    """Local-only adapter using Ollama's schema-constrained generation."""

    adapter = "ollama_cli"
    default_executable = "ollama"
    noninteractive_flag = "run"

    def _proposal_argv(self, inline_prompt: str | None) -> tuple[str, ...]:
        if self.model is None:
            raise ValueError("Ollama worker requires an explicit model")
        argv = [
            self.executable,
            "run",
            self.model,
            "--format",
            "json",
            "--hidethinking",
            "--nowordwrap",
        ]
        if inline_prompt is not None:
            argv.append(inline_prompt)
        return tuple(argv)

    def _extract_payload(self, output: str) -> str:
        return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", output).strip()


def _bounded_prompt(
    request: WorkerRequest,
    *,
    scratch_directory: str | None = None,
    include_response_schema: bool = False,
    codex_edit_transport: bool = False,
) -> bytes:
    payload: dict[str, object] = {
        "protocol": "fleet-worker-proposal/2",
        "run_id": request.run_id,
        "goal": request.goal,
        "accepted_plan_digest": request.accepted_plan_digest,
        "workspace_context": request.workspace_context,
        "harness_digest": request.harness_digest,
        "effective_policy_digest": request.effective_policy_digest,
        "remaining_budgets": request.remaining_budgets,
        "prior_result_digests": request.prior_result_digests,
        "response_contract": (
            "codex-edit-transport/1"
            if codex_edit_transport
            else "fleet-worker-proposal/2 schema supplied by the worker adapter"
        ),
        "writable_scratch_directory": scratch_directory,
        "instruction": (
            "Return only the strict JSON envelope. The repository is the current working "
            "directory; you may inspect its files with read-only tools. Do not edit the "
            "repository or run commands that change repository "
            "state; express every requested repository action only as a typed proposal. If a "
            "writable_scratch_directory is supplied, you may create temporary candidate files "
            "only below that exact directory and use them for deterministic diff generation and "
            "read-only validation against the repository. Every proposal and nested request must "
            "use the supplied run_id."
        ),
    }
    if include_response_schema:
        payload["response_schema"] = _envelope_schema()
    if codex_edit_transport:
        payload["transport_instruction"] = (
            "The supplied output schema accepts edit_intent proposals and existing_lock install "
            "proposals only. Put each proposed repository patch directly in proposals with all "
            "schema fields populated. Request an existing_lock install when repository-local "
            "dependencies are required for verification. That install must use manifest_path "
            "package.json, lock_path package-lock.json, manager_executable tools/fleet-npm, argv "
            "[ci, --ignore-scripts], target node_modules, network_required true, lifecycle_scripts "
            "false, and expected_mutations []; Fleet will execute it before edit proposals. Use "
            "the "
            "supplied run_id for both the proposal and payload run_id. Set assistant_note "
            "directly; use an empty string when there is no note. Encode a usage object as JSON "
            "text in usage_json, using {} when no usage is available. In unified_diff, use "
            "actual newline characters for line boundaries; do not "
            "flatten Markdown into one line with HTML <br> tags. Fleet will compute all omitted "
            "content digests locally."
        )
    value = canonical_json(payload).encode()
    if len(value) > 64_000:
        raise ValueError("bounded worker request exceeds 64000 bytes")
    return value


def _validate_worker_envelope(payload: str) -> WorkerProposalEnvelope:
    """Validate proposals after replacing worker-claimed digests with local computation."""

    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("worker proposal envelope must be a JSON object")
    proposals = raw.get("proposals")
    if isinstance(proposals, list):
        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal.pop("content_digest", None)
            proposal.pop("digest_metadata", None)
            request = proposal.get("payload")
            if isinstance(request, dict):
                request.pop("content_digest", None)
                request.pop("digest_metadata", None)
                unified_diff = request.get("unified_diff")
                if proposal.get("kind") == "edit_intent" and isinstance(unified_diff, str):
                    request["unified_diff"] = _normalize_new_file_diff(unified_diff)
    normalized = json.dumps(raw, separators=(",", ":"))
    return WorkerProposalEnvelope.model_validate_json(normalized, strict=True)


def _envelope_schema() -> dict[str, object]:
    return WorkerProposalEnvelope.model_json_schema()


def _normalize_new_file_diff(value: str) -> str:
    """Repair malformed new-file bodies without rewriting neighboring file patches."""

    lines = value.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    if not starts:
        return _normalize_new_file_section(value)
    chunks: list[str] = ["".join(lines[: starts[0]])]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        chunks.append(_normalize_new_file_section("".join(lines[start:end])))
    return "".join(chunks)


def _normalize_new_file_section(value: str) -> str:
    """Repair one new-file patch whose body omitted '+' markers."""

    lines = value.splitlines(keepends=True)
    if "--- /dev/null\n" not in lines or not any(line.startswith("+++ b/") for line in lines):
        return value
    hunk = next((index for index, line in enumerate(lines) if line.startswith("@@ ")), None)
    if hunk is None:
        return value
    if " @@" not in lines[hunk][3:]:
        ending = "\n" if lines[hunk].endswith("\n") else ""
        lines[hunk] = f"{lines[hunk].rstrip()} @@{ending}"
    body = lines[hunk + 1 :]
    if (
        len(body) == 1
        and body[0].startswith("+# ")
        and body[0].count("<br>") >= 10
    ):
        content = body[0][1:].rstrip("\n").replace("<br>", "\n")
        body = [f"+{line}\n" for line in content.split("\n")]
        lines = [*lines[: hunk + 1], *body]
    if not body or all(line.startswith(("+", "\\")) for line in body):
        return _recount_new_file_hunk(lines, hunk)
    repaired = [line if line.startswith("\\") else f"+{line}" for line in body]
    return _recount_new_file_hunk([*lines[: hunk + 1], *repaired], hunk)


def _recount_new_file_hunk(lines: list[str], hunk: int) -> str:
    added = sum(1 for line in lines[hunk + 1 :] if line.startswith("+"))
    ending = "\n" if lines[hunk].endswith("\n") else ""
    lines[hunk] = f"@@ -0,0 +1,{added} @@{ending}"
    return "".join(lines)


def worker_proposal_schema_json() -> bytes:
    """Return the minimal strict schema used to transport Codex worker output.

    The full Pydantic action union contains constraints outside the Structured Outputs
    subset. The Codex adapter therefore accepts only ``edit_intent`` and the narrowly scoped
    ``existing_lock`` install operation through a small strict schema; Fleet then validates
    every proposal locally before mediation.
    """

    identity_properties: dict[str, object] = {
        "schema_version": {"type": "string", "enum": ["2"]},
        "id": {"type": "string"},
        "run_id": {"type": "string"},
        "created_at": {"type": "string"},
    }
    edit_payload_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            **identity_properties,
            "paths": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "unified_diff": {"type": "string"},
        },
        "required": [*identity_properties, "paths", "summary", "unified_diff"],
        "additionalProperties": False,
    }
    install_payload_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            **identity_properties,
            "ecosystem": {"type": "string", "enum": ["node_project"]},
            "operation": {"type": "string", "enum": ["existing_lock"]},
            "manifest_path": {"type": "string", "enum": ["package.json"]},
            "lock_path": {"type": "string", "enum": ["package-lock.json"]},
            "manifest_digest": {"type": "string"},
            "lock_digest": {"type": "string"},
            "manager_executable": {"type": "string", "enum": ["tools/fleet-npm"]},
            "manager_version": {"type": "string"},
            "argv": {
                "type": "array",
                "items": {"type": "string"},
            },
            "target": {"type": "string", "enum": ["node_modules"]},
            "network_required": {"type": "boolean", "enum": [True]},
            "lifecycle_scripts": {"type": "boolean", "enum": [False]},
            "expected_mutations": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            *identity_properties,
            "ecosystem",
            "operation",
            "manifest_path",
            "lock_path",
            "manifest_digest",
            "lock_digest",
            "manager_executable",
            "manager_version",
            "argv",
            "target",
            "network_required",
            "lifecycle_scripts",
            "expected_mutations",
        ],
        "additionalProperties": False,
    }

    def proposal_schema(kind: str, payload_schema: dict[str, object]) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                **identity_properties,
                "worker_id": {"type": "string"},
                "kind": {"type": "string", "enum": [kind]},
                "payload": payload_schema,
                "reason": {"type": "string"},
                "expected_artifact_kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                *identity_properties,
                "worker_id",
                "kind",
                "payload",
                "reason",
                "expected_artifact_kinds",
            ],
            "additionalProperties": False,
        }

    edit_proposal_schema = proposal_schema("edit_intent", edit_payload_schema)
    install_proposal_schema = proposal_schema("install", install_payload_schema)
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": ["2"]},
            "proposals": {
                "type": "array",
                "items": {"anyOf": [edit_proposal_schema, install_proposal_schema]},
            },
            "assistant_note": {"type": "string"},
            "usage_json": {"type": "string"},
        },
        "required": [
            "schema_version",
            "proposals",
            "assistant_note",
            "usage_json",
        ],
        "additionalProperties": False,
    }
    return canonical_json(schema).encode("utf-8")


def _worker_failure(
    request: WorkerRequest,
    started: float,
    code: StableFailureCode,
    message: str,
    *,
    stdout_artifact_digest: str | None = None,
    stderr_artifact_digest: str | None = None,
) -> WorkerResult:
    return WorkerResult(
        id=identifier("worker-result"),
        run_id=request.run_id,
        created_at=now(),
        request_digest=request.content_digest or "",
        status="failed",
        failure=StableFailure(code=code, message=message[:2_000]),
        duration_seconds=time.monotonic() - started,
        stdout_artifact_digest=stdout_artifact_digest,
        stderr_artifact_digest=stderr_artifact_digest,
    )
