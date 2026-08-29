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
    EvaluationDecision,
    ExecutionPolicy,
    ExecutionStrategy,
    Goal,
    Graph,
    HarnessCommand,
    HarnessEvaluator,
    HarnessReview,
    HarnessVerification,
    Node,
    NodeKind,
    OutputContract,
    ProjectHarnessV2,
    RoutingMode,
)
from ai_employee.domain.browser import BrowserAction, BrowserCapture, BrowserScenario
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
    WorkerContextManifest,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.graph_composition import GraphPatchComposer, GraphPatchCompositionRecord
from ai_employee.graph_evaluation import (
    GraphCandidateEvaluator,
    ParentCandidateEvaluationRecord,
)
from ai_employee.graph_execution import GraphExecutionService
from ai_employee.inspector import inspect_graph_run
from ai_employee.orchestration import WorkCoordinator
from ai_employee.parent_review import (
    ParentSemanticBasis,
    ParentSemanticConfidence,
    ParentSemanticFinding,
    ParentSemanticFindingType,
    ParentSemanticReviewDecision,
    ParentSemanticReviewPayload,
    ParentSemanticReviewRequest,
    ParentSemanticReviewResult,
    ParentSemanticSeverity,
    bind_parent_semantic_review_payload,
)
from ai_employee.run_explanation import explain_any_run
from ai_employee.runtime import DeterministicRuntime
from ai_employee.serialization import canonical_digest, project_harness_digest
from ai_employee.services_v2 import (
    AtomicArtifactStore,
    GitWorkspaceManager,
    PlaywrightBrowserEvaluationServices,
)
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import NodeExecutionRecord
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "parent_verification_succeeds",
    [
        True,
        False,
        None,
        "browser",
        "semantic-repair",
        "semantic-retry",
        "semantic-stale-node",
    ],
)
def test_bounded_fork_join_executes_composes_and_replays_without_promotion(
    tmp_path: Path, parent_verification_succeeds: bool | str | None
) -> None:
    semantic_repair = parent_verification_succeeds == "semantic-repair"
    semantic_retry = parent_verification_succeeds == "semantic-retry"
    semantic_stale_node = parent_verification_succeeds == "semantic-stale-node"
    semantic_enabled = semantic_repair or semantic_retry or semantic_stale_node
    semantic_invoked = semantic_repair or semantic_retry
    deterministic_parent_succeeds = parent_verification_succeeds in {
        True,
        "browser",
        "semantic-repair",
        "semantic-retry",
        "semantic-stale-node",
    }
    parent_ready = parent_verification_succeeds in {True, "browser"}
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
                verification_requirement_ids=(
                    () if parent_verification_succeeds == "browser" else ("parent-test",)
                ),
                required_artifact_ids=("workspace_patch",),
            ),
        ),
    )
    if parent_verification_succeeds == "browser":
        browser_scenario = BrowserScenario(
            origin="http://127.0.0.1:3000",
            actions=(BrowserAction(kind="navigate", url="http://127.0.0.1:3000/index.html"),),
            captures=(
                BrowserCapture(
                    id="browser-screen",
                    kind="screenshot",
                    logical_kind="browser_screenshot",
                ),
            ),
        )
        harness = ProjectHarnessV2(
            evaluators=(
                HarnessEvaluator(
                    id="parent-browser-evaluator",
                    provider_id="browser.playwright",
                    browser_scenario=browser_scenario,
                    criterion_ids=("parent-verification",),
                ),
            ),
            verification=HarnessVerification(
                required_evaluators=("parent-browser-evaluator",),
            ),
        )
    else:
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
                review=HarnessReview(parent_semantic_review=semantic_enabled),
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
    harness_digest = project_harness_digest(harness)
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
            if not deterministic_parent_succeeds:
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

    class ParentBrowserEngine:
        def __init__(self) -> None:
            self.url: str | None = None

        def open(self, _route_handler: object) -> None:
            pass

        def navigate(self, url: str, _timeout_seconds: float) -> None:
            self.url = url

        def click(self, _selector: str, _timeout_seconds: float) -> None:
            pass

        def fill(self, _selector: str, _value: str, _timeout_seconds: float) -> None:
            pass

        def screenshot(self, _timeout_seconds: float) -> bytes:
            return b"browser-screenshot"

        def console(self) -> tuple[dict[str, object], ...]:
            return ()

        def dom(self, _timeout_seconds: float) -> str:
            return "<html></html>"

        def accessibility(self, _timeout_seconds: float) -> object:
            return {"role": "document"}

        def current_url(self) -> str | None:
            return self.url

        def close_page(self) -> None:
            pass

        def close_context(self) -> None:
            pass

        def close_browser(self) -> None:
            pass

        def close_engine(self) -> None:
            pass

    def parent_browser_services(
        snapshot: object, cancellation: object
    ) -> PlaywrightBrowserEvaluationServices:
        return PlaywrightBrowserEvaluationServices(
            snapshot.isolated_worktree,  # type: ignore[attr-defined]
            artifacts,
            cancellation,  # type: ignore[arg-type]
            engine_factory=ParentBrowserEngine,  # type: ignore[arg-type]
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

        class SemanticReviewer:
            def __init__(self, reviewer_strategy: ExecutionStrategy) -> None:
                self.strategy = reviewer_strategy
                self.calls = 0

            def review(self, request: ParentSemanticReviewRequest) -> ParentSemanticReviewResult:
                self.calls += 1
                if semantic_retry and self.calls == 1:
                    raise ValueError("temporary reviewer outage")
                finding = ParentSemanticFinding(
                    id="cross-task-integration-gap",
                    finding_type=ParentSemanticFindingType.INTEGRATION_CONSISTENCY,
                    severity=ParentSemanticSeverity.HIGH,
                    confidence=ParentSemanticConfidence.CERTAIN,
                    basis=ParentSemanticBasis.OBSERVED,
                    criterion_ids=("parent-verification",),
                    node_ids=("a", "b"),
                    observation="the individually passing changes do not integrate semantically",
                    rationale="the exact composed candidate retains incompatible assumptions",
                    evidence_digests=(request.candidate_artifact_digest,),
                    artifact_digests=(request.candidate_artifact_digest,),
                    repair_objective="make node b consume node a's integrated result",
                )
                return bind_parent_semantic_review_payload(
                    ParentSemanticReviewPayload(
                        findings=(finding,),
                        reviewed_criterion_ids=("parent-verification",),
                        reviewed_node_ids=("a", "b", "c"),
                    ),
                    request=request,
                    record_id="semantic-result",
                    run_id=request.run_id,
                    created_at=NOW,
                )

        semantic_reviewer = SemanticReviewer(strategy)

        if semantic_enabled:
            with pytest.raises(ValueError, match="must match the bound Harness"):
                GraphCandidateEvaluator(
                    store,
                    workspace,
                    harness,
                    ParentExecutor,
                    allow_parent_process,
                    semantic_reviewer=semantic_reviewer,
                    semantic_block_severities=(ParentSemanticSeverity.LOW,),
                )

        parent_evaluator = GraphCandidateEvaluator(
            store,
            workspace,
            harness,
            ParentExecutor,
            allow_parent_process,
            browser_services_factory=(
                parent_browser_services if parent_verification_succeeds == "browser" else None
            ),
            semantic_reviewer=semantic_reviewer if semantic_enabled else None,
        )

        class CountingComposer(GraphPatchComposer):
            def compose(self, request: object, cancellation: object) -> object:
                nonlocal composition_calls
                composition_calls += 1
                result = real_composer.compose(request, cancellation)  # type: ignore[arg-type]
                if semantic_stale_node:
                    accepted = next(
                        item
                        for item in store.list_records(
                            "node_execution_v2", NodeExecutionRecord, run_id="graph-e2e"
                        )
                        if item.node_id == "a" and item.status == "passed"
                    )
                    payload = accepted.model_dump(mode="python")
                    payload.update(
                        id="late-uncomposed-pass-a",
                        attempt=accepted.attempt + 1,
                        sequence=accepted.sequence + 100,
                        content_digest=None,
                    )
                    store.put(
                        "node_execution_v2",
                        NodeExecutionRecord.model_validate(payload),
                        run_id="graph-e2e",
                    )
                return result

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

        assert run.status == ("ready_to_promote" if parent_ready else "failed")
        assert run.failure_code == (
            None
            if parent_ready
            else (
                "PARENT_EVALUATION_UNAVAILABLE"
                if parent_verification_succeeds is None
                else (
                    "PARENT_SEMANTIC_REPAIR"
                    if semantic_repair
                    else (
                        "PARENT_SEMANTIC_REVIEW_UNAVAILABLE"
                        if semantic_retry
                        else (
                            "PARENT_SEMANTIC_BINDING_MISMATCH"
                            if semantic_stale_node
                            else "PARENT_VERIFICATION_FAILED"
                        )
                    )
                )
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
        if semantic_stale_node:
            by_node["a"] = next(
                item
                for item in store.list_records(
                    "node_execution_v2", NodeExecutionRecord, run_id=run.id
                )
                if item.node_id == "a"
                and item.status == "passed"
                and item.id != "late-uncomposed-pass-a"
            )
        evaluator_by_id = {item.id: item for item in replay.evaluator_decisions}
        manifests = store.list_records(
            "worker_context_manifest_v2", WorkerContextManifest, run_id=run.id
        )
        assert replay.context_manifests == tuple(
            sorted(manifests, key=lambda item: (item.generation, item.attempt, item.node_id))
        )
        assert {item.node_id for item in manifests} == {"a", "b", "c"}
        manifest_by_node = {item.node_id: item for item in manifests}
        for node_id, request in requests.items():
            manifest = manifest_by_node[node_id]
            assert manifest.worker_request_digest == request.content_digest
            assert manifest.objective_digest == canonical_digest(request.goal)
            assert manifest.completion_criteria_digest == canonical_digest(
                request.completion_criteria
            )
            assert request.completion_criteria
            assert manifest.required_capabilities == request.required_capabilities
            assert manifest.accepted_graph_revision_digest == run.accepted_graph_revision_digest
            assert manifest.generation == request.generation
            assert manifest.predecessor_result_digests == request.prior_result_digests
            assert not manifest.conversation_history_included
            assert not manifest.artifact_bodies_included
        assert manifest_by_node["c"].predecessor_evidence_digests == tuple(
            by_node[name].evidence_digest for name in ("a", "b")
        )
        inspected = inspect_graph_run(store, run.id)
        explanation = explain_any_run(store, run.id)
        assert len(inspected["parent_acceptance"]) == (
            0 if parent_verification_succeeds is None else 1
        )
        assert len(inspected["parent_browser_observations"]) == (
            1 if parent_verification_succeeds == "browser" else 0
        )
        assert len(inspected["parent_semantic_review"]["decisions"]) == (
            1 if semantic_invoked else 0
        )
        assert len(inspected["parent_semantic_review"]["repair_requests"]) == (
            1 if semantic_repair else 0
        )
        inspected_manifests = inspected["worker_context_manifests"]
        assert len(inspected_manifests) == 3
        assert all(item["artifact_bodies_included"] is False for item in inspected_manifests)
        assert all(
            item["information_flow"]["artifact_bodies_included"] is False
            for item in explanation["task_stories"]
        )
        if parent_ready:
            assert explanation["current_state"]["promotion_approval_state"] == "pending"
            assert explanation["final_outcome"]["promotion_approval"]["binding"] == ("bound")
            assert explanation["final_outcome"]["disposition"] == ("accepted_awaiting_approval")
            assert explanation["final_outcome"]["next_action"] == (
                "approve or deny the exact pending promotion request"
            )
        if semantic_repair:
            assert explanation["final_outcome"]["parent_semantic_review"]["action"] == ("REPAIR")
            assert explanation["final_outcome"]["disposition"] == "rejected_or_incomplete"
        assert requests["c"].prior_artifact_digests == (
            by_node["a"].patch_digest,
            by_node["b"].patch_digest,
        )
        for reference in requests["c"].predecessor_outputs:
            predecessor = by_node[reference.node_id]
            evaluator = evaluator_by_id[reference.evaluator_id]
            assert reference.accepted_graph_revision_digest == (run.accepted_graph_revision_digest)
            assert reference.generation == predecessor.generation
            assert reference.result_generation == predecessor.output_generation
            assert reference.attempt == predecessor.attempt
            assert reference.worker_result_id == predecessor.worker_result_id
            assert reference.worker_result_digest == predecessor.worker_result_digest
            assert reference.artifact_descriptor_id == predecessor.patch_artifact_id
            assert reference.artifact_descriptor_digest == (predecessor.patch_descriptor_digest)
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
        assert parent_process_calls == (
            0 if parent_verification_succeeds in {None, "browser", "semantic-stale-node"} else 1
        )
        if run.parent_evaluation_id is not None:
            parent_record = store.get(
                "parent_candidate_evaluation_v2",
                run.parent_evaluation_id,
                ParentCandidateEvaluationRecord,
            )
            assert parent_record.status == run.status
            parent_replay = parent_evaluator.replay(parent_record.id)
            assert parent_replay.record == parent_record
            assert len(parent_replay.acceptance_ledger.criteria) == 1
            assert parent_replay.acceptance_ledger.criteria[0].disposition == (
                "uncovered" if semantic_stale_node else "satisfied" if parent_ready else "blocked"
            )
            if semantic_stale_node:
                assert parent_record.verification_result_digests == ()
            assert len(parent_replay.semantic_decisions) == (1 if semantic_invoked else 0)
            assert len(parent_replay.semantic_repair_requests) == (1 if semantic_repair else 0)
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
        if semantic_invoked:
            assert run.composition_id is not None
            persisted_composition = store.get(
                "graph_patch_composition_v2",
                run.composition_id,
                GraphPatchCompositionRecord,
            )
            if semantic_retry:
                original_runtime = store.list_records(
                    "verification_result_v2", ExecutionResult, run_id=run.id
                )[0]
                changed_runtime = ExecutionResult(
                    id="second-parent-verification-result",
                    run_id=run.id,
                    created_at=NOW,
                    request_digest=original_runtime.request_digest,
                    status="succeeded",
                    exit_code=0,
                    duration_seconds=0.02,
                )
                assert changed_runtime.content_digest != original_runtime.content_digest
                store.put("verification_result_v2", changed_runtime, run_id=run.id)
                pending_request = store.list_records(
                    "parent_semantic_review_request_v2",
                    ParentSemanticReviewRequest,
                    run_id=run.id,
                )[0]
                interrupted_result = semantic_reviewer.review(pending_request)
                store.put(
                    "parent_semantic_review_result_v2",
                    interrupted_result,
                    run_id=run.id,
                )
            resumed_evaluation = parent_evaluator.evaluate(
                goal,
                replay.acceptance.accepted_revision,
                persisted_composition,
                harness_digest=harness_digest,
                effective_policy_digest=effective_policy_digest,
                cancellation=type("NotCancelled", (), {"cancelled": lambda self: False})(),
            )
            assert resumed_evaluation.decision is EvaluationDecision.REPAIR
            assert len(parent_evaluator.replay(resumed_evaluation.id).semantic_results) == 1
            completed_decisions = tuple(
                item
                for item in store.list_records(
                    "parent_semantic_review_decision_v2",
                    ParentSemanticReviewDecision,
                    run_id=run.id,
                )
                if item.result_digest is not None
            )
            assert len(completed_decisions) == 1
            assert len({item.content_digest for item in completed_decisions}) == 1
        assert semantic_reviewer.calls == (2 if semantic_retry else 1 if semantic_repair else 0)
