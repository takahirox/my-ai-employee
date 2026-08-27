from __future__ import annotations

import subprocess
import threading
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
    Graph,
    HarnessCommand,
    HarnessEvaluator,
    HarnessVerification,
    Node,
    NodeKind,
    OutputContract,
    ProjectHarnessV2,
    RoutingMode,
)
from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    ActionKind,
    ActionProposal,
    ArtifactDescriptor,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    StableFailure,
    StableFailureCode,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph_composition import GraphPatchComposer
from ai_employee.graph_evaluation import (
    GraphCandidateEvaluator,
    ParentCandidateEvaluationRecord,
)
from ai_employee.graph_execution import GraphExecutionService
from ai_employee.orchestration import WorkCoordinator
from ai_employee.runtime import DeterministicRuntime
from ai_employee.serialization import canonical_digest
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("parent_verification_succeeds", [True, False, None])
def test_bounded_fork_join_executes_composes_and_replays_without_promotion(
    tmp_path: Path, parent_verification_succeeds: bool | None
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.test"), check=True
    )
    subprocess.run(("git", "-C", str(repository), "config", "user.name", "Fleet Test"), check=True)
    for name in ("a", "b", "c"):
        (repository / f"{name}.txt").write_text(f"{name}-before\n")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()

    database = tmp_path / "fleet.db"
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    requests: dict[str, WorkerRequest] = {}
    finished: set[str] = set()

    strategy = ExecutionStrategy(
        id="scripted-process",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="bounded",
        capabilities=("edit_intent", "process"),
    )

    def criterion(name: str) -> CompletionCriterion:
        return CompletionCriterion(
            id=f"criterion-{name}",
            description=f"{name} verification passed",
            verification_requirement_ids=(f"verify-{name}",),
            required_artifact_ids=("workspace_patch",),
        )

    def node(name: str) -> Node:
        return Node(
            id=name,
            kind=NodeKind.FUNCTION,
            name=name,
            objective=f"change {name}.txt",
            output_contract=OutputContract(id=f"contract-{name}"),
            required_capabilities=("edit_intent", "process"),
            completion_criteria=(criterion(name),),
            complexity=2 if name in {"a", "b"} else 3,
        )

    graph = Graph(
        id="bounded-fork-join",
        nodes=(node("a"), node("b"), node("c")),
        edges=(
            Edge(id="a-c", source_id="a", target_id="c"),
            Edge(id="b-c", source_id="b", target_id="c"),
        ),
        entry_node_ids=("a", "b"),
        terminal_node_ids=("c",),
        budget=Budget(max_attempts=3, max_nodes=3, max_wall_seconds=30.0),
    )
    goal = Goal(
        id="goal-bounded",
        statement="make three bounded changes",
        completion_criteria=(
            CompletionCriterion(
                id="parent-verification",
                description="the exact composed candidate passes its Harness verification",
                verification_requirement_ids=("parent-test",),
                required_artifact_ids=("workspace_patch",),
            ),
        ),
    )
    harness = ProjectHarnessV2(
        commands={"parent-test": HarnessCommand(argv=("verify-parent",))},
        evaluators=(
            HarnessEvaluator(
                id="parent-process-evaluator",
                provider_id="process.harness",
                command_ref="parent-test",
                criterion_ids=("parent-verification",),
            ),
        ),
        verification=HarnessVerification(
            required=("parent-test",),
            required_evaluators=("parent-process-evaluator",),
        ),
    )
    policy = PolicyLayer(
        id="policy-parent",
        run_id="graph-e2e",
        created_at=NOW,
        kind=PolicyLayerKind.BUILTIN,
        allowed_capabilities=("edit_intent", "process"),
        writable_paths=("**",),
        https_domains=(),
        network_mode=NetworkMode.DISABLED,
        process_shell_allowed=False,
        install_ecosystems=(),
        max_wall_seconds=30.0,
        max_processes=8,
        max_worker_turns=1,
        max_download_bytes=0,
        max_artifact_bytes=1_000_000,
    )
    effective_policy_digest = canonical_digest([policy.content_digest])
    harness_digest = canonical_digest(harness)
    proposal = ProposedGraph(
        id="proposal-bounded",
        run_id="graph-e2e",
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=strategy,
        effective_policy_digest=effective_policy_digest,
        harness_digest=harness_digest,
    )

    class Adapter:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def probe(self) -> WorkerAvailability:
            return WorkerAvailability(
                id=f"availability-{self.node_id}-{threading.get_ident()}",
                run_id=requests.get(self.node_id, proposal).run_id,
                created_at=NOW,
                adapter="scripted",
                availability="available",
                auth="available",
            )

        def propose(self, request: WorkerRequest, channel: object) -> WorkerResult:
            with lock:
                requests[self.node_id] = request
            if self.node_id in {"a", "b"}:
                barrier.wait(timeout=5)
            else:
                with lock:
                    assert finished == {"a", "b"}
            patch = (
                f"diff --git a/{self.node_id}.txt b/{self.node_id}.txt\n"
                f"--- a/{self.node_id}.txt\n"
                f"+++ b/{self.node_id}.txt\n"
                "@@ -1 +1 @@\n"
                f"-{self.node_id}-before\n"
                f"+{self.node_id}-after\n"
            )
            edit = EditIntentRequest(
                id=f"edit-{self.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                paths=(f"{self.node_id}.txt",),
                summary=f"change {self.node_id}",
                unified_diff=patch,
            )
            action = ActionProposal(
                id=f"proposal-{self.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                worker_id=f"worker-{self.node_id}",
                kind=ActionKind.EDIT_INTENT,
                payload=edit,
                reason="bounded fixture edit",
            )
            channel.submit(action)  # type: ignore[attr-defined]
            return WorkerResult(
                id=f"worker-result-{self.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or "0" * 64,
                status="succeeded",
                duration_seconds=0.01,
                proposals=(action,),
            )

    class Executor:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def execute(
            self, request: ProcessRequest, _decision: PolicyDecision, _cancellation: object
        ) -> ExecutionResult:
            with lock:
                finished.add(self.node_id)
            return ExecutionResult(
                id=f"verification-result-{self.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or "0" * 64,
                status="succeeded",
                exit_code=0,
                duration_seconds=0.01,
            )

    def coordinator_factory(
        selected_node: Node, request: WorkerRequest, selected_strategy: ExecutionStrategy
    ) -> WorkCoordinator:
        inner = SQLiteStore(database)
        verification = ProcessRequest(
            id=f"verify-{selected_node.id}",
            run_id=request.run_id,
            created_at=NOW,
            argv=("verify", selected_node.id),
            purpose=f"required Harness verification: {selected_node.id}",
        )
        return WorkCoordinator(
            inner,
            DeterministicRuntime({}, store=inner),
            workspace,
            lambda _snapshot, _cancellation: Adapter(selected_node.id),
            lambda _snapshot: Executor(selected_node.id),
            lambda descriptor: artifacts.open_verified(descriptor).read(),
            (policy,),
            task_assessment=__import__("ai_employee.routing", fromlist=["assess_task"]).assess_task(
                selected_node.objective or selected_node.name, run_id=request.run_id
            ),
            selected_strategy=selected_strategy,
            verification_requests=(verification,),
            allowed_processes=(verification.argv,),
            protected_paths=(".git/**",),
        )

    composition_calls = 0
    parent_process_calls = 0

    class ParentExecutor:
        def __init__(self, snapshot: object) -> None:
            self.snapshot = snapshot

        def execute(
            self, request: ProcessRequest, _decision: PolicyDecision, _cancellation: object
        ) -> ExecutionResult:
            nonlocal parent_process_calls
            parent_process_calls += 1
            isolated = Path(self.snapshot.isolated_worktree)  # type: ignore[attr-defined]
            assert isolated != repository
            assert all(
                (isolated / f"{name}.txt").read_text() == f"{name}-after\n"
                for name in ("a", "b", "c")
            )
            failure = None
            status = "succeeded"
            exit_code = 0
            if not parent_verification_succeeds:
                failure = StableFailure(
                    code=StableFailureCode.PROCESS_FAILED,
                    message="declared parent verification failed",
                )
                status = "failed"
                exit_code = 1
            return ExecutionResult(
                id="parent-verification-result",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or "0" * 64,
                status=status,  # type: ignore[arg-type]
                failure=failure,
                exit_code=exit_code,
                duration_seconds=0.01,
            )

    with SQLiteStore(database) as store:

        def allow_composition(edit: EditIntentRequest) -> PolicyDecision:
            return PolicyDecision(
                id=f"allow-{edit.id}",
                run_id=edit.run_id,
                created_at=NOW,
                request_digest=edit.content_digest or "0" * 64,
                effective_policy_digest=effective_policy_digest,
                outcome=DecisionOutcome.ALLOW,
                reason_code="composition_allowed",
            )

        real_composer = GraphPatchComposer(store, workspace, artifacts, allow_composition)

        def allow_parent_process(request: ProcessRequest) -> PolicyDecision:
            return PolicyDecision(
                id=f"allow-{request.id}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or "0" * 64,
                effective_policy_digest=effective_policy_digest,
                outcome=DecisionOutcome.ALLOW,
                reason_code="parent_verification_allowed",
            )

        parent_evaluator = GraphCandidateEvaluator(
            store, workspace, harness, ParentExecutor, allow_parent_process
        )

        class CountingComposer(GraphPatchComposer):
            def compose(self, request: object, cancellation: object) -> object:
                nonlocal composition_calls
                composition_calls += 1
                return real_composer.compose(request, cancellation)  # type: ignore[arg-type,return-value]

        service = GraphExecutionService(
            store,
            coordinator_factory,
            CountingComposer(store, workspace, artifacts, allow_composition),
            (strategy,),
            repository=str(repository),
            base_commit=head,
            max_concurrency=2,
            parent_evaluator=(None if parent_verification_succeeds is None else parent_evaluator),
        )
        run = service.run(
            goal,
            proposal,
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=harness_digest,
            effective_policy_digest=effective_policy_digest,
            run_id="graph-e2e",
            available_capabilities=("edit_intent", "process"),
        )

        assert run.status == ("ready_to_promote" if parent_verification_succeeds else "failed")
        assert run.failure_code == (
            None
            if parent_verification_succeeds
            else (
                "PARENT_EVALUATION_UNAVAILABLE"
                if parent_verification_succeeds is None
                else "PARENT_VERIFICATION_FAILED"
            )
        )
        assert (run.parent_evaluation_id is not None) == (parent_verification_succeeds is not None)
        assert run.composition_digest is not None
        assert run.parent_candidate_artifact_id is not None
        assert composition_calls == 1
        assert set(requests) == {"a", "b", "c"}
        assert requests["c"].prior_result_digests == tuple(
            requests[name].content_digest
            and store.get("worker_result_v2", f"worker-result-{name}", WorkerResult).content_digest
            for name in ("a", "b")
        )
        replay = service.replay(run.id)
        by_node = {item.node_id: item for item in replay.nodes}
        evaluator_by_id = {item.id: item for item in replay.evaluator_decisions}
        assert requests["c"].prior_artifact_digests == (
            by_node["a"].patch_digest,
            by_node["b"].patch_digest,
        )
        for reference in requests["c"].predecessor_outputs:
            predecessor = by_node[reference.node_id]
            evaluator = evaluator_by_id[reference.evaluator_id]
            assert reference.accepted_graph_revision_digest == (
                run.accepted_graph_revision_digest
            )
            assert reference.generation == predecessor.generation
            assert reference.result_generation == predecessor.output_generation
            assert reference.attempt == predecessor.attempt
            assert reference.worker_result_id == predecessor.worker_result_id
            assert reference.worker_result_digest == predecessor.worker_result_digest
            assert reference.artifact_descriptor_id == predecessor.patch_artifact_id
            assert reference.artifact_descriptor_digest == (
                predecessor.patch_descriptor_digest
            )
            assert reference.artifact_digest == predecessor.patch_digest
            assert reference.evaluator_id == predecessor.evaluator_id
            assert reference.evaluator_digest == predecessor.evaluator_digest
            assert evaluator.node_id == predecessor.node_id
            assert evaluator.generation == predecessor.output_generation
            assert evaluator.attempt == predecessor.attempt
        for predecessor in (by_node["a"], by_node["b"]):
            descriptor = store.get(
                "artifact_descriptor_v2",
                predecessor.patch_artifact_id or "missing",
                ArtifactDescriptor,
            )
            inner_run = store.get_work_run(predecessor.work_run_id or "missing")
            assert descriptor.run_id == inner_run.id == predecessor.work_run_id
            assert descriptor in store.list_records(
                "artifact_descriptor_v2", ArtifactDescriptor, run_id=inner_run.id
            )
        assert len({by_node[name].workspace_id for name in ("a", "b")}) == 2
        assert all(item.status == "passed" for item in replay.nodes)
        assert service.replay(run.id) == replay
        assert composition_calls == 1
        assert parent_process_calls == (0 if parent_verification_succeeds is None else 1)
        if run.parent_evaluation_id is not None:
            parent_record = store.get(
                "parent_candidate_evaluation_v2",
                run.parent_evaluation_id,
                ParentCandidateEvaluationRecord,
            )
            assert parent_record.status == run.status
            parent_replay = parent_evaluator.replay(parent_record.id)
            assert parent_replay.record == parent_record
            assert parent_replay.process_invocations == 0
            assert parent_replay.composition_invocations == 0
            assert parent_replay.promotion_invocations == 0
        assert composition_calls == 1
        candidate = store.get(
            "artifact_descriptor_v2",
            run.parent_candidate_artifact_id,
            __import__("ai_employee.domain.v2", fromlist=["ArtifactDescriptor"]).ArtifactDescriptor,
        )
        body = artifacts.open_verified(candidate).read().decode()
        assert body.count("diff --git") == 3
        assert all(f"+{name}-after" in body for name in ("a", "b", "c"))
        assert all(
            (repository / f"{name}.txt").read_text() == f"{name}-before\n"
            for name in ("a", "b", "c")
        )
        assert (
            store.list_records(
                "promotion_v2",
                PromotionRecord,
                run_id=run.id,
            )
            == ()
        )
