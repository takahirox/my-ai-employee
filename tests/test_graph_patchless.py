from __future__ import annotations

import io
import json
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.evaluation import EvaluationDecision
from ai_employee.domain.models import AcceptedGraphRevision
from ai_employee.domain.policy_v2 import NetworkMode, PolicyLayer, PolicyLayerKind
from ai_employee.domain.v2 import (
    ActionKind,
    ActionProposal,
    ApprovalRecord,
    ApprovalRequest,
    ArtifactDescriptor,
    ArtifactPutRequest,
    CriterionEvidence,
    DecisionOutcome,
    EditIntentRequest,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
    PromotionRecord,
    WorkerAvailability,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph_composition import (
    GraphPatchComposer,
    GraphPatchCompositionRecord,
    GraphPatchCompositionRequest,
)
from ai_employee.graph_evaluation import ParentCandidateEvaluationRecord
from ai_employee.graph_execution import GraphExecutionService
from ai_employee.inspector import inspect_graph_run
from ai_employee.orchestration import WorkCoordinator
from ai_employee.runtime import DeterministicRuntime
from ai_employee.serialization import canonical_digest
from ai_employee.services_v2 import AtomicArtifactStore, GitWorkspaceManager
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GoalEvaluatorRecord,
    GraphReplay,
    GraphRunRecord,
    NodeExecutionResult,
    TaskOrchestrator,
)
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64


def _repository(tmp_path: Path, names: tuple[str, ...] = ()) -> tuple[Path, str]:
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
    for name in names:
        (repository / f"{name}.txt").write_text(f"{name}-before\n", encoding="utf-8")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "base"), check=True)
    head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    return repository, head


def _policy(run_id: str) -> PolicyLayer:
    return PolicyLayer(
        id=f"policy-{run_id}",
        run_id=run_id,
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


def _strategy() -> ExecutionStrategy:
    return ExecutionStrategy(
        id="scripted-process",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="scripted",
        model="fixture",
        effort="bounded",
        capabilities=("edit_intent", "process"),
    )


class _BodyReader:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _descriptor: ArtifactDescriptor) -> bytes:
        self.calls += 1
        raise AssertionError("patchless execution must not open artifact bodies")


class _ExplodingServices:
    def __init__(self) -> None:
        self.calls = {"composition": 0, "evaluation": 0, "approval": 0}

    def compose(self, *_args: object, **_kwargs: object) -> object:
        self.calls["composition"] += 1
        raise AssertionError("patchless execution must not compose")

    def evaluate(self, *_args: object, **_kwargs: object) -> object:
        self.calls["evaluation"] += 1
        raise AssertionError("patchless execution must not evaluate a parent patch")

    def request(self, *_args: object, **_kwargs: object) -> object:
        self.calls["approval"] += 1
        raise AssertionError("patchless execution must not request promotion approval")


class _ProcessExecutor:
    def __init__(
        self,
        artifacts: AtomicArtifactStore,
        node_id: str,
        finished: set[str],
        lock: threading.Lock,
    ) -> None:
        self.artifacts = artifacts
        self.node_id = node_id
        self.finished = finished
        self.lock = lock
        self.descriptors: dict[tuple[str, str, str], ArtifactDescriptor] = {}

    def execute(
        self,
        request: ProcessRequest,
        _decision: PolicyDecision,
        _cancellation: object,
    ) -> ExecutionResult:
        execution_id = f"execution-{self.node_id}"
        body = f"authoritative output for {self.node_id}\n".encode()
        descriptor = self.artifacts.put(
            io.BytesIO(body),
            ArtifactPutRequest(
                id=f"put-{self.node_id}",
                run_id=request.run_id,
                created_at=NOW,
                media_type="text/plain",
                logical_kind="process_stdout",
                producer_action_id=request.id,
                source={
                    "request_digest": request.content_digest,
                    "execution_id": execution_id,
                    "bounded": True,
                },
            ),
        )
        self.descriptors[(descriptor.artifact_digest, descriptor.logical_kind, execution_id)] = (
            descriptor
        )
        with self.lock:
            self.finished.add(self.node_id)
        return ExecutionResult(
            id=execution_id,
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            exit_code=0,
            duration_seconds=0.01,
            stdout_artifact_digest=descriptor.artifact_digest,
        )

    def output_descriptor(
        self,
        artifact_digest: str,
        logical_kind: str,
        producer_execution_id: str,
    ) -> ArtifactDescriptor:
        return self.descriptors[(artifact_digest, logical_kind, producer_execution_id)]


