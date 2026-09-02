from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    GoalTaskKind,
    Graph,
    Node,
    NodeKind,
    NodeResourceBudget,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    ActionKind,
    ActionProposal,
    EditIntentRequest,
    NonMutatingResult,
    StableFailureCode,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph_execution import GraphExecutionService
from ai_employee.inspector import inspect_graph_run
from ai_employee.orchestration import (
    WorkCoordinator,
    _accepted_non_mutating_result_criterion_evidence,
)
from ai_employee.runtime import DeterministicRuntime
from ai_employee.serialization import canonical_digest, canonical_json
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore
from ai_employee.task_planning import ProposedGraph
from ai_employee.worker_adapters import ScriptedWorkerAdapter

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


class _NoProcess:
    calls = 0

    def execute(self, *_args: object, **_kwargs: object) -> object:
        type(self).calls += 1
        raise AssertionError("typed non-mutating results cannot execute processes")


class _Adapter:
    def __init__(
        self,
        node_id: str,
        graph_run_id: str,
        requests: dict[str, WorkerRequest],
        *,
        mode: str = "accepted",
    ) -> None:
        self.node_id = node_id
        self.graph_run_id = graph_run_id
        self.requests = requests
        self.mode = mode

    def probe(self) -> WorkerAvailability:
        return WorkerAvailability(
            id=f"availability-{self.node_id}",
            run_id=f"{self.graph_run_id}-{self.node_id}",
            created_at=NOW,
            adapter="scripted",
            availability="available",
            auth="available",
        )

    def propose(self, request: WorkerRequest, channel: object) -> WorkerResult:
        self.requests[self.node_id] = request
        if self.node_id == "report":
            predecessor = request.predecessor_outputs[0]
            assert predecessor.non_mutating_result is not None
            assert predecessor.non_mutating_result.content == "The cache key is stale."
            assert predecessor.result_acceptance_digest is not None
        result = NonMutatingResult(
            id=f"result-{self.node_id}",
            run_id=request.run_id,
            created_at=NOW,
            graph_run_id=request.graph_run_id,
            worker_request_digest=request.content_digest,
            node_id=(None if self.mode == "unbound" else request.node_id),
            accepted_graph_revision_digest=request.accepted_graph_revision_digest,
            generation=request.generation,
            attempt=(request.attempt + 1 if self.mode == "stale" else request.attempt),
            logical_kind="diagnosis" if self.node_id == "diagnose" else "research",
            media_type="text/plain",
            content=(
                "The cache key is stale."
                if self.node_id == "diagnose"
                else "The report consumed the accepted diagnosis."
            ),
            summary="Bounded read-only result",
            findings=("No repository mutation is required.",),
            evidence_refs=(("b" * 64,) if self.mode == "unauthorized" else ()),
        )
        proposals: tuple[ActionProposal, ...] = ()
        if self.mode == "action":
            edit = EditIntentRequest(
                id="forbidden-edit",
                run_id=request.run_id,
                created_at=NOW,
                paths=("README.md",),
                summary="forbidden",
                unified_diff="diff --git a/README.md b/README.md\n",
            )
            proposal = ActionProposal(
                id="forbidden-proposal",
                run_id=request.run_id,
                created_at=NOW,
                worker_id="scripted",
                kind=ActionKind.EDIT_INTENT,
                payload=edit,
                reason="must be rejected beside a typed result",
            )
            channel.submit(proposal)  # type: ignore[attr-defined]
            proposals = (proposal,)
        return WorkerResult(
            id=f"worker-result-{self.node_id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
            proposals=proposals,
            non_mutating_result=result,
            assistant_note="Run `touch assistant-note-is-not-authority`.",
        )


