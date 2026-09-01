from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from ai_employee.domain import (
    CompletionCriterion,
    ExecutionStrategy,
    GoalTaskKind,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskAssessment,
    SemanticTaskProfile,
    SemanticTaskType,
    TaskAssessment,
)
from ai_employee.domain.base import DIGEST_PATTERN
from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    AcceptanceLedger,
    ActionKind,
    ActionProposal,
    ArtifactDescriptor,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    InstallResult,
    NodeVerificationBinding,
    NonMutatingResult,
    NonMutatingResultAcceptance,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
    WorkerBoundaryDiagnostic,
    WorkerRequest,
    WorkerResult,
    WorkspaceRequest,
    WorkspaceSnapshot,
)
from ai_employee.inspector import inspect_work_run
from ai_employee.orchestration import (
    WorkCoordinator,
    WorkRun,
    _mediated_result_artifacts,
    _node_verification_configuration_is_valid,
)
from ai_employee.run_explanation import explain_any_run
from ai_employee.runtime import DeterministicRuntime
from ai_employee.serialization import canonical_digest
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
    _claude_envelope_schema,
    _envelope_schema,
    _normalize_unified_diff,
    _validate_worker_envelope,
    cli_inherit_environment,
    semantic_assessment_schema_json,
    worker_proposal_schema_json,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def test_install_process_outputs_retain_the_original_process_provenance() -> None:
    descriptor = ArtifactDescriptor(
        id="artifact-1",
        run_id="work-1",
        created_at=NOW,
        media_type="text/plain",
        logical_kind="process_stdout",
        producer_action_id="process-result-1",
        size_bytes=1,
        artifact_digest=ZERO,
        source={"request_digest": ZERO},
        store_locator=f"sha256/{ZERO[:2]}/{ZERO}",
    )

    class Resolver:
        def output_descriptor(
            self, digest: str, logical_kind: str, producer_action_id: str
        ) -> ArtifactDescriptor:
            assert (digest, logical_kind, producer_action_id) == (
                ZERO,
                "process_stdout",
                "process-result-1",
            )
            return descriptor

    result = InstallResult(
        id="install-result-1",
        run_id="work-1",
        created_at=NOW,
        request_digest=ZERO,
        status="succeeded",
        duration_seconds=0.01,
        stdout_artifact_digest=ZERO,
        resource_usage={"process_result_id": "process-result-1"},
    )

    assert _mediated_result_artifacts(result, Resolver()) == (descriptor,)


def test_cli_environment_inherits_only_required_local_state() -> None:
    assert cli_inherit_environment("claude_code_cli") == ("HOME", "USER")
    assert cli_inherit_environment("ollama_cli") == ("HOME",)
    assert cli_inherit_environment("codex_cli") == ()


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


class FailingVerificationExecutor:
    def execute(
        self, request: ProcessRequest, _decision: PolicyDecision, _cancellation: object
    ) -> ExecutionResult:
        return ExecutionResult(
            id=f"result-{request.id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            status="failed",
            failure=StableFailure(
                code=StableFailureCode.PROCESS_FAILED,
                message="ruff found an unused import",
            ),
            exit_code=1,
            duration_seconds=0.01,
        )


class BoundProcessExecutor:
    def __init__(self, code: StableFailureCode, status: str) -> None:
        self.code = code
        self.status = status

    def execute(
        self, request: ProcessRequest, _decision: PolicyDecision, _cancellation: object
    ) -> ExecutionResult:
        return ExecutionResult(
            id=f"process-result-{request.id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            status=self.status,  # type: ignore[arg-type]
            failure=StableFailure(code=self.code, message="bounded worker failure"),
            exit_code=7,
            duration_seconds=0.25,
            resource_usage={"stdout_bytes": 17, "stderr_bytes": 23},
            stdout_artifact_digest="1" * 64,
            stderr_artifact_digest="2" * 64,
        )