class _ProcessAdapter:
    def __init__(
        self,
        node_id: str,
        run_id: str,
        barrier: threading.Barrier | None,
        finished: set[str],
        lock: threading.Lock,
        requests: dict[str, WorkerRequest],
    ) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.barrier = barrier
        self.finished = finished
        self.lock = lock
        self.requests = requests

    def probe(self) -> WorkerAvailability:
        return WorkerAvailability(
            id=f"availability-{self.node_id}-{threading.get_ident()}",
            run_id=self.run_id,
            created_at=NOW,
            adapter="scripted",
            availability="available",
            auth="available",
        )

    def propose(self, request: WorkerRequest, channel: object) -> WorkerResult:
        with self.lock:
            self.requests[self.node_id] = request
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if self.node_id == "join":
            with self.lock:
                assert self.finished >= {"a", "b"}
        process = ProcessRequest(
            id=f"process-{self.node_id}",
            run_id=request.run_id,
            created_at=NOW,
            argv=("produce", self.node_id),
            purpose=f"produce typed facts for {self.node_id}",
        )
        action = ActionProposal(
            id=f"action-{self.node_id}",
            run_id=request.run_id,
            created_at=NOW,
            worker_id=f"worker-{self.node_id}",
            kind=ActionKind.PROCESS,
            payload=process,
            reason="produce a mediated process artifact",
            expected_artifact_kinds=("process_stdout",),
        )
        channel.submit(action)  # type: ignore[attr-defined]
        return WorkerResult(
            id=f"worker-result-{self.node_id}",
            run_id=request.run_id,
            created_at=NOW,
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
            proposals=(action,),
        )


@dataclass
class _PatchlessExecution:
    repository: Path
    database: Path
    head: str
    run: GraphRunRecord
    replay: GraphReplay
    inspected: dict[str, object]
    requests: dict[str, WorkerRequest]
    body_reader: _BodyReader
    services: _ExplodingServices
    strategy: ExecutionStrategy