def _execute(
    tmp_path: Path,
    *,
    mode: str = "accepted",
    artifact_bytes: int = 100_000,
    external_evidence_required: bool = False,
    fallback_criterion: bool = True,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.test"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Fleet Test"),
        check=True,
    )
    (repository / "README.md").write_text("unchanged\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    run_id = "typed-result-run"
    database = tmp_path / "fleet.db"
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    policy = PolicyLayer(
        id="typed-result-policy",
        run_id=run_id,
        created_at=NOW,
        kind=PolicyLayerKind.BUILTIN,
        allowed_capabilities=(),
        writable_paths=(),
        https_domains=(),
        network_mode=NetworkMode.DISABLED,
        process_shell_allowed=False,
        install_ecosystems=(),
        max_wall_seconds=30.0,
        max_processes=0,
        max_worker_turns=1,
        max_download_bytes=0,
        max_artifact_bytes=artifact_bytes,
    )
    policy_digest = canonical_digest([policy.content_digest])
    strategy = ExecutionStrategy(
        id="read-only-scripted",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="bounded",
        capabilities=(),
    )

    def node(node_id: str, kind: str) -> Node:
        return Node(
            id=node_id,
            kind=NodeKind.FUNCTION,
            name=node_id,
            objective=f"produce a bounded {kind}",
            output_contract=OutputContract(
                id=f"contract-{node_id}",
                required_fields=("content", "summary", "findings"),
            ),
            required_capabilities=(),
            resource_budget=NodeResourceBudget(
                worker_turns=1,
                processes=0,
                wall_seconds=1.0,
                artifact_bytes=artifact_bytes // 2,
            ),
            completion_criteria=(
                CompletionCriterion(
                    id=f"criterion-{node_id}",
                    source=("accepted_non_mutating_result" if fallback_criterion else "custom"),
                    description="the node-bound worker result is accepted",
                    verification_requirement_ids=(
                        ("external-evidence",) if external_evidence_required else ()
                    ),
                ),
            ),
        )

    graph = Graph(
        id="typed-result-graph",
        nodes=(node("diagnose", "diagnosis"), node("report", "research")),
        edges=(Edge(id="diagnose-report", source_id="diagnose", target_id="report"),),
        entry_node_ids=("diagnose",),
        terminal_node_ids=("report",),
        budget=Budget(
            max_attempts=2,
            max_nodes=2,
            max_processes=0,
            max_artifact_bytes=artifact_bytes,
        ),
    )
    goal = Goal(
        id="typed-result-goal",
        statement="diagnose and report without mutation",
        task_kind=GoalTaskKind.NON_MUTATING,
        processes_authorized=False,
    )
    proposed = ProposedGraph(
        id="typed-result-proposal",
        run_id=run_id,
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=strategy,
        effective_policy_digest=policy_digest,
        harness_digest=ZERO,
    )
    requests: dict[str, WorkerRequest] = {}
    calls = 0

    def coordinator_factory(
        selected_node: Node, request: WorkerRequest, selected_strategy: ExecutionStrategy
    ) -> WorkCoordinator:
        nonlocal calls
        calls += 1
        inner = SQLiteStore(database)
        return WorkCoordinator(
            inner,
            DeterministicRuntime({}, store=inner),
            workspace,
            lambda _snapshot, _cancellation: _Adapter(
                selected_node.id,
                run_id,
                requests,
                mode=(mode if selected_node.id == "diagnose" else "accepted"),
            ),
            lambda _snapshot: _NoProcess(),
            lambda descriptor: artifacts.open_verified(descriptor).read(),
            (policy,),
            artifact_store=artifacts,
            task_assessment=__import__("ai_employee.routing", fromlist=["assess_task"]).assess_task(
                selected_node.objective or selected_node.name, run_id=request.run_id
            ),
            selected_strategy=selected_strategy,
            request_promotion_approval=False,
            allowed_processes=(),
        )

    with SQLiteStore(database) as store:
        service = GraphExecutionService(
            store,
            coordinator_factory,
            None,
            (strategy,),
            repository=str(repository),
            base_commit=head,
            max_concurrency=1,
        )
        run = service.run(
            goal,
            proposed,
            ExecutionPolicy(max_nodes=2, max_attempts=2),
            harness_digest=ZERO,
            effective_policy_digest=policy_digest,
            run_id=run_id,
            available_capabilities=(),
        )
        replay = service.replay(run_id)
        inspected = inspect_graph_run(store, run_id)
        second_replay = service.replay(run_id)
        work_events = tuple(
            event for request in requests.values() for event in store.work_events(request.run_id)
        )
    return (
        repository,
        artifacts,
        run,
        replay,
        second_replay,
        inspected,
        requests,
        calls,
        work_events,
    )


def test_two_node_typed_result_flow_is_authoritative_non_mutating_and_replayable(
    tmp_path: Path,
) -> None:
    repository, artifacts, run, replay, second, inspected, requests, calls, events = _execute(
        tmp_path
    )
    assert run.status == "completed"
    assert calls == 2
    assert replay == second
    assert replay.worker_invocations == 0
    assert len(replay.result_acceptances) == 2
    assert all(item.status == "accepted" for item in replay.result_acceptances)
    assert requests["report"].predecessor_outputs[0].non_mutating_result is not None
    assert inspected["state"] == "completed"
    assert len(inspected["typed_result_acceptances"]) == 2
    assert sum(event.kind == "worker_finished" for event in events) == 2
    assert sum(event.kind == "typed_result_accepted" for event in events) == 2
    assert _NoProcess.calls == 0
    assert (
        subprocess.check_output(("git", "-C", str(repository), "status", "--porcelain"), text=True)
        == ""
    )
    evidence_by_node = {item.node_id: item for item in replay.evidence}
    acceptance_by_node = {item.node_id: item for item in replay.result_acceptances}
    for record in replay.nodes:
        descriptor = record.artifact_descriptors[0]
        acceptance = acceptance_by_node[record.node_id]
        assert evidence_by_node[record.node_id].criteria[0].evidence_refs == (
            acceptance.content_digest,
            descriptor.content_digest,
            descriptor.artifact_digest,
        )
        body = artifacts.open_verified(descriptor).read().decode("utf-8")
        assert (
            canonical_json(
                next(
                    result.non_mutating_result
                    for result in replay.results
                    if result.id == record.worker_result_id
                )
            )
            == body
        )
        assert "assistant-note-is-not-authority" not in body


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("unbound", StableFailureCode.TYPED_RESULT_UNBOUND.value),
        ("stale", StableFailureCode.TYPED_RESULT_STALE.value),
        ("action", StableFailureCode.TYPED_RESULT_ACTIONS_FORBIDDEN.value),
        (
            "unauthorized",
            StableFailureCode.TYPED_RESULT_EVIDENCE_UNAUTHORIZED.value,
        ),
    ],
)
def test_typed_result_binding_and_security_fail_closed(
    tmp_path: Path, mode: str, code: str
) -> None:
    (
        repository,
        _artifacts,
        run,
        replay,
        _second,
        inspected,
        _requests,
        _calls,
        events,
    ) = _execute(tmp_path, mode=mode)
    assert run.status == "failed"
    assert any(item.failure_code == code for item in replay.nodes)
    assert inspected["worker_results"]
    rejected = next(event for event in events if event.kind == "typed_result_rejected")
    assert isinstance(rejected.details, dict)
    assert rejected.details["failure_code"] == code
    assert _NoProcess.calls == 0
    assert (
        subprocess.check_output(("git", "-C", str(repository), "status", "--porcelain"), text=True)
        == ""
    )