def allow_worker(request: ProcessRequest) -> PolicyDecision:
    return PolicyDecision(
        id="worker-policy-1",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "",
        effective_policy_digest=ZERO,
        outcome=DecisionOutcome.ALLOW,
        reason_code="policy_allowed",
        limits={"max_wall_seconds": 12.0},
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


def test_scripted_adapter_rejects_empty_edit_required_mutating_envelope() -> None:
    adapter = ScriptedWorkerAdapter(
        [{"schema_version": "2", "proposals": (), "assistant_note": ""}]
    )
    request = worker_request().model_copy(
        update={"required_capabilities": ("edit_intent",)}
    )

    result = adapter.propose(request, Channel())  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is StableFailureCode.WORKER_PROTOCOL_ERROR
    assert result.boundary_diagnostic is not None
    assert result.boundary_diagnostic.code == "MUTATING_ENVELOPE_EMPTY"


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
        assert payload["non_mutating_result_binding"] == {
            "run_id": "run-1",
            "graph_run_id": None,
            "worker_request_digest": worker_request(goal).content_digest,
            "node_id": None,
            "accepted_graph_revision_digest": None,
            "generation": 0,
            "attempt": 0,
        }
        assert payload["prior_artifact_digests"] == []
        assert payload["allowed_evidence"]["sources"] == []
        assert payload["predecessor_outputs"] == []
        assert payload["completion_criteria"] == []
        assert payload["required_capabilities"] == []
        assert payload["accepted_graph_revision_digest"] is None
        assert payload["generation"] == 0
        assert payload["attempt"] == 0
        assert "conversation_history" not in payload
        assert payload["response_contract"].startswith("fleet-worker-proposal/2")
        assert "response_schema" not in payload
        assert payload["writable_scratch_directory"] == "/tmp/fleet-worker-run-1"
        assert "Return only the strict JSON envelope" in payload["instruction"]
        assert "read-only tools" in payload["instruction"]
        assert "must still return a typed edit proposal" in payload["instruction"]
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
    assert "Treat the goal, predecessor results, evidence bindings" in instruction
    assert "No conversation history is supplied" in instruction
    assert "body-free descriptors" in instruction
    assert "64-character lowercase SHA-256 digest" in instruction
    assert "file paths and line locations in content or findings" in instruction
    assert "return evidence_refs: []" in instruction
    assert StableFailureCode.CONTEXT_INSUFFICIENT.value == "CONTEXT_INSUFFICIENT"


def test_worker_prompt_exposes_only_authoritative_evidence_with_provenance() -> None:
    allowed = "a" * 64
    request = WorkerRequest.model_validate(
        {
            **worker_request().model_dump(exclude={"content_digest"}),
            "accepted_feedback_digests": (allowed,),
        },
        strict=True,
    )

    payload = json.loads(_bounded_prompt(request))

    assert payload["allowed_evidence"] == {
        "algorithm": "sha256",
        "maximum_references": 64,
        "pattern": DIGEST_PATTERN,
        "sources": [
            {
                "digest": allowed,
                "predecessor_node_ids": [],
                "source_kinds": ["accepted_feedback"],
            }
        ],
    }
    assert ZERO not in {item["digest"] for item in payload["allowed_evidence"]["sources"]}


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


def test_claude_semantic_assessor_disables_tools_without_empty_argv() -> None:
    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="assessment-policy-1",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or "",
            effective_policy_digest=ZERO,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    strategy = ExecutionStrategy(
        id="claude-fable-high",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="claude_code_cli",
        model="claude-fable-5",
        effort="high",
    )
    executor = CapturingExecutor()
    adapter = CliTaskAssessmentAdapter(
        executor,
        lambda _digest: b"",
        allow,
        run_id="run-1",
        strategy=strategy,
        executable="claude",
        cwd=".",
        prompt_writer=lambda _value: ZERO,
    )

    argv = adapter._argv()

    assert "--tools=" in argv
    assert "" not in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert argv[argv.index("--effort") + 1] == "high"

    with pytest.raises(ValueError, match="denied by injected runtime policy"):
        adapter.assess(
            "render an articulated arm in 3D",
            TaskAssessment(
                id="assessment-claude-home",
                run_id="run-1",
                goal_digest=ZERO,
                complexity=1,
                scale=1,
                risk=0,
                required_capabilities=("process",),
                reasons=("test",),
            ),
        )
    assert executor.request is not None
    assert executor.request.inherit_environment == ("HOME", "USER")


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

    assert "--tools=Read,Glob,Grep" in argv
    assert "" not in argv
    assert "--safe-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "manual"
    assert argv[argv.index("--model") + 1] == "claude-exact-model"
    assert argv[argv.index("--effort") + 1] == "high"


def test_claude_worker_schema_exposes_only_edit_intents() -> None:
    schema = _claude_envelope_schema()
    encoded = json.dumps(schema)

    assert "prefixItems" not in encoded
    assert "ProcessRequest" not in encoded
    assert "InstallRequest" not in encoded
    properties = schema["properties"]
    assert isinstance(properties, dict)
    proposals = properties["proposals"]
    assert isinstance(proposals, dict)
    action = proposals["items"]
    assert isinstance(action, dict)
    action_properties = action["properties"]
    assert isinstance(action_properties, dict)
    assert action_properties["kind"] == {"type": "string", "enum": ["edit_intent"]}
    assert (
        properties["non_mutating_result"]
        == json.loads(worker_proposal_schema_json())["properties"]["non_mutating_result"]
    )


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
        "task_type",
        "reasoning_class",
        "scope",
        "ambiguity",
        "reasons",
    ]


def test_version_one_semantic_assessment_remains_parseable() -> None:
    assessment = SemanticTaskAssessment.model_validate_json(
        '{"complexity":2,"scale":1,"reasons":["bounded change"]}',
        strict=True,
    )

    assert assessment.schema_version == "1"
    assert assessment.required_capabilities == ()


def test_semantic_profile_rejects_unknown_enums_and_extra_fields() -> None:
    valid = {
        "task_type": "mechanical",
        "reasoning_class": "simple",
        "scope": "bounded",
        "ambiguity": "low",
        "reasons": ["bounded operation"],
    }
    with pytest.raises(ValueError):
        SemanticTaskProfile.model_validate({**valid, "task_type": "unknown"})
    with pytest.raises(ValueError):
        SemanticTaskProfile.model_validate({**valid, "risk": 0})
    with pytest.raises(ValueError):
        SemanticTaskProfile.model_validate({**valid, "reasons": [""]})