def _execute_patchless(tmp_path: Path, run_id: str) -> _PatchlessExecution:
    repository, head = _repository(tmp_path)
    database = tmp_path / "fleet.db"
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    policy = _policy(run_id)
    policy_digest = canonical_digest([policy.content_digest])
    strategy = _strategy()
    requests: dict[str, WorkerRequest] = {}
    finished: set[str] = set()
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    body_reader = _BodyReader()
    services = _ExplodingServices()

    def node(name: str) -> Node:
        return Node(
            id=name,
            kind=NodeKind.FUNCTION,
            name=name,
            objective=f"produce {name} facts",
            output_contract=OutputContract(id=f"contract-{name}"),
            required_capabilities=("process",),
            completion_criteria=(
                CompletionCriterion(
                    id=f"criterion-{name}",
                    description=f"{name} produced authoritative stdout",
                    required_artifact_ids=("process_stdout",),
                ),
            ),
        )

    graph = Graph(
        id="patchless-fork-join",
        nodes=(node("a"), node("b"), node("join")),
        edges=(
            Edge(id="a-join", source_id="a", target_id="join"),
            Edge(id="b-join", source_id="b", target_id="join"),
        ),
        entry_node_ids=("a", "b"),
        terminal_node_ids=("join",),
        budget=Budget(max_attempts=3, max_nodes=3, max_wall_seconds=30.0),
    )
    goal = Goal(
        id="goal-patchless",
        statement="produce and join process facts",
        completion_criteria=(
            CompletionCriterion(
                id="criterion-join",
                description="the join consumed authoritative predecessor facts",
            ),
        ),
    )
    proposal = ProposedGraph(
        id="proposal-patchless",
        run_id=run_id,
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=strategy,
        effective_policy_digest=policy_digest,
        harness_digest=ZERO,
    )

    def coordinator_factory(
        selected_node: Node,
        request: WorkerRequest,
        selected_strategy: ExecutionStrategy,
    ) -> WorkCoordinator:
        inner = SQLiteStore(database)
        executor = _ProcessExecutor(artifacts, selected_node.id, finished, lock)
        node_barrier = barrier if selected_node.id in {"a", "b"} else None
        return WorkCoordinator(
            inner,
            DeterministicRuntime({}, store=inner),
            workspace,
            lambda _snapshot, _cancellation: _ProcessAdapter(
                selected_node.id,
                request.run_id,
                node_barrier,
                finished,
                lock,
                requests,
            ),
            lambda _snapshot: executor,
            body_reader,
            (policy,),
            task_assessment=__import__(
                "ai_employee.routing", fromlist=["assess_task"]
            ).assess_task(selected_node.objective or selected_node.name, run_id=request.run_id),
            selected_strategy=selected_strategy,
            request_promotion_approval=False,
            allowed_processes=(("produce", selected_node.id),),
        )

    with SQLiteStore(database) as store:
        service = GraphExecutionService(
            store,
            coordinator_factory,
            services,  # type: ignore[arg-type]
            (strategy,),
            repository=str(repository),
            base_commit=head,
            max_concurrency=2,
            parent_evaluator=services,  # type: ignore[arg-type]
            approval_service=services,  # type: ignore[arg-type]
        )
        run = service.run(
            goal,
            proposal,
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest=policy_digest,
            run_id=run_id,
            available_capabilities=("edit_intent", "process"),
        )
        replay = service.replay(run_id)
        inspected = inspect_graph_run(store, run_id)

    return _PatchlessExecution(
        repository=repository,
        database=database,
        head=head,
        run=run,
        replay=replay,
        inspected=inspected,
        requests=requests,
        body_reader=body_reader,
        services=services,
        strategy=strategy,
    )


