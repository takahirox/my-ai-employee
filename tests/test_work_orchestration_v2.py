from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    StableFailure,
    StableFailureCode,
    WorkerRequest,
)
from ai_employee.orchestration import WorkCoordinator
from ai_employee.runtime import DeterministicRuntime
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
        allowed_capabilities=("process",),
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