def test_worker_proposal_schema_is_canonical_json() -> None:
    schema = json.loads(worker_proposal_schema_json())

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "schema_version",
        "proposals",
        "non_mutating_result",
        "assistant_note",
        "usage_json",
    ]
    assert schema["properties"]["non_mutating_result"]["anyOf"][1] == {"type": "null"}
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
    assert edit_proposal["properties"]["expected_artifact_kinds"]["items"] == {
        "type": "string",
        "pattern": "^[A-Za-z][A-Za-z0-9_.:-]{0,127}$",
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


def test_all_worker_transport_schemas_share_exact_evidence_ref_constraints() -> None:
    expected = {
        "type": "array",
        "maxItems": 64,
        "items": {"type": "string", "pattern": DIGEST_PATTERN},
    }

    def evidence_schema(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict) and "evidence_refs" in properties:
                found = properties["evidence_refs"]
                assert isinstance(found, dict)
                return found
            for nested in value.values():
                try:
                    return evidence_schema(nested)
                except LookupError:
                    pass
        elif isinstance(value, list):
            for nested in value:
                try:
                    return evidence_schema(nested)
                except LookupError:
                    pass
        raise LookupError("evidence_refs schema is absent")

    schemas = (
        json.loads(worker_proposal_schema_json()),
        _claude_envelope_schema(),
        _envelope_schema(),
    )
    assert [evidence_schema(schema) for schema in schemas] == [expected, expected, expected]


@pytest.mark.parametrize(
    ("refs", "expected_code"),
    [
        ((), None),
        (("a" * 64,), None),
        (("b" * 64,), StableFailureCode.TYPED_RESULT_EVIDENCE_UNAUTHORIZED),
        (("src/x.py:10",), StableFailureCode.TYPED_RESULT_MALFORMED),
        (("A" * 64,), StableFailureCode.TYPED_RESULT_MALFORMED),
        (("a" * 63,), StableFailureCode.TYPED_RESULT_MALFORMED),
        (("a" * 64, "a" * 64), StableFailureCode.TYPED_RESULT_MALFORMED),
        (tuple(f"{index:064x}" for index in range(65)), StableFailureCode.TYPED_RESULT_MALFORMED),
    ],
)
def test_cli_adapter_enforces_evidence_shape_and_authority(
    refs: tuple[str, ...], expected_code: StableFailureCode | None
) -> None:
    allowed = "a" * 64
    request = WorkerRequest(
        id="evidence-request",
        run_id="evidence-run",
        created_at=NOW,
        goal="produce a diagnosis",
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
        accepted_plan_digest=ZERO,
        node_id="diagnose",
        accepted_graph_revision_digest=ZERO,
        graph_run_id="graph-run",
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={"artifact_bytes": 10_000},
        accepted_feedback_digests=(allowed,),
    )
    output = json.dumps(
        {
            "schema_version": "2",
            "proposals": [],
            "non_mutating_result": {
                "schema_version": "2",
                "id": "diagnosis",
                "run_id": request.run_id,
                "created_at": "2026-01-01T00:00:00Z",
                "graph_run_id": request.graph_run_id,
                "worker_request_digest": request.content_digest,
                "node_id": request.node_id,
                "accepted_graph_revision_digest": request.accepted_graph_revision_digest,
                "generation": request.generation,
                "attempt": request.attempt,
                "logical_kind": "diagnosis",
                "media_type": "text/plain",
                "content": "File locations stay here: src/x.py:10.",
                "summary": "bounded diagnosis",
                "findings": ["source location is human-readable support"],
                "evidence_refs": refs,
            },
            "assistant_note": "",
            "usage_json": "{}",
        }
    ).encode()

    class OutputExecutor:
        def execute(
            self,
            process_request: ProcessRequest,
            _decision: PolicyDecision,
            _cancellation: object,
        ) -> ExecutionResult:
            return ExecutionResult(
                id="worker-process-result",
                run_id=process_request.run_id,
                created_at=NOW,
                request_digest=process_request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest="f" * 64,
                resource_usage={"stdout_bytes": len(output)},
            )

    adapter = CodexCliWorkerAdapter(
        OutputExecutor(),  # type: ignore[arg-type]
        lambda digest: output if digest == "f" * 64 else b"",
        allow_worker,
        run_id=request.run_id,
    )
    result = adapter.propose(request, Channel())  # type: ignore[arg-type]

    if expected_code is None:
        assert result.status == "succeeded"
        assert result.non_mutating_result is not None
        assert result.non_mutating_result.evidence_refs == refs
    else:
        assert result.status == "failed"
        assert result.failure is not None
        assert result.failure.code is expected_code
        assert result.non_mutating_result is None
        assert result.boundary_diagnostic is not None
        if expected_code is StableFailureCode.TYPED_RESULT_MALFORMED:
            assert "put file locations in content/findings" in (
                result.boundary_diagnostic.exception_message or ""
            )


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
    assert "non_mutating_result" in prompt["transport_instruction"]
    assert "copy every supplied binding exactly" in prompt["transport_instruction"]
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


class _NotCancelled:
    def cancelled(self) -> bool:
        return False


def _edit_envelope(unified_diff: str, paths: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-headerless",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-headerless",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": paths,
                        "summary": "Apply header-less file sections.",
                        "unified_diff": unified_diff,
                    },
                    "reason": "Implement the requested bounded edits.",
                }
            ],
        }
    )


