from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ai_employee.domain import (
    ExecutionStrategy,
    RoutingMode,
    SemanticTaskAssessment,
    TaskAssessment,
)
from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    ActionKind,
    ActionProposal,
    ArtifactDescriptor,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    InstallResult,
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
    ClaudeCodeCliWorkerAdapter,
    CliTaskAssessmentAdapter,
    CodexCliWorkerAdapter,
    OllamaCliWorkerAdapter,
    ScriptedWorkerAdapter,
    WorkerProposalEnvelope,
    _bounded_prompt,
    _validate_worker_envelope,
    semantic_assessment_schema_json,
    worker_proposal_schema_json,
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
        self.request: ProcessRequest | None = None

    def execute(
        self, request: ProcessRequest, decision: PolicyDecision, _cancellation: object
    ) -> ExecutionResult:
        self.decision = decision
        self.request = request
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


def worker_request(goal: str = "make a bounded change") -> WorkerRequest:
    return WorkerRequest(
        id="worker-request-1",
        run_id="run-1",
        created_at=NOW,
        goal=goal,
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


def test_worker_prompt_binds_run_schema_and_scoped_scratch() -> None:
    local_goal = "Fix the local parser bug"
    broad_goal = "Exhaustively audit every authentication path for security defects"
    payloads = [
        json.loads(
            _bounded_prompt(worker_request(goal), scratch_directory="/tmp/fleet-worker-run-1")
        )
        for goal in (local_goal, broad_goal)
    ]

    for payload, goal in zip(payloads, (local_goal, broad_goal), strict=True):
        assert payload["protocol"] == "fleet-worker-proposal/2"
        assert payload["run_id"] == "run-1"
        assert payload["goal"] == goal
        assert payload["response_contract"].startswith("fleet-worker-proposal/2")
        assert "response_schema" not in payload
        assert payload["writable_scratch_directory"] == "/tmp/fleet-worker-run-1"
        assert "Return only the strict JSON envelope" in payload["instruction"]
        assert "read-only tools" in payload["instruction"]
        assert "current working directory" in payload["instruction"]
        assert "only below that exact directory" in payload["instruction"]
        assert "supplied run_id" in payload["instruction"]

    instruction = payloads[0]["instruction"]
    assert instruction == payloads[1]["instruction"]
    assert "minimal_sufficient as the default" in instruction
    assert "prefer existing mechanisms" in instruction
    assert "explicit in the supplied node goal" in instruction
    assert "do not infer it from importance, security relevance" in instruction
    assert "not unrelated implementation" in instruction
    assert "correctness, security, safety, required verification" in instruction
    assert "error handling, compatibility" in instruction
    assert "its reason must tie that expansion" in instruction
    assert "current goal requirement or concrete repository evidence" in instruction


def test_codex_worker_uses_explicit_scratch_as_its_only_workspace() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    adapter = CodexCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        allow,
        run_id="run-1",
        scratch_directory="/tmp/fleet-worker-run-1",
    )

    argv = adapter._proposal_argv("prompt")

    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--cd") + 1] == "/tmp/fleet-worker-run-1"
    assert "--skip-git-repo-check" in argv
    assert "--add-dir" not in argv


def test_codex_worker_without_scratch_inspects_current_repository_read_only() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    adapter = CodexCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        allow,
        run_id="run-1",
        output_schema_path="/tmp/fleet-worker-schema.json",
        model="qwen3-coder:30b",
        effort="high",
    )

    argv = adapter._proposal_argv("prompt")

    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--cd" not in argv
    assert "--skip-git-repo-check" not in argv
    assert argv[argv.index("--output-schema") + 1] == "/tmp/fleet-worker-schema.json"
    assert argv.count("--ask-for-approval") == 1
    assert argv[argv.index("--model") + 1] == "qwen3-coder:30b"
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="high"'


def test_codex_semantic_assessor_binds_sol_high_without_tools() -> None:
    strategy = ExecutionStrategy(
        id="codex-sol-high",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="codex_cli",
        model="gpt-5.6-sol",
        effort="high",
    )
    adapter = CliTaskAssessmentAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        lambda _request: (_ for _ in ()).throw(AssertionError("must not execute")),
        run_id="run-1",
        strategy=strategy,
        executable="codex",
        cwd=".",
        prompt_writer=lambda _value: ZERO,
        output_schema_path="/tmp/semantic-assessment.json",
    )

    argv = adapter._argv()

    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--output-schema" in argv