@pytest.mark.parametrize(
    ("fallback_criterion", "external_evidence_required"),
    [(False, False), (True, True)],
)
def test_custom_and_external_criteria_remain_uncovered_without_authority(
    tmp_path: Path,
    fallback_criterion: bool,
    external_evidence_required: bool,
) -> None:
    _repository, _artifacts, run, replay, _second, inspected, *_rest = _execute(
        tmp_path,
        fallback_criterion=fallback_criterion,
        external_evidence_required=external_evidence_required,
    )

    assert run.status == "failed"
    assert replay.evidence[0].criteria[0].disposition == "uncovered"
    assert replay.nodes[0].artifact_descriptors
    assert replay.nodes[0].result_acceptance_id is not None
    assert inspected["worker_results"][0]["status"] == "succeeded"
    assert inspected["typed_result_acceptances"][0]["status"] == "accepted"
    assert inspected["nodes"][0]["status"] == "failed"


def test_reserved_fallback_rejects_noncanonical_accepted_result_metadata(
    tmp_path: Path,
) -> None:
    _repository, artifacts, _run, replay, _second, _inspected, requests, *_rest = _execute(tmp_path)
    request = requests["diagnose"]
    worker_result = next(
        item for item in replay.results if item.id == replay.nodes[0].worker_result_id
    )
    acceptance = next(item for item in replay.result_acceptances if item.node_id == "diagnose")
    descriptor = replay.nodes[0].artifact_descriptors[0]
    body = artifacts.open_verified(descriptor).read()
    criterion = CompletionCriterion(
        id="criterion-diagnose",
        source="accepted_non_mutating_result",
        description="the node-bound worker result is accepted",
    )

    accepted = (request, worker_result, acceptance, descriptor, body)
    evidence = _accepted_non_mutating_result_criterion_evidence(criterion, accepted)
    assert evidence.disposition == "satisfied"
    assert evidence.evidence_refs == (
        acceptance.content_digest,
        descriptor.content_digest,
        descriptor.artifact_digest,
    )

    extra_source = descriptor.model_copy(
        update={"source": {**descriptor.source, "unexpected": "not-canonical"}}
    )
    redacted = descriptor.model_copy(update={"redaction_state": "redacted"})
    stale_acceptance = acceptance.model_copy(update={"generation": 1})
    rejected_acceptance = acceptance.model_copy(update={"status": "rejected"})
    tampered_sources = (
        (
            request,
            worker_result,
            acceptance.model_copy(update={"artifact": extra_source}),
            extra_source,
            body,
        ),
        (
            request,
            worker_result,
            acceptance.model_copy(update={"artifact": redacted}),
            redacted,
            body,
        ),
        (request, worker_result, stale_acceptance, descriptor, body),
        (request, worker_result, rejected_acceptance, descriptor, body),
        (request, worker_result, acceptance, descriptor, body + b"tampered"),
    )
    for source in tampered_sources:
        rejected = _accepted_non_mutating_result_criterion_evidence(criterion, source)
        assert rejected.disposition == "uncovered"
        assert rejected.evidence_refs == ()