def test_cli_worker_synthesizes_only_missing_file_delimiters_idempotently() -> None:
    delimited = (
        "diff --git a/kept.txt b/kept.txt\n"
        "--- a/kept.txt\n"
        "+++ b/kept.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )
    headerless = (
        "--- a/existing.txt\n"
        "+++ b/existing.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "--- /dev/null\n"
        "+++ b/created.txt\n"
        "@@ -0,0 +1 @@\n"
        "+created\n"
        "--- a/deleted.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-deleted\n"
    )

    envelope = _validate_worker_envelope(
        _edit_envelope(
            delimited + headerless,
            ("kept.txt", "existing.txt", "created.txt", "deleted.txt"),
        )
    )

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert payload.unified_diff.startswith(delimited)
    assert payload.unified_diff.count("diff --git a/kept.txt b/kept.txt\n") == 1
    assert (
        "diff --git a/existing.txt b/existing.txt\n--- a/existing.txt\n"
        in payload.unified_diff
    )
    assert "diff --git a/created.txt b/created.txt\n--- /dev/null\n" in payload.unified_diff
    assert (
        "diff --git a/deleted.txt b/deleted.txt\n--- a/deleted.txt\n"
        in payload.unified_diff
    )
    assert _normalize_unified_diff(payload.unified_diff) == payload.unified_diff


def test_cli_worker_strips_header_timestamps_and_preserves_crlf_delimiters() -> None:
    patch = (
        "--- a/timed.txt\t2026-09-02 00:00:00 +0000\r\n"
        "+++ b/timed.txt\t2026-09-02 00:01:00 +0000\r\n"
        "@@ -1 +1 @@\r\n"
        "-before\r\n"
        "+after\r\n"
    )

    assert _normalize_unified_diff(patch) == "diff --git a/timed.txt b/timed.txt\r\n" + patch


def test_cli_worker_preserves_explicit_rename_section_byte_for_byte() -> None:
    patch = (
        "diff --git a/old.txt b/new.txt\n"
        "similarity index 80%\n"
        "rename from old.txt\n"
        "rename to new.txt\n"
        "--- a/old.txt\n"
        "+++ b/new.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )

    assert _normalize_unified_diff(patch) == patch


def test_cli_worker_preserves_three_file_six_hunk_headerless_incident() -> None:
    paths = ("first.txt", "second.txt", "third.txt")
    patch = (
        "--- a/first.txt\n"
        "+++ b/first.txt\n"
        "@@ -1 +1 @@\n"
        "-first old one\n"
        "+first new one\n"
        "@@ -3 +3 @@\n"
        "-first old two\n"
        "+first new two\n"
        "--- a/second.txt\n"
        "+++ b/second.txt\n"
        "@@ -2 +2 @@\n"
        "-second old one\n"
        "+second new one\n"
        "@@ -4 +4 @@\n"
        "-second old two\n"
        "+second new two\n"
        "--- a/third.txt\n"
        "+++ b/third.txt\n"
        "@@ -5 +5 @@\n"
        "-third old one\n"
        "+third new one\n"
        "@@ -7 +7 @@\n"
        "-third old two\n"
        "+third new two\n"
    )
    expected = patch
    for path in paths:
        expected = expected.replace(
            f"--- a/{path}\n",
            f"diff --git a/{path} b/{path}\n--- a/{path}\n",
            1,
        )

    normalized = _normalize_unified_diff(patch)

    assert normalized.count("diff --git ") == 3
    assert normalized == expected


def test_cli_worker_does_not_split_header_shaped_hunk_content() -> None:
    patch = (
        "--- a/first.txt\n"
        "+++ b/first.txt\n"
        "@@ -1 +1 @@\n"
        "--- a/literal.txt\n"
        "+++ b/literal.txt\n"
        "--- a/second.txt\n"
        "+++ b/second.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    envelope = _validate_worker_envelope(_edit_envelope(patch, ("first.txt", "second.txt")))

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert payload.unified_diff.count("diff --git ") == 2
    assert "--- a/literal.txt\n+++ b/literal.txt\n" in payload.unified_diff
    assert "diff --git a/literal.txt b/literal.txt" not in payload.unified_diff


@pytest.mark.parametrize(
    "patch",
    (
        pytest.param('--- "a/file.txt"\n+++ "b/file.txt"\n', id="quoted"),
        pytest.param("--- \n+++ \n", id="empty"),
        pytest.param("--- /dev/null\n+++ /dev/null\n", id="double-dev-null"),
        pytest.param("--- a/file.txt\n+++ c/file.txt\n", id="bad-prefix"),
        pytest.param("--- a/../escape.txt\n+++ b/../escape.txt\n", id="traversal"),
        pytest.param("--- a//etc/passwd\n+++ b//etc/passwd\n", id="absolute"),
        pytest.param("--- a/old.txt\n+++ b/new.txt\n", id="rename"),
        pytest.param("--- a/orphan.txt\n@@ -1 +1 @@\n", id="non-adjacent-pair"),
    ),
)
def test_cli_worker_rejects_unsafe_or_unrecognized_headerless_paths(patch: str) -> None:
    with pytest.raises(ValueError, match="header-less unified diff"):
        _validate_worker_envelope(_edit_envelope(patch, ("safe.txt",)))


def test_codex_adapter_classifies_headerless_rename_as_malformed_envelope() -> None:
    output = json.loads(
        _edit_envelope("--- a/old.txt\n+++ b/new.txt\n", ("old.txt", "new.txt"))
    )
    output.update({"assistant_note": "", "usage_json": "{}"})
    executor = SuccessfulExecutor()
    executor.execute = lambda request, _decision, _cancel: ExecutionResult(  # type: ignore[method-assign]
        id="headerless-rename-process",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "",
        status="succeeded",
        duration_seconds=0.01,
        stdout_artifact_digest="1" * 64,
    )

    result = CodexCliWorkerAdapter(
        executor,
        lambda _digest: json.dumps(output).encode(),
        allow_worker,
        run_id="run-1",
    ).propose(worker_request(), Channel())  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is StableFailureCode.WORKER_PROTOCOL_ERROR
    assert result.boundary_diagnostic is not None
    assert result.boundary_diagnostic.code == "WORKER_ENVELOPE_MALFORMED"


_HEADERLESS_TWO_FILE_PATCH = (
    "--- a/first.txt\n"
    "+++ b/first.txt\n"
    "@@ -1 +1 @@\n"
    "-before one\n"
    "+after one\n"
    "--- a/second.txt\n"
    "+++ b/second.txt\n"
    "@@ -1 +1 @@\n"
    "-before two\n"
    "+after two\n"
)
_HEADERLESS_TWO_FILE_PATHS = ("first.txt", "second.txt")


def _headerless_workspace(tmp_path: Path) -> tuple[GitWorkspaceManager, WorkspaceSnapshot]:
    repository = tmp_path / "headerless-repo"
    repository.mkdir()
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "fleet@example.invalid"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Fleet Test"), check=True
    )
    (repository / "first.txt").write_text("before one\n")
    (repository / "second.txt").write_text("before two\n")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    manager = GitWorkspaceManager(
        tmp_path / "headerless-state",
        AtomicArtifactStore(tmp_path / "headerless-artifacts"),
    )
    snapshot = manager.create(
        WorkspaceRequest(
            id="workspace-headerless",
            run_id="run-1",
            created_at=NOW,
            repository=str(repository),
            base_commit=head,
        )
    )
    return manager, snapshot


def test_headerless_sections_apply_through_workspace_manager(tmp_path: Path) -> None:
    manager, snapshot = _headerless_workspace(tmp_path)
    envelope = _validate_worker_envelope(
        _edit_envelope(_HEADERLESS_TWO_FILE_PATCH, _HEADERLESS_TWO_FILE_PATHS)
    )
    request = envelope.proposals[0].payload
    assert isinstance(request, EditIntentRequest)

    result = manager.apply_edit(
        snapshot, request, allow_worker(request), _NotCancelled()  # type: ignore[arg-type]
    )

    assert result.status == "succeeded"
    assert result.resource_usage["changed_paths"] == _HEADERLESS_TWO_FILE_PATHS
    isolated = Path(snapshot.isolated_worktree)
    assert (isolated / "first.txt").read_text() == "after one\n"
    assert (isolated / "second.txt").read_text() == "after two\n"


def test_headerless_sections_reject_base_mismatch_through_workspace_manager(
    tmp_path: Path,
) -> None:
    manager, snapshot = _headerless_workspace(tmp_path)
    mismatched_patch = _HEADERLESS_TWO_FILE_PATCH.replace(
        "-before one\n", "-not the base\n", 1
    )
    envelope = _validate_worker_envelope(
        _edit_envelope(mismatched_patch, _HEADERLESS_TWO_FILE_PATHS)
    )
    request = envelope.proposals[0].payload
    assert isinstance(request, EditIntentRequest)

    result = manager.apply_edit(
        snapshot, request, allow_worker(request), _NotCancelled()  # type: ignore[arg-type]
    )

    assert result.failure is not None
    assert result.failure.code is StableFailureCode.PATCH_PREFLIGHT_FAILED
    isolated = Path(snapshot.isolated_worktree)
    assert (isolated / "first.txt").read_text() == "before one\n"
    assert (isolated / "second.txt").read_text() == "before two\n"


def test_headerless_sections_reject_declared_paths_mismatch_through_workspace_manager(
    tmp_path: Path,
) -> None:
    manager, snapshot = _headerless_workspace(tmp_path)
    envelope = _validate_worker_envelope(
        _edit_envelope(_HEADERLESS_TWO_FILE_PATCH, ("first.txt",))
    )
    request = envelope.proposals[0].payload
    assert isinstance(request, EditIntentRequest)

    result = manager.apply_edit(
        snapshot, request, allow_worker(request), _NotCancelled()  # type: ignore[arg-type]
    )

    assert result.failure is not None
    assert result.failure.code is StableFailureCode.INVALID_REQUEST
    isolated = Path(snapshot.isolated_worktree)
    assert (isolated / "first.txt").read_text() == "before one\n"
    assert (isolated / "second.txt").read_text() == "before two\n"


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


def test_cli_worker_repairs_missing_existing_file_context_markers() -> None:
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
                        "paths": ["example.css"],
                        "summary": "Update styles.",
                        "unified_diff": (
                            "diff --git a/example.css b/example.css\n"
                            "--- a/example.css\n"
                            "+++ b/example.css\n"
                            "@@ -1,3 +1,3 @@\n"
                            " :root{color:black}\n"
                            "body{margin:0}\n"
                            "-canvas{display:none}\n"
                            "+canvas{display:block}\n"
                        ),
                    },
                    "reason": "Implement the requested style.",
                }
            ],
        }
    )

    envelope = _validate_worker_envelope(raw)

    payload = envelope.proposals[0].payload
    assert isinstance(payload, EditIntentRequest)
    assert "\n body{margin:0}\n" in payload.unified_diff