def test_patchless_fork_join_persists_body_free_artifact_evidence(tmp_path: Path) -> None:
    execution = _execute_patchless(tmp_path, "patchless-run")

    assert execution.run.status == "completed"
    assert execution.run.failure_code is None
    assert execution.run.composition_id is None
    assert execution.run.parent_evaluation_id is None
    assert execution.run.promotion_approval_id is None
    assert set(execution.requests) == {"a", "b", "join"}
    join_request = execution.requests["join"]
    assert join_request.prior_result_digests == tuple(
        reference.worker_result_digest for reference in join_request.predecessor_outputs
    )
    assert {reference.node_id for reference in join_request.predecessor_outputs} == {"a", "b"}
    assert all(reference.artifact_descriptors for reference in join_request.predecessor_outputs)
    assert join_request.prior_artifact_digests == tuple(
        artifact.artifact_digest
        for reference in join_request.predecessor_outputs
        for artifact in reference.artifact_descriptors
    )

    replay_by_node = {item.node_id: item for item in execution.replay.nodes}
    assert all(item.status == "passed" for item in replay_by_node.values())
    assert all(
        item.evaluator_decision is EvaluationDecision.PASS
        for item in replay_by_node.values()
    )
    for reference in join_request.predecessor_outputs:
        predecessor = replay_by_node[reference.node_id]
        assert reference.worker_result_id == predecessor.worker_result_id
        assert reference.worker_result_digest == predecessor.worker_result_digest
        assert reference.artifact_descriptors == tuple(
            item
            for descriptor in predecessor.artifact_descriptors
            for item in reference.artifact_descriptors
            if item.descriptor_id == descriptor.id
        )
        assert reference.artifact_descriptor_id == predecessor.artifact_descriptors[0].id
        assert (
            reference.artifact_descriptor_digest
            == predecessor.artifact_descriptors[0].content_digest
        )

    with SQLiteStore(execution.database) as store:
        goal_records = store.list_records(
            "goal_evaluator_v2", GoalEvaluatorRecord, run_id=execution.run.id
        )
        assert len(goal_records) == 1
        goal_record = goal_records[0]
        assert goal_record.decision is EvaluationDecision.PASS
        all_descriptors = tuple(
            descriptor
            for node_record in execution.replay.nodes
            for descriptor in node_record.artifact_descriptors
        )
        assert goal_record.artifact_descriptor_digests == tuple(
            descriptor.content_digest for descriptor in all_descriptors
        )
        assert goal_record.artifact_content_digests == tuple(
            descriptor.artifact_digest for descriptor in all_descriptors
        )

        for node_id, request in execution.requests.items():
            worker_result = store.get(
                "worker_result_v2", f"worker-result-{node_id}", WorkerResult
            )
            action = worker_result.proposals[0]
            action_results = store.list_records(
                "action_result_v2", ExecutionResult, run_id=request.run_id
            )
            assert len(action_results) == 1
            result = action_results[0]
            descriptor = replay_by_node[node_id].artifact_descriptors[0]
            assert isinstance(action.payload, ProcessRequest)
            assert descriptor.producer_action_id == action.payload.id
            assert descriptor.source["request_digest"] == action.payload.content_digest
            assert descriptor.source["execution_id"] == result.id
            assert result.stdout_artifact_digest == descriptor.artifact_digest

        assert store.list_records(
            "approval_request_v2", ApprovalRequest, run_id=execution.run.id
        ) == ()
        assert store.list_records(
            "approval_v2", ApprovalRecord, run_id=execution.run.id
        ) == ()
        assert store.list_records(
            "promotion_v2", PromotionRecord, run_id=execution.run.id
        ) == ()

    assert execution.replay.worker_invocations == 0
    assert execution.replay.verification_invocations == 0
    assert execution.replay.composition_invocations == 0
    assert execution.replay.promotion_invocations == 0
    assert execution.body_reader.calls == 0
    assert execution.services.calls == {"composition": 0, "evaluation": 0, "approval": 0}
    assert execution.inspected["state"] == "completed"
    assert len(execution.inspected["artifact_descriptors"]) == 3  # type: ignore[arg-type]
    assert all(
        "body" not in descriptor
        for descriptor in execution.inspected["artifact_descriptors"]  # type: ignore[union-attr]
    )


def _boundary_graph() -> tuple[Goal, Graph, ExecutionStrategy]:
    criterion = CompletionCriterion(
        id="criterion-only",
        description="stdout is authoritative",
        required_artifact_ids=("process_stdout",),
    )
    node = Node(
        id="only",
        kind=NodeKind.FUNCTION,
        name="only",
        objective="produce one fact",
        output_contract=OutputContract(id="contract-only"),
        required_capabilities=("process",),
        completion_criteria=(criterion,),
    )
    graph = Graph(
        id="boundary-graph",
        nodes=(node,),
        entry_node_ids=(node.id,),
        terminal_node_ids=(node.id,),
        budget=Budget(max_attempts=1, max_nodes=1),
    )
    goal = Goal(
        id="goal-boundary",
        statement="accept only authoritative facts",
        completion_criteria=(
            CompletionCriterion(id=criterion.id, description="the node criterion passed"),
        ),
    )
    return goal, graph, _strategy()


def _descriptor(index: int, run_id: str) -> ArtifactDescriptor:
    body = f"artifact-{index}".encode()
    digest = sha256(body).hexdigest()
    return ArtifactDescriptor(
        id=f"artifact-{index}",
        run_id=run_id,
        created_at=NOW,
        artifact_digest=digest,
        media_type="text/plain",
        size_bytes=len(body),
        logical_kind="process_stdout",
        producer_action_id=f"process-{index}",
        source={"execution_id": f"execution-{index}"},
        store_locator=f"sha256/{digest[:2]}/{digest}",
    )