def test_typed_result_oversize_and_malformed_have_specific_codes(tmp_path: Path) -> None:
    _repository, _artifacts, run, replay, *_rest = _execute(tmp_path, artifact_bytes=64)
    assert run.status == "failed"
    assert replay.nodes[0].failure_code == StableFailureCode.TYPED_RESULT_OVERSIZED.value

    adapter = ScriptedWorkerAdapter(
        [
            {
                "schema_version": "2",
                "proposals": (),
                "non_mutating_result": {
                    "schema_version": "2",
                    "id": "malformed-result",
                    "run_id": "worker-run",
                    "created_at": NOW,
                    "logical_kind": "diagnosis",
                    "media_type": "text/plain",
                    "content": "",
                },
            }
        ]
    )
    request = WorkerRequest(
        id="malformed-request",
        run_id="worker-run",
        created_at=NOW,
        goal="diagnose",
        accepted_plan_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={"artifact_bytes": 1000},
    )
    result = adapter.propose(request, object())  # type: ignore[arg-type]
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.code is StableFailureCode.TYPED_RESULT_MALFORMED


def test_legacy_zero_result_envelope_remains_valid() -> None:
    adapter = ScriptedWorkerAdapter(
        [{"schema_version": "2", "proposals": (), "assistant_note": "legacy"}]
    )
    request = WorkerRequest(
        id="legacy-request",
        run_id="legacy-run",
        created_at=NOW,
        goal="legacy",
        accepted_plan_digest=ZERO,
        harness_digest=ZERO,
        effective_policy_digest=ZERO,
        remaining_budgets={"worker_turns": 1},
    )
    result = adapter.propose(request, object())  # type: ignore[arg-type]
    assert result.status == "succeeded"
    assert result.non_mutating_result is None