@pytest.mark.parametrize(
    "hunk",
    (
        (
            "@@ -1,2 +1,2 @@\n"
            "-:root{color:black}\n"
            "body{margin:0}\n"
            "+:root{color:white}\n"
            "body{margin:1px}\n"
        ),
        ("@@ -1,3 +1,3 @@\n :root{color:black}\n-body{margin:0}\n+body{margin:1px}\n"),
    ),
)
def test_cli_worker_rejects_ambiguous_or_count_inconsistent_existing_hunks(
    hunk: str,
) -> None:
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
                        "paths": ["example.css"],
                        "summary": "Replace multiline styles.",
                        "unified_diff": (
                            "diff --git a/example.css b/example.css\n"
                            "--- a/example.css\n"
                            "+++ b/example.css\n" + hunk
                        ),
                    },
                    "reason": "Implement the requested style.",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="ambiguous or inconsistent line counts"):
        _validate_worker_envelope(raw)


def test_headerless_ambiguous_hunk_records_exact_protocol_classification() -> None:
    output = json.dumps(
        {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": "proposal-ambiguous",
                    "run_id": "run-1",
                    "created_at": "2026-01-01T00:00:00Z",
                    "worker_id": "worker-1",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": "edit-ambiguous",
                        "run_id": "run-1",
                        "created_at": "2026-01-01T00:00:00Z",
                        "paths": ["example.css"],
                        "summary": "Replace one style.",
                        "unified_diff": (
                            "--- a/example.css\n"
                            "+++ b/example.css\n"
                            "@@ -1,2 +1,2 @@\n"
                            " body{margin:0}\n"
                        ),
                    },
                    "reason": "Implement the requested style.",
                }
            ],
            "assistant_note": "",
            "usage_json": "{}",
        }
    ).encode()
    executor = SuccessfulExecutor()
    executor.execute = lambda request, _decision, _cancel: ExecutionResult(  # type: ignore[method-assign]
        id="ambiguous-process-success",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "",
        status="succeeded",
        duration_seconds=0.01,
        stdout_artifact_digest="1" * 64,
    )

    result = CodexCliWorkerAdapter(
        executor,
        lambda _digest: output,
        allow_worker,
        run_id="run-1",
    ).propose(worker_request(), Channel())  # type: ignore[arg-type]

    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is StableFailureCode.WORKER_PROTOCOL_ERROR
    assert result.boundary_diagnostic is not None
    assert result.boundary_diagnostic.process_status == "succeeded"
    assert result.boundary_diagnostic.code == "DIFF_HUNK_AMBIGUOUS", (
        result.boundary_diagnostic.code,
        result.boundary_diagnostic.exception_type,
        result.boundary_diagnostic.exception_message,
    )


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


@pytest.mark.parametrize(
    ("code", "status"),
    (
        (StableFailureCode.TIMEOUT, "failed"),
        (StableFailureCode.CANCELLED, "cancelled"),
        (StableFailureCode.SPAWN_FAILED, "failed"),
        (StableFailureCode.PROCESS_FAILED, "failed"),
        (StableFailureCode.POLICY_DENIED, "failed"),
        (StableFailureCode.APPROVAL_REQUIRED, "failed"),
    ),
)
def test_cli_worker_preserves_process_failure_and_diagnostics(
    code: StableFailureCode, status: str
) -> None:
    result = CodexCliWorkerAdapter(
        BoundProcessExecutor(code, status),
        lambda _digest: b"",
        allow_worker,
        run_id="run-1",
        timeout_seconds=30.0,
    ).propose(worker_request(), Channel())  # type: ignore[arg-type]

    assert result.status == status
    assert result.failure is not None and result.failure.code is code
    assert result.failure.code is not StableFailureCode.WORKER_UNAVAILABLE
    diagnostic = result.boundary_diagnostic
    assert diagnostic is not None
    assert diagnostic.code == code.value
    assert diagnostic.worker_result_digest == result.content_digest
    assert diagnostic.process_request_digest is not None
    assert diagnostic.process_result_digest is not None
    assert diagnostic.configured_timeout_seconds == 30.0
    assert diagnostic.effective_timeout_seconds == 12.0
    assert (diagnostic.stdout_bytes, diagnostic.stderr_bytes) == (17, 23)