def test_claude_worker_binds_exact_model_and_effort() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    adapter = ClaudeCodeCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        allow,
        run_id="run-1",
        model="claude-exact-model",
        effort="high",
    )

    argv = adapter._proposal_argv("prompt")

    assert argv[argv.index("--model") + 1] == "claude-exact-model"
    assert argv[argv.index("--effort") + 1] == "high"


def test_ollama_worker_binds_thinking_effort() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    adapter = OllamaCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        allow,
        run_id="run-1",
        model="qwen3-coder:30b",
        effort="low",
    )

    argv = adapter._proposal_argv("prompt")

    assert argv[argv.index("--think") + 1] == "low"


def test_ollama_worker_uses_local_model_and_inline_schema() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    adapter = OllamaCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        allow,
        run_id="run-1",
        model="qwen3-coder:30b",
    )

    argv = adapter._proposal_argv("prompt")

    assert argv[:3] == ("ollama", "run", "qwen3-coder:30b")
    assert argv[argv.index("--format") + 1] == "json"
    assert "--hidethinking" in argv
    assert argv[-1] == "prompt"


def test_semantic_assessment_schema_requires_every_property() -> None:
    schema = json.loads(semantic_assessment_schema_json())

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "schema_version",
        "complexity",
        "scale",
        "required_capabilities",
        "reasons",
    ]


def test_semantic_assessment_runtime_defaults_remain_parseable() -> None:
    assessment = SemanticTaskAssessment.model_validate_json(
        '{"complexity":2,"scale":1,"reasons":["bounded change"]}',
        strict=True,
    )

    assert assessment.schema_version == "1"
    assert assessment.required_capabilities == ()


def test_worker_proposal_schema_is_canonical_json() -> None:
    schema = json.loads(worker_proposal_schema_json())

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "schema_version",
        "proposals",
        "assistant_note",
        "usage_json",
    ]
    edit_proposal, install_proposal = schema["properties"]["proposals"]["items"]["anyOf"]
    assert schema["properties"]["assistant_note"] == {"type": "string"}
    assert schema["properties"]["usage_json"] == {"type": "string"}
    assert edit_proposal["properties"]["kind"] == {
        "type": "string",
        "enum": ["edit_intent"],
    }
    assert edit_proposal["properties"]["payload"]["properties"]["unified_diff"] == {
        "type": "string"
    }
    assert install_proposal["properties"]["kind"] == {
        "type": "string",
        "enum": ["install"],
    }
    assert install_proposal["properties"]["payload"]["properties"]["operation"] == {
        "type": "string",
        "enum": ["existing_lock"],
    }
    install_payload = install_proposal["properties"]["payload"]["properties"]
    assert install_payload["manager_executable"]["enum"] == ["tools/fleet-npm"]
    assert install_payload["target"]["enum"] == ["node_modules"]


def test_codex_worker_decodes_edit_transport() -> None:
    adapter = CodexCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        lambda request: PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        ),
        run_id="run-1",
    )
    output = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "wrong-run",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "/root",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "wrong-run",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["example.md"],
                        "summary": "Add example.",
                        "unified_diff": "example patch",
                    },
                    "reason": "Add the requested example.",
                    "expected_artifact_kinds": ["documentation"],
                }
            ],
            "assistant_note": "No action needed.",
            "usage_json": '{"input_tokens":12}',
        }
    )

    decoded = json.loads(adapter._extract_payload(output))

    assert decoded == {
        "schema_version": "2",
        "proposals": [
            {
                "schema_version": "2",
                "id": "proposal-1",
                "run_id": "run-1",
                "created_at": "2026-01-01T00:00:00Z",
                "worker_id": "codex_cli",
                "kind": "edit_intent",
                "payload": {
                    "schema_version": "2",
                    "id": "edit-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "paths": ["example.md"],
                    "summary": "Add example.",
                    "unified_diff": "example patch",
                },
                "reason": "Add the requested example.",
                "expected_artifact_kinds": ["documentation"],
            }
        ],
        "assistant_note": "No action needed.",
        "usage": {"input_tokens": 12},
    }