@pytest.mark.parametrize("case", ["tampered", "unpersisted", "wrong_run"])
def test_patchless_descriptor_provenance_failures_are_closed(
    tmp_path: Path, case: str
) -> None:
    goal, graph, strategy = _boundary_graph()
    database = tmp_path / f"{case}.db"

    with SQLiteStore(database) as store:

        def runner(
            _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
        ) -> NodeExecutionResult:
            descriptor = _descriptor(1, request.run_id)
            if case == "wrong_run":
                descriptor = _descriptor(1, "another-run")
                with SQLiteStore(database) as writer:
                    writer.put(
                        "artifact_descriptor_v2", descriptor, run_id=request.run_id
                    )
            elif case == "tampered":
                with SQLiteStore(database) as writer:
                    writer.put(
                        "artifact_descriptor_v2", descriptor, run_id=request.run_id
                    )
                descriptor = descriptor.model_copy(update={"source": {"tampered": True}})
            worker = WorkerResult(
                id="worker-result-only",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            )
            return NodeExecutionResult(
                worker_result=worker,
                criterion_evidence=(
                    CriterionEvidence(
                        criterion_id="criterion-only",
                        disposition="satisfied",
                        evidence_refs=(
                            descriptor.content_digest or ZERO,
                            descriptor.artifact_digest,
                        ),
                    ),
                ),
                artifact_descriptors=(descriptor,),
            )

        orchestrator = TaskOrchestrator(
            store,
            runner,
            (strategy,),
            bounded_graph_execution=True,
            defer_parent_evaluation=True,
        )
        run = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1),
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            run_id=f"boundary-{case}",
            available_capabilities=("edit_intent", "process"),
        )
        replay = orchestrator.replay(run.id)

    assert run.status == "failed"
    assert replay.nodes[0].status == "failed"
    expected_error = "ValidationError" if case == "tampered" else "ValueError"
    assert replay.nodes[0].failure_code == f"WORKER_BOUNDARY:{expected_error}"


@pytest.mark.parametrize("case", ["duplicate_kind", "missing_descriptor_digest"])
def test_patchless_criterion_artifact_ambiguity_fails_evaluation(
    tmp_path: Path, case: str
) -> None:
    goal, graph, strategy = _boundary_graph()
    database = tmp_path / f"{case}.db"

    with SQLiteStore(database) as store:

        def runner(
            _node: Node, request: WorkerRequest, _strategy: ExecutionStrategy
        ) -> NodeExecutionResult:
            descriptors = (
                (_descriptor(1, request.run_id), _descriptor(2, request.run_id))
                if case == "duplicate_kind"
                else (_descriptor(1, request.run_id),)
            )
            with SQLiteStore(database) as writer:
                for descriptor in descriptors:
                    writer.put(
                        "artifact_descriptor_v2", descriptor, run_id=request.run_id
                    )
            refs = tuple(
                reference
                for descriptor in descriptors
                for reference in (descriptor.content_digest, descriptor.artifact_digest)
                if reference is not None
            )
            if case == "missing_descriptor_digest":
                refs = (descriptors[0].artifact_digest,)
            return NodeExecutionResult(
                worker_result=WorkerResult(
                    id="worker-result-only",
                    run_id=request.run_id,
                    created_at=NOW,
                    request_digest=request.content_digest or ZERO,
                    status="succeeded",
                    duration_seconds=0.01,
                ),
                criterion_evidence=(
                    CriterionEvidence(
                        criterion_id="criterion-only",
                        disposition="satisfied",
                        evidence_refs=refs,
                    ),
                ),
                artifact_descriptors=descriptors,
            )

        orchestrator = TaskOrchestrator(
            store,
            runner,
            (strategy,),
            bounded_graph_execution=True,
            defer_parent_evaluation=True,
        )
        run = orchestrator.run(
            goal,
            graph,
            ExecutionPolicy(max_nodes=1, max_attempts=1),
            harness_digest=ZERO,
            effective_policy_digest=ZERO,
            run_id=f"evaluation-{case}",
            available_capabilities=("edit_intent", "process"),
        )
        replay = orchestrator.replay(run.id)

    assert run.status == "failed"
    assert replay.nodes[0].status == "failed"
    assert replay.nodes[0].evaluator_decision is EvaluationDecision.FAIL
    assert replay.nodes[0].failure_code == "NODE_EVALUATION_NOT_PASS"