@pytest.mark.parametrize(
    ("adapter_type", "output", "expected", "expected_failure"),
    (
        (
            CodexCliWorkerAdapter,
            b"",
            "WORKER_EMPTY_OUTPUT",
            StableFailureCode.WORKER_EMPTY_OUTPUT,
        ),
        (
            CodexCliWorkerAdapter,
            b"not json",
            "WORKER_ENVELOPE_MALFORMED",
            StableFailureCode.WORKER_PROTOCOL_ERROR,
        ),
        (
            ClaudeCodeCliWorkerAdapter,
            b"{}",
            "WORKER_STRUCTURED_OUTPUT_MISSING",
            StableFailureCode.WORKER_STRUCTURED_OUTPUT_MISSING,
        ),
        (
            CodexCliWorkerAdapter,
            json.dumps(
                {
                    "schema_version": "2",
                    "proposals": [],
                    "non_mutating_result": {"logical_kind": "diagnosis"},
                    "assistant_note": "",
                    "usage_json": "{}",
                }
            ).encode(),
            "TYPED_RESULT_MALFORMED",
            StableFailureCode.TYPED_RESULT_MALFORMED,
        ),
    ),
)
def test_cli_worker_distinguishes_protocol_failures(
    adapter_type: type[CodexCliWorkerAdapter],
    output: bytes,
    expected: str,
    expected_failure: StableFailureCode,
) -> None:
    executor = SuccessfulExecutor()
    executor.execute = lambda request, _decision, _cancel: ExecutionResult(  # type: ignore[method-assign]
        id="process-success",
        run_id=request.run_id,
        created_at=NOW,
        request_digest=request.content_digest or "",
        status="succeeded",
        duration_seconds=0.01,
        stdout_artifact_digest="1" * 64,
    )
    result = adapter_type(
        executor,
        lambda _digest: output,
        allow_worker,
        run_id="run-1",
    ).propose(worker_request(), Channel())  # type: ignore[arg-type]
    assert result.boundary_diagnostic is not None
    assert result.boundary_diagnostic.code == expected
    assert result.failure is not None
    assert result.failure.code is expected_failure


def test_coordinator_persists_and_projects_worker_boundary_diagnostic(
    tmp_path: Path,
) -> None:
    malformed = {
        "schema_version": "2",
        "proposals": [{"kind": "unsupported"}],
    }
    with SQLiteStore(tmp_path / "boundary.db") as store:
        coordinator = WorkCoordinator(
            store,
            DeterministicRuntime({}, store=store),
            FakeWorkspace(b""),  # type: ignore[arg-type]
            lambda _snapshot, _cancellation: ScriptedWorkerAdapter([malformed]),
            lambda _snapshot: (_ for _ in ()).throw(
                AssertionError("failed worker result must not create an executor")
            ),
            lambda _artifact: b"",
            (builtin_policy("work-boundary"),),
        )
        run = coordinator.start(
            "record a bounded worker failure",
            str(tmp_path),
            "a" * 40,
            worker_name="scripted",
            run_id="work-boundary",
        )
        diagnostics = store.list_records(
            "worker_boundary_diagnostic_v2",
            WorkerBoundaryDiagnostic,
            run_id=run.id,
        )
        view = inspect_work_run(store, run.id)

    assert run.status == "failed"
    assert run.failure_code == StableFailureCode.WORKER_PROTOCOL_ERROR.value
    assert len(diagnostics) == 1
    assert diagnostics[0].worker_result_id == run.worker_result_id
    assert diagnostics[0].worker_result_digest is not None
    assert view["worker"]["boundary_diagnostics"] == [diagnostics[0].model_dump(mode="json")]


def test_work_coordinator_accepts_bound_typed_result_with_authoritative_budget(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "typed.db")
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    try:
        coordinator = WorkCoordinator(
            store,
            DeterministicRuntime({}, store=store),
            FakeWorkspace(b""),  # type: ignore[arg-type]
            lambda _snapshot, _cancellation: ScriptedWorkerAdapter([]),
            lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
            lambda _artifact: b"",
            (builtin_policy("worker-run"),),
            artifact_store=artifacts,
        )
        request = WorkerRequest(
            id="typed-request",
            run_id="worker-run",
            created_at=NOW,
            goal="return a diagnosis",
            task_kind=GoalTaskKind.NON_MUTATING,
            processes_authorized=False,
            accepted_plan_digest=ZERO,
            node_id="diagnose",
            accepted_graph_revision_digest=ZERO,
            graph_run_id="graph-run",
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            remaining_budgets={"worker_turns": 1, "artifact_bytes": 1_024},
        )
        typed = NonMutatingResult(
            id="typed-result",
            run_id=request.run_id,
            created_at=NOW,
            graph_run_id=request.graph_run_id,
            worker_request_digest=request.content_digest or ZERO,
            node_id=request.node_id,
            accepted_graph_revision_digest=request.accepted_graph_revision_digest,
            generation=request.generation,
            attempt=request.attempt,
            logical_kind="diagnosis",
            media_type="text/plain",
            content="bounded diagnosis",
        )
        result = WorkerResult(
            id="typed-worker-result",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
            non_mutating_result=typed,
        )
        accepted = coordinator._accept_non_mutating_result(request, result, 0)

        missing_budget_request = WorkerRequest(
            **{
                **request.model_dump(exclude={"content_digest"}),
                "id": "missing-budget-request",
                "remaining_budgets": {"worker_turns": 1},
            }
        )
        missing_budget_typed = typed.model_copy(
            update={
                "worker_request_digest": missing_budget_request.content_digest,
            }
        )
        missing_budget_result = result.model_copy(
            update={
                "request_digest": missing_budget_request.content_digest,
                "non_mutating_result": missing_budget_typed,
            }
        )
        rejected = coordinator._accept_non_mutating_result(
            missing_budget_request, missing_budget_result, 0
        )
    finally:
        store.close()

    assert accepted is not None
    assert accepted.status == "accepted"
    assert accepted.failure_code is None
    assert accepted.artifact is not None
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.failure_code is StableFailureCode.ARTIFACT_BUDGET_INVALID
    assert rejected.failure_code is not StableFailureCode.TYPED_RESULT_UNBOUND