def test_codex_worker_orders_existing_lock_install_before_edits() -> None:
    adapter = CodexCliWorkerAdapter(
        CapturingExecutor(),
        lambda _digest: b"",
        lambda request: PolicyDecision(
            id="worker-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        ),
        run_id="run-1",
    )
    output = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {"kind": "edit_intent", "payload": {}},
                {"kind": "install", "payload": {}},
            ],
            "assistant_note": "",
            "usage_json": "{}",
        }
    )

    decoded = json.loads(adapter._extract_payload(output))

    assert [proposal["kind"] for proposal in decoded["proposals"]] == [
        "install",
        "edit_intent",
    ]
    assert all(proposal["run_id"] == "run-1" for proposal in decoded["proposals"])


def test_codex_prompt_describes_edit_transport() -> None:
    prompt = json.loads(
        _bounded_prompt(
            worker_request(),
            codex_edit_transport=True,
        )
    )

    assert prompt["response_contract"] == "codex-edit-transport/1"
    assert "response_schema" not in prompt
    assert "edit_intent" in prompt["transport_instruction"]
    assert "existing_lock" in prompt["transport_instruction"]
    assert "diff --git" in prompt["transport_instruction"]
    assert "never use *** Begin Patch" in prompt["transport_instruction"]


def test_ollama_prompt_can_include_pydantic_schema_for_json_mode() -> None:
    prompt = _bounded_prompt(worker_request(), include_response_schema=True)
    schema = json.loads(prompt)["response_schema"]

    assert schema["title"] == "WorkerProposalEnvelope"
    assert "required" not in schema


def test_scripted_adapter_rejects_unknown_envelope_fields() -> None:
    adapter = ScriptedWorkerAdapter(
        [{"schema_version": "2", "proposals": (), "command": "touch escaped"}]
    )
    result = adapter.propose(worker_request(), Channel())  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code.value == "WORKER_PROTOCOL_ERROR"


def test_cli_worker_recomputes_untrusted_proposal_and_request_digests() -> None:
    raw = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "content_digest": ZERO,
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["example.txt"],
                        "summary": "Add an example file.",
                        "unified_diff": (
                            "diff --git a/example.txt b/example.txt\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/example.txt\n"
                            "@@ -0,0 +1 @@\n"
                            "+example\n"
                        ),
                        "content_digest": ZERO,
                    },
                    "reason": "Implement the requested example.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    proposal = envelope.proposals[0]
    assert proposal.content_digest != ZERO
    assert proposal.payload.content_digest != ZERO


def test_cli_worker_repairs_unmarked_new_file_diff_body() -> None:
    raw = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["example.md"],
                        "summary": "Add an example file.",
                        "unified_diff": (
                            "diff --git a/example.md b/example.md\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/example.md\n"
                            "@@ -0,0 +1,99\n"
                            "# Example\n"
                            "\n"
                            "- item\n"
                        ),
                    },
                    "reason": "Implement the requested example.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert "@@ -0,0 +1,3 @@\n" in payload.unified_diff
    assert payload.unified_diff.endswith("+# Example\n+\n+- item\n")


def test_cli_worker_repairs_only_new_file_sections_in_multi_file_diff() -> None:
    raw = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["package.json", "example.ts"],
                        "summary": "Update the manifest and add an example.",
                        "unified_diff": (
                            "diff --git a/package.json b/package.json\n"
                            "--- a/package.json\n"
                            "+++ b/package.json\n"
                            "@@ -1 +1 @@\n"
                            "-{}\n"
                            '+{"private":true}\n'
                            "diff --git a/example.ts b/example.ts\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/example.ts\n"
                            "@@ -0,0 +1,2 @@\n"
                            "export const answer = 42;\n"
                            "\n"
                        ),
                    },
                    "reason": "Implement the requested example.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert "\ndiff --git a/example.ts b/example.ts\n" in payload.unified_diff
    assert "\n+diff --git" not in payload.unified_diff
    assert payload.unified_diff.endswith("+export const answer = 42;\n+\n")