class _EditAdapter:
    def __init__(self, node_id: str, run_id: str, requests: dict[str, WorkerRequest]) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.requests = requests

    def probe(self) -> WorkerAvailability:
        return WorkerAvailability(
            id=f"availability-{self.node_id}",
            run_id=self.run_id,
            created_at=NOW,
            adapter="scripted",
            availability="available",
            auth="available",
        )

    def propose(self, request: WorkerRequest, channel: object) -> WorkerResult:
        self.requests[self.node_id] = request
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
            id=f"action-{self.node_id}",
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
            request_digest=request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
            proposals=(action,),
        )


class _UnusedProcessExecutor:
    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("writing nodes have no process verification in this fixture")


class _RecordingComposer:
    def __init__(self, delegate: GraphPatchComposer) -> None:
        self.delegate = delegate
        self.requests: list[GraphPatchCompositionRequest] = []

    def compose(
        self, request: GraphPatchCompositionRequest, cancellation: object
    ) -> GraphPatchCompositionRecord:
        self.requests.append(request)
        return self.delegate.compose(request, cancellation)  # type: ignore[arg-type]


class _PassingParentEvaluator:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.calls = 0

    def evaluate(
        self,
        goal: Goal,
        revision: AcceptedGraphRevision,
        composition: GraphPatchCompositionRecord,
        *,
        harness_digest: str,
        effective_policy_digest: str,
        cancellation: object,
    ) -> ParentCandidateEvaluationRecord:
        del harness_digest, cancellation
        self.calls += 1
        assert composition.composition_workspace is not None
        assert composition.candidate_patch is not None
        goal_record = GoalEvaluatorRecord(
            id="mixed-parent-goal-evaluation",
            run_id=composition.run_id,
            created_at=NOW,
            goal_id=goal.id,
            accepted_graph_revision_digest=revision.content_digest or ZERO,
            evidence_digests=(),
            decision=EvaluationDecision.PASS,
        )
        self.store.put("goal_evaluator_v2", goal_record, run_id=composition.run_id)
        record = ParentCandidateEvaluationRecord(
            id="mixed-parent-evaluation",
            run_id=composition.run_id,
            created_at=NOW,
            request_digest=canonical_digest({"composition": composition.content_digest}),
            accepted_graph_revision_digest=revision.content_digest or ZERO,
            composition_record_digest=composition.content_digest or ZERO,
            composition_workspace_digest=(
                composition.composition_workspace.content_digest or ZERO
            ),
            candidate_digest=canonical_digest(
                {"artifact": composition.candidate_patch.artifact_digest}
            ),
            candidate_descriptor_digest=composition.candidate_patch.content_digest or ZERO,
            candidate_artifact_digest=composition.candidate_patch.artifact_digest,
            effective_policy_digest=effective_policy_digest,
            goal_evaluator_digest=goal_record.content_digest or ZERO,
            decision=EvaluationDecision.PASS,
            status="ready_to_promote",
        )
        self.store.put("parent_candidate_evaluation_v2", record, run_id=composition.run_id)
        return record


class _CountingApprovalService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.calls = 0

    def request(
        self, request: ApprovalRequest, _decision: PolicyDecision
    ) -> ApprovalRecord:
        self.calls += 1
        record = ApprovalRecord(
            id="mixed-promotion-approval",
            run_id=request.run_id,
            created_at=request.created_at,
            request_digest=request.request_digest,
            policy_digest=request.policy_digest,
            scope=(request.request_digest,),
            decision="pending",
            operator_label="test-operator",
            expires_at=request.expires_at,
        )
        self.store.put("approval_v2", record, run_id=request.run_id)
        return record