def test_malformed_evidence_refs_create_no_acceptance_or_partial_artifact(
    tmp_path: Path,
) -> None:
    run_id = "malformed-evidence-run"
    policy = builtin_policy(run_id)
    request = WorkerRequest(
        id="malformed-evidence-request",
        run_id=run_id,
        created_at=NOW,
        goal="produce a diagnosis",
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
        accepted_plan_digest=ZERO,
        node_id="diagnose",
        accepted_graph_revision_digest=ZERO,
        graph_run_id="graph-run",
        harness_digest=ZERO,
        effective_policy_digest=canonical_digest([policy.content_digest]),
        remaining_budgets={"worker_turns": 1, "artifact_bytes": 10_000},
    )
    malformed = {
        "schema_version": "2",
        "proposals": (),
        "non_mutating_result": {
            "schema_version": "2",
            "id": "malformed-diagnosis",
            "run_id": run_id,
            "created_at": NOW,
            "graph_run_id": request.graph_run_id,
            "worker_request_digest": request.content_digest,
            "node_id": request.node_id,
            "accepted_graph_revision_digest": request.accepted_graph_revision_digest,
            "generation": request.generation,
            "attempt": request.attempt,
            "logical_kind": "diagnosis",
            "media_type": "text/plain",
            "content": "Complete diagnosis with source locations.",
            "summary": "bounded diagnosis",
            "findings": ("src/x.py:10 contains the relevant branch",),
            "evidence_refs": ("src/x.py:10",),
        },
    }
    store = SQLiteStore(tmp_path / "malformed-evidence.db")
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    coordinator = WorkCoordinator(
        store,
        DeterministicRuntime({}, store=store),
        FakeWorkspace(b""),  # type: ignore[arg-type]
        lambda _snapshot, _cancellation: ScriptedWorkerAdapter([malformed]),
        lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
        lambda _artifact: b"",
        (policy,),
        artifact_store=artifacts,
    )
    try:
        run = coordinator.execute_node(
            request,
            (),
            str(tmp_path),
            "a" * 40,
            worker_name="scripted",
            capture_patch=False,
        )
        assert run.status == "failed"
        assert run.failure_code == StableFailureCode.TYPED_RESULT_MALFORMED.value
        assert not store.list_records(
            "non_mutating_result_acceptance_v2",
            NonMutatingResultAcceptance,
            run_id=run_id,
        )
        assert not store.list_records("artifact_descriptor_v2", ArtifactDescriptor, run_id=run_id)
    finally:
        store.close()


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


def test_bound_probe_failure_persists_an_exact_failed_worker_result(tmp_path: Path) -> None:
    run_id = "probe-failure-child"
    policy = builtin_policy(run_id)
    request = WorkerRequest(
        id="probe-failure-request",
        run_id=run_id,
        created_at=NOW,
        goal="diagnose without mutation",
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
        accepted_plan_digest=ZERO,
        node_id="diagnosis-node",
        graph_run_id="parent-run",
        accepted_graph_revision_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=canonical_digest([policy.content_digest]),
        remaining_budgets={"worker_turns": 1, "wall_seconds": 2.0},
    )
    with SQLiteStore(tmp_path / "probe-failure.db") as store:
        coordinator = WorkCoordinator(
            store,
            DeterministicRuntime({}, store=store),
            NoWorkspace(),  # type: ignore[arg-type]
            lambda _snapshot, _cancellation: CodexCliWorkerAdapter(
                CapturingExecutor(),
                lambda _digest: b"",
                allow_worker,
                run_id=run_id,
            ),
            lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
            lambda _artifact: b"",
            (policy,),
        )

        run = coordinator.execute_node(
            request,
            (),
            str(tmp_path),
            "a" * 40,
            worker_name="codex_cli",
            capture_patch=False,
        )

        assert run.status == "failed"
        assert run.worker_result_id is not None
        result = store.get("worker_result_v2", run.worker_result_id, WorkerResult)
        assert result.request_digest == request.content_digest
        assert result.status == "failed"
        assert result.failure is not None
        assert result.failure.code is StableFailureCode.POLICY_DENIED


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
            semantic_profile=SemanticTaskProfile(
                task_type=SemanticTaskType.MECHANICAL,
                reasoning_class=SemanticReasoningClass.SIMPLE,
                scope=SemanticScope.BOUNDED,
                ambiguity=SemanticAmbiguity.LOW,
                reasons=("bounded plan",),
            ),
            context_character_count=11,
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
        assert routing["assessment"]["context_character_count"] == 11
        assert routing["assessment"]["semantic_profile"]["task_type"] == "mechanical"
        assert routing["selected_strategy"]["backend"] == "codex_cli"
        assert routing["selected_strategy"]["model"] == "gpt-5.6-luna"
        assert routing["selected_strategy"]["effort"] == "medium"
        assert run.status == "planned"
        assert run.workspace_id is None
        assert store.load_work_checkpoint(run.id)[1]["status"] == "planned"


def test_node_verification_bindings_are_explicit_content_bound_and_one_to_one() -> None:
    run_id = "node-run"
    requirements = ("test", "lint", "typecheck")
    criteria = tuple(
        CompletionCriterion(
            id=f"criterion-{requirement}",
            description=f"{requirement} passes",
            verification_requirement_ids=(requirement,),
        )
        for requirement in requirements
    )
    requests = tuple(
        ProcessRequest(
            id=f"opaque-{uuid4().hex}",
            run_id=run_id,
            created_at=NOW,
            argv=(requirement,),
            purpose="offline verification",
        )
        for requirement in requirements
    )
    bindings = tuple(
        NodeVerificationBinding(
            id=f"binding-{requirement}",
            run_id=run_id,
            created_at=NOW,
            requirement_id=requirement,
            process_request_id=request.id,
            process_request_digest=request.content_digest or ZERO,
        )
        for requirement, request in zip(requirements, requests, strict=True)
    )
    assert _node_verification_configuration_is_valid(run_id, criteria, requests, bindings)

    rebound = requests[0].model_copy(update={"id": f"opaque-{uuid4().hex}"})
    rebound_binding = bindings[0].model_copy(
        update={"process_request_id": rebound.id, "content_digest": None}
    )
    assert _node_verification_configuration_is_valid(
        run_id,
        criteria,
        (rebound, *requests[1:]),
        (rebound_binding, *bindings[1:]),
    )

    unknown = bindings[0].model_copy(update={"requirement_id": "unknown", "content_digest": None})
    stale = bindings[0].model_copy(update={"process_request_digest": ZERO, "content_digest": None})
    duplicate = bindings[1].model_copy(update={"requirement_id": "test", "content_digest": None})
    ambiguous_request = requests[0].model_copy(update={"id": f"opaque-{uuid4().hex}"})
    ambiguous_binding = bindings[1].model_copy(
        update={
            "process_request_id": ambiguous_request.id,
            "process_request_digest": ambiguous_request.content_digest,
            "content_digest": None,
        }
    )
    invalid_configurations = (
        (requests, ()),
        (requests, bindings[:-1]),
        (requests, (unknown, *bindings[1:])),
        (requests, (stale, *bindings[1:])),
        (requests, (bindings[0], duplicate, bindings[2])),
        (
            (requests[0], ambiguous_request, requests[2]),
            (bindings[0], ambiguous_binding, bindings[2]),
        ),
    )
    assert all(
        not _node_verification_configuration_is_valid(
            run_id, criteria, invalid_requests, invalid_bindings
        )
        for invalid_requests, invalid_bindings in invalid_configurations
    )