def test_cli_worker_recounts_incorrect_new_file_hunk_length() -> None:
    raw = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["example.ts"],
                        "summary": "Add an example.",
                        "unified_diff": (
                            "diff --git a/example.ts b/example.ts\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/example.ts\n"
                            "@@ -0,0 +1,99 @@\n"
                            "+export const one = 1;\n"
                            "+export const two = 2;\n"
                        ),
                    },
                    "reason": "Implement the requested example.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert "@@ -0,0 +1,2 @@\n" in payload.unified_diff


def test_cli_worker_expands_flattened_markdown_new_file_diff() -> None:
    breaks = "<br>".join(f"section {index}" for index in range(11))
    raw = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-1",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-1",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["plan.md"],
                        "summary": "Add plan.",
                        "unified_diff": (
                            "diff --git a/plan.md b/plan.md\n"
                            "new file mode 100644\n"
                            "--- /dev/null\n"
                            "+++ b/plan.md\n"
                            "@@ -0,0 +1 @@\n"
                            f"+# Plan<br>{breaks}\n"
                        ),
                    },
                    "reason": "Add the requested plan.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert "<br>" not in payload.unified_diff
    assert payload.unified_diff.count("\n+") >= 11


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
        executable="/custom/bin/codex",
    )
    availability = adapter.probe()
    assert availability.availability == "unavailable"
    assert availability.executable == "/custom/bin/codex"
    assert executor.request is not None
    assert executor.request.argv == ("/custom/bin/codex", "--version")
    assert executor.request.stderr_bytes == 1_000_000
    assert executor.decision is not None
    assert executor.decision.outcome is DecisionOutcome.DENY
    assert executor.decision.reason_code == "operator_policy_denied"


def test_plan_only_probes_without_workspace_or_action_mutation(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "fleet.db") as store:
        runtime = DeterministicRuntime({}, store=store)
        assessment = TaskAssessment(
            id="assessment-plan",
            run_id="work-plan",
            goal_digest=ZERO,
            complexity=1,
            scale=1,
            risk=0,
            reasons=("plan-only routing",),
        )
        strategy = ExecutionStrategy(
            id="strategy-codex",
            routing_mode=RoutingMode.POLICY,
            backend="codex_cli",
            model="gpt-5.6-luna",
            effort="medium",
        )
        coordinator = WorkCoordinator(
            store,
            runtime,
            NoWorkspace(),  # type: ignore[arg-type]
            lambda _snapshot, _cancellation: ScriptedWorkerAdapter([WorkerProposalEnvelope()]),
            lambda _snapshot: (_ for _ in ()).throw(
                AssertionError("plan-only must not create an executor")
            ),
            lambda _artifact: (_ for _ in ()).throw(
                AssertionError("plan-only must not read artifacts")
            ),
            (builtin_policy("work-plan"),),
            task_assessment=assessment,
            selected_strategy=strategy,
        )
        run = coordinator.start(
            "plan safely",
            str(tmp_path),
            "base",
            worker_name="scripted",
            plan_only=True,
            run_id="work-plan",
        )
        routing = inspect_work_run(store, run.id)["routing"]
        assert routing["assessment"]["complexity"] == 1
        assert routing["assessment"]["run_id"] == run.id
        assert routing["selected_strategy"]["backend"] == "codex_cli"
        assert routing["selected_strategy"]["model"] == "gpt-5.6-luna"
        assert routing["selected_strategy"]["effort"] == "medium"
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
    store, run = _complete_coordinator_run(tmp_path, patch, protected_paths=("protected.txt",))
    try:
        assert run.status == "failed"
        assert run.failure_code == "REVIEW_BLOCKED"
    finally:
        store.close()


def test_empty_patch_is_not_ready_to_promote(tmp_path: Path) -> None:
    store, run = _complete_coordinator_run(tmp_path, b"")
    try:
        assert run.status == "failed"
        assert run.failure_code == "EMPTY_PATCH"
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
        install = InstallResult(
            id="install-result-1",
            run_id="work-1",
            created_at=NOW,
            request_digest=ZERO,
            status="succeeded",
            duration_seconds=0.01,
            inventory_artifact_digest=ZERO,
        )
        store.put("action_result_v2", install, run_id="work-1")
        view = inspect_work_run(store, "work-1")
        assert view["kind"] == "work_run"
        assert view["state"] == "ready_to_promote"
        assert view["verification"][0]["status"] == "succeeded"
        assert any(item.get("inventory_artifact_digest") == ZERO for item in view["actions"])
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