def test_mixed_graph_composes_only_writing_nodes_and_preserves_artifact_refs(
    tmp_path: Path,
) -> None:
    repository, head = _repository(tmp_path, ("a", "b"))
    database = tmp_path / "fleet.db"
    artifacts = AtomicArtifactStore(tmp_path / "artifacts")
    workspace = GitWorkspaceManager(tmp_path / "workspaces", artifacts)
    run_id = "mixed-run"
    policy = _policy(run_id)
    policy_digest = canonical_digest([policy.content_digest])
    strategy = _strategy()
    requests: dict[str, WorkerRequest] = {}
    finished: set[str] = set()
    lock = threading.Lock()

    fact_criterion = CompletionCriterion(
        id="criterion-facts",
        description="facts are authoritative",
        required_artifact_ids=("process_stdout",),
    )

    def writing_node(name: str) -> Node:
        return Node(
            id=name,
            kind=NodeKind.FUNCTION,
            name=name,
            objective=f"change {name}.txt",
            output_contract=OutputContract(id=f"contract-{name}"),
            required_capabilities=("edit_intent", "process"),
            completion_criteria=(
                CompletionCriterion(
                    id=f"criterion-{name}",
                    description=f"{name} produced an exact patch",
                    required_artifact_ids=("workspace_patch",),
                ),
            ),
        )

    facts = Node(
        id="facts",
        kind=NodeKind.FUNCTION,
        name="facts",
        objective="produce read-only facts",
        output_contract=OutputContract(id="contract-facts"),
        required_capabilities=("process",),
        completion_criteria=(fact_criterion,),
    )
    graph = Graph(
        id="mixed-graph",
        nodes=(facts, writing_node("a"), writing_node("b")),
        edges=(
            Edge(id="facts-a", source_id="facts", target_id="a"),
            Edge(id="facts-b", source_id="facts", target_id="b"),
        ),
        entry_node_ids=("facts",),
        terminal_node_ids=("a", "b"),
        budget=Budget(max_attempts=3, max_nodes=3, max_wall_seconds=30.0),
    )
    goal = Goal(id="goal-mixed", statement="use facts to make two bounded edits")
    proposal = ProposedGraph(
        id="proposal-mixed",
        run_id=run_id,
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=strategy,
        effective_policy_digest=policy_digest,
        harness_digest=ZERO,
    )

    def coordinator_factory(
        selected_node: Node,
        request: WorkerRequest,
        selected_strategy: ExecutionStrategy,
    ) -> WorkCoordinator:
        inner = SQLiteStore(database)
        if selected_node.id == "facts":
            executor = _ProcessExecutor(artifacts, selected_node.id, finished, lock)

            def worker_factory(
                _snapshot: object, _cancellation: object
            ) -> _ProcessAdapter:
                return _ProcessAdapter(
                    selected_node.id,
                    request.run_id,
                    None,
                    finished,
                    lock,
                    requests,
                )

            def process_factory(_snapshot: object) -> _ProcessExecutor:
                return executor

            allowed_processes = (("produce", "facts"),)
        else:
            def worker_factory(
                _snapshot: object, _cancellation: object
            ) -> _EditAdapter:
                return _EditAdapter(selected_node.id, request.run_id, requests)

            def process_factory(_snapshot: object) -> _UnusedProcessExecutor:
                return _UnusedProcessExecutor()

            allowed_processes = ()
        return WorkCoordinator(
            inner,
            DeterministicRuntime({}, store=inner),
            workspace,
            worker_factory,
            process_factory,  # type: ignore[arg-type]
            lambda descriptor: artifacts.open_verified(descriptor).read(),
            (policy,),
            task_assessment=__import__(
                "ai_employee.routing", fromlist=["assess_task"]
            ).assess_task(selected_node.objective or selected_node.name, run_id=request.run_id),
            selected_strategy=selected_strategy,
            request_promotion_approval=False,
            allowed_processes=allowed_processes,
        )

    with SQLiteStore(database) as store:

        def allow_composition(edit: EditIntentRequest) -> PolicyDecision:
            return PolicyDecision(
                id=f"allow-{edit.id}",
                run_id=edit.run_id,
                created_at=NOW,
                request_digest=edit.content_digest or ZERO,
                effective_policy_digest=policy_digest,
                outcome=DecisionOutcome.ALLOW,
                reason_code="composition_allowed",
            )

        composer = _RecordingComposer(
            GraphPatchComposer(store, workspace, artifacts, allow_composition)
        )
        evaluator = _PassingParentEvaluator(store)
        approval = _CountingApprovalService(store)
        service = GraphExecutionService(
            store,
            coordinator_factory,
            composer,  # type: ignore[arg-type]
            (strategy,),
            repository=str(repository),
            base_commit=head,
            max_concurrency=2,
            parent_evaluator=evaluator,  # type: ignore[arg-type]
            approval_service=approval,
        )
        run = service.run(
            goal,
            proposal,
            ExecutionPolicy(max_nodes=3, max_attempts=3, max_wall_seconds=30.0),
            harness_digest=ZERO,
            effective_policy_digest=policy_digest,
            run_id=run_id,
            available_capabilities=("edit_intent", "process"),
        )
        replay = service.replay(run_id)
        composition = store.get(
            "graph_patch_composition_v2",
            run.composition_id or "missing",
            GraphPatchCompositionRecord,
        )
        approvals = store.list_records("approval_v2", ApprovalRecord, run_id=run_id)
        promotions = store.list_records("promotion_v2", PromotionRecord, run_id=run_id)

    assert run.status == "ready_to_promote"
    assert run.failure_code is None
    assert len(composer.requests) == 1
    assert tuple(item.node_id for item in composer.requests[0].node_patches) == ("a", "b")
    assert tuple(item.node_id for item in composition.ordered_inputs) == ("a", "b")
    assert evaluator.calls == 1
    assert approval.calls == 1
    assert len(approvals) == 1
    assert promotions == ()

    facts_record = next(item for item in replay.nodes if item.node_id == "facts")
    facts_descriptor = facts_record.artifact_descriptors[0]
    for node_id in ("a", "b"):
        request = requests[node_id]
        assert request.prior_artifact_digests == (facts_descriptor.artifact_digest,)
        assert request.predecessor_outputs[0].artifact_descriptors[0].descriptor_id == (
            facts_descriptor.id
        )
        assert request.predecessor_outputs[0].artifact_descriptors[0].descriptor_digest == (
            facts_descriptor.content_digest
        )

    assert subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip() == head
    assert subprocess.check_output(
        ("git", "-C", str(repository), "status", "--porcelain"), text=True
    ) == ""
    assert (repository / "a.txt").read_text(encoding="utf-8") == "a-before\n"
    assert (repository / "b.txt").read_text(encoding="utf-8") == "b-before\n"


def test_cli_rejects_diff_and_promotion_for_completed_patchless_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    execution = _execute_patchless(tmp_path, "patchless-cli-run")

    assert cli.main(["diff", execution.run.id, "--db", str(execution.database)]) == 5
    diff_output = json.loads(capsys.readouterr().out)
    assert diff_output["stable_code"] == "PATCHLESS_RUN_HAS_NO_DIFF"

    assert (
        cli.main(
            [
                "promote",
                execution.run.id,
                "--patch-digest",
                ZERO,
                "--db",
                str(execution.database),
            ]
        )
        == 5
    )
    promote_output = json.loads(capsys.readouterr().out)
    assert promote_output["stable_code"] == "PATCHLESS_RUN_CANNOT_PROMOTE"
    assert execution.body_reader.calls == 0

    assert subprocess.check_output(
        ("git", "-C", str(execution.repository), "rev-parse", "HEAD"), text=True
    ).strip() == execution.head
    assert subprocess.check_output(
        ("git", "-C", str(execution.repository), "status", "--porcelain"), text=True
    ) == ""
    with SQLiteStore(execution.database) as store:
        assert store.list_records(
            "promotion_v2", PromotionRecord, run_id=execution.run.id
        ) == ()