def test_node_run_with_missing_binding_fails_closed_with_inspector_diagnostic(
    tmp_path: Path,
) -> None:
    run_id = "node-missing-binding"
    patch = (
        b"diff --git a/file.txt b/file.txt\n"
        b"--- a/file.txt\n"
        b"+++ b/file.txt\n"
        b"@@ -1 +1 @@\n-before\n+after\n"
    )
    policy = builtin_policy(run_id)
    store = SQLiteStore(tmp_path / "missing-binding.db")
    verification = ProcessRequest(
        id=f"opaque-{uuid4().hex}",
        run_id=run_id,
        created_at=NOW,
        argv=("python", "-m", "pytest"),
        purpose="offline verification",
    )
    request = WorkerRequest(
        id="accepted-node-request",
        run_id=run_id,
        created_at=NOW,
        goal="make a verified change",
        accepted_plan_digest=ZERO,
        node_id="fix",
        graph_run_id="graph-run",
        accepted_graph_revision_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=canonical_digest([policy.content_digest]),
        remaining_budgets={"worker_turns": 1},
    )
    coordinator = WorkCoordinator(
        store,
        DeterministicRuntime({}, store=store),
        FakeWorkspace(patch),  # type: ignore[arg-type]
        lambda _snapshot, _cancellation: ScriptedWorkerAdapter([WorkerProposalEnvelope()]),
        lambda _snapshot: SuccessfulExecutor(),  # type: ignore[return-value]
        lambda _descriptor: patch,
        (policy,),
        verification_requests=(verification,),
        allowed_processes=(verification.argv,),
    )
    try:
        run = coordinator.execute_node(
            request,
            (
                CompletionCriterion(
                    id="criterion-test",
                    description="tests pass",
                    verification_requirement_ids=("test",),
                    required_artifact_ids=("workspace_patch",),
                ),
            ),
            str(tmp_path),
            "a" * 40,
            worker_name="scripted",
        )
        assert run.status == "failed"
        assert run.failure_code == StableFailureCode.VERIFICATION_BINDING_INVALID.value
        assert run.acceptance_ledger_id is not None
        ledger = store.get("acceptance_ledger_v2", run.acceptance_ledger_id, AcceptanceLedger)
        assert ledger.criteria[0].disposition == "uncovered"
        view = inspect_work_run(store, run.id)
        diagnostic = next(
            item for item in view["events"] if item["kind"] == "node_verification_binding_rejected"
        )["details"]
        assert diagnostic["stable_failure_code"] == "VERIFICATION_BINDING_INVALID"
        assert diagnostic["graph_run_id"] == "graph-run"
        assert diagnostic["node_id"] == "fix"
        assert diagnostic["criteria"][0]["requirement_refs"] == ["test"]
        assert diagnostic["requests"][0]["request_id"] == verification.id
        assert diagnostic["bindings"] == []
    finally:
        store.close()


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


def test_verification_failure_preserves_worker_identity_and_candidate_patch(
    tmp_path: Path,
) -> None:
    patch = (
        b"diff --git a/file.txt b/file.txt\n"
        b"--- a/file.txt\n"
        b"+++ b/file.txt\n"
        b"@@ -1 +1 @@\n-before\n+after\n"
    )
    store = SQLiteStore(tmp_path / "fleet.db")
    workspace = FakeWorkspace(patch)
    verification = ProcessRequest(
        id="verify-1",
        run_id="work-1",
        created_at=NOW,
        argv=("ruff", "check"),
        purpose="offline verification",
    )
    coordinator = WorkCoordinator(
        store,
        DeterministicRuntime({}, store=store),
        workspace,  # type: ignore[arg-type]
        lambda _snapshot, _cancellation: ScriptedWorkerAdapter([WorkerProposalEnvelope()]),
        lambda _snapshot: FailingVerificationExecutor(),  # type: ignore[return-value]
        lambda _descriptor: patch,
        (builtin_policy("work-1"),),
        verification_requests=(verification,),
        allowed_processes=(verification.argv,),
    )
    try:
        run = coordinator.start(
            "make a verified change",
            str(tmp_path),
            "a" * 40,
            worker_name="scripted",
            run_id="work-1",
            _completion_criteria=(
                CompletionCriterion(
                    id="verified-patch",
                    description="candidate passes ruff",
                    verification_requirement_ids=("verify-1",),
                    required_artifact_ids=("workspace_patch",),
                ),
            ),
        )
        assert run.status == "failed"
        assert run.failure_code == "VERIFICATION_FAILED"
        assert run.worker_result_id is not None
        assert run.patch_artifact_id == "patch-1"
        assert run.acceptance_ledger_id is not None
        ledger = store.get("acceptance_ledger_v2", run.acceptance_ledger_id, AcceptanceLedger)
        assert ledger.criteria[0].disposition == "uncovered"
        assert len(run.verification_result_digests) == 1
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
        with pytest.raises(KeyError, match=run.id):
            explain_any_run(store, run.id)
        assert store.get_work_run(run.id) == run
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
