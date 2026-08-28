from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
)
from ai_employee.domain.models import NodeResourceBudget
from ai_employee.domain.v2 import (
    ApprovalRecord,
    ArtifactDescriptor,
    CriterionEvidence,
    WorkerRequest,
    WorkerResult,
)
from ai_employee.inspector import inspect_graph_run
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeExecutionResult,
    TaskOrchestrator,
)
from ai_employee.task_planning import ProposedGraph

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ZERO = "0" * 64
HARNESS = "1" * 64
EFFECTIVE_POLICY = "2" * 64
RUN_ID = "revisioned-replan"


def _criterion(node_id: str) -> CompletionCriterion:
    return CompletionCriterion(
        id=f"criterion-{node_id}",
        description=f"{node_id} completed",
    )


def _node(node_id: str, *, repaired: bool = False) -> Node:
    objective = f"complete {node_id}"
    if repaired:
        objective = f"complete repaired {node_id}"
    return Node(
        id=node_id,
        kind=NodeKind.FUNCTION,
        name=node_id,
        objective=objective,
        output_contract=OutputContract(id=f"contract-{node_id}"),
        required_capabilities=("process",),
        completion_criteria=(_criterion(node_id),),
        complexity=2,
        scale=1,
        risk=1,
        resource_budget=NodeResourceBudget(
            worker_turns=1,
            processes=1,
            wall_seconds=1.0,
            artifact_bytes=10,
        ),
    )


def _budget(limit: int, max_replans: int) -> Budget:
    return Budget(
        max_attempts=limit,
        max_replans=max_replans,
        max_nodes=3,
        max_wall_seconds=float(limit),
        max_worker_turns=limit,
        max_processes=limit,
        max_artifact_bytes=limit * 10,
    )


def _revision_one_graph(limit: int, max_replans: int) -> Graph:
    return Graph(
        id="revisioned-graph",
        nodes=(_node("keep"), _node("broken")),
        entry_node_ids=("keep", "broken"),
        terminal_node_ids=("keep", "broken"),
        budget=_budget(limit, max_replans),
    )


def _revision_two_graph(
    limit: int,
    max_replans: int,
    *,
    change_keep: bool = False,
) -> Graph:
    return Graph(
        id="revisioned-graph",
        nodes=(
            _node("keep", repaired=change_keep),
            _node("broken", repaired=True),
            _node("join"),
        ),
        edges=(
            Edge(id="keep-join", source_id="keep", target_id="join"),
            Edge(id="broken-join", source_id="broken", target_id="join"),
        ),
        entry_node_ids=("keep", "broken"),
        terminal_node_ids=("join",),
        budget=_budget(limit, max_replans),
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


@dataclass
class _Scenario:
    store: SQLiteStore
    orchestrator: TaskOrchestrator
    goal: Goal
    policy: ExecutionPolicy
    strategy: ExecutionStrategy
    initial_graph: Graph
    initial_run: GraphRunRecord
    calls: list[tuple[str, int]]
    requests: dict[tuple[str, int], WorkerRequest]
    evidence: str
    limit: int
    max_replans: int


def _proposal(
    scenario: _Scenario,
    graph: Graph,
    *,
    previous: str | None,
    evidence: tuple[str, ...],
    trigger: str = "repair failed node",
    goal: Goal | None = None,
    harness_digest: str = HARNESS,
    effective_policy_digest: str = EFFECTIVE_POLICY,
) -> ProposedGraph:
    bound_goal = goal or scenario.goal
    return ProposedGraph(
        id=f"proposal-{graph.id}-{graph.budget.max_attempts}",
        run_id=RUN_ID,
        created_at=NOW,
        goal_id=bound_goal.id,
        goal_digest=canonical_digest(bound_goal),
        graph=graph,
        planner_strategy=scenario.strategy,
        effective_policy_digest=effective_policy_digest,
        harness_digest=harness_digest,
        previous_accepted_revision_digest=previous,
        replan_trigger=trigger,
        replan_evidence=evidence,
    )


def _start(
    store: SQLiteStore,
    *,
    limit: int = 4,
    max_replans: int = 1,
) -> _Scenario:
    calls: list[tuple[str, int]] = []
    requests: dict[tuple[str, int], WorkerRequest] = {}

    def runner(
        node: Node,
        request: WorkerRequest,
        _selected_strategy: ExecutionStrategy,
    ) -> NodeExecutionResult:
        key = (node.id, request.generation)
        calls.append(key)
        requests[key] = request
        blocked = node.id == "broken" and request.generation == 0
        artifact_digest = canonical_digest({"node_id": node.id, "generation": request.generation})
        descriptor = ArtifactDescriptor(
            id=f"artifact-{node.id}-{request.generation}",
            run_id=request.run_id,
            created_at=NOW,
            artifact_digest=artifact_digest,
            media_type="application/json",
            size_bytes=1,
            logical_kind=f"result-{node.id}",
            producer_action_id=f"action-{node.id}-{request.generation}",
            source={"request_digest": request.content_digest},
            store_locator=f"sha256/{artifact_digest[:2]}/{artifact_digest}",
        )
        with SQLiteStore(Path(store.path)) as writer:
            writer.put("artifact_descriptor_v2", descriptor, run_id=request.run_id)
        return NodeExecutionResult(
            worker_result=WorkerResult(
                id=f"result-{node.id}-{request.generation}",
                run_id=request.run_id,
                created_at=NOW,
                request_digest=request.content_digest or ZERO,
                status="succeeded",
                duration_seconds=0.01,
            ),
            criterion_evidence=(
                CriterionEvidence(
                    criterion_id=f"criterion-{node.id}",
                    disposition="blocked" if blocked else "satisfied",
                    evidence_refs=(ZERO,),
                ),
            ),
            artifact_descriptors=(descriptor,),
        )

    strategy = _strategy()
    graph = _revision_one_graph(limit, max_replans)
    goal = Goal(id="revisioned-goal", statement="complete the revisioned graph")
    policy = ExecutionPolicy(
        max_nodes=3,
        max_attempts=limit,
        max_wall_seconds=float(limit),
    )
    orchestrator = TaskOrchestrator(
        store,
        runner,
        (strategy,),
        max_concurrency=1,
        bounded_graph_execution=True,
        defer_parent_evaluation=True,
    )
    initial_proposal = ProposedGraph(
        id="proposal-revision-one",
        run_id=RUN_ID,
        created_at=NOW,
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=graph,
        planner_strategy=strategy,
        effective_policy_digest=EFFECTIVE_POLICY,
        harness_digest=HARNESS,
    )
    run = orchestrator.run(
        goal,
        initial_proposal,
        policy,
        harness_digest=HARNESS,
        effective_policy_digest=EFFECTIVE_POLICY,
        run_id=RUN_ID,
        available_capabilities=("process",),
    )
    replay = orchestrator.replay(RUN_ID)
    by_node = {item.node_id: item for item in replay.nodes}
    assert run.status == "failed"
    assert by_node["keep"].status == "passed"
    assert by_node["broken"].status == "failed"
    assert by_node["broken"].content_digest is not None
    return _Scenario(
        store=store,
        orchestrator=orchestrator,
        goal=goal,
        policy=policy,
        strategy=strategy,
        initial_graph=graph,
        initial_run=run,
        calls=calls,
        requests=requests,
        evidence=by_node["broken"].content_digest,
        limit=limit,
        max_replans=max_replans,
    )


def _execute_revision_two(
    scenario: _Scenario,
    *,
    graph: Graph | None = None,
) -> GraphRunRecord:
    candidate = graph or _revision_two_graph(scenario.limit, scenario.max_replans)
    proposal = _proposal(
        scenario,
        candidate,
        previous=scenario.initial_run.accepted_graph_revision_digest,
        evidence=(scenario.evidence,),
    )
    return scenario.orchestrator.run(
        scenario.goal,
        proposal,
        scenario.policy,
        harness_digest=HARNESS,
        effective_policy_digest=EFFECTIVE_POLICY,
        run_id=RUN_ID,
        available_capabilities=("process",),
        replan=True,
    )


def test_revision_two_retains_patchless_pass_and_fences_stale_authority(
    tmp_path: Path,
) -> None:
    with SQLiteStore(tmp_path / "replan.db") as store:
        scenario = _start(store)
        revision_one_digest = scenario.initial_run.accepted_graph_revision_digest
        stale_approval = ApprovalRecord(
            id="revision-one-promotion-approval",
            run_id=RUN_ID,
            created_at=NOW,
            request_digest="a" * 64,
            policy_digest=EFFECTIVE_POLICY,
            scope=("a" * 64,),
            decision="approved",
            operator_label="fixture-operator",
            expires_at=NOW + timedelta(hours=1),
            decided_at=NOW,
        )
        store.put("approval_v2", stale_approval, run_id=RUN_ID)
        store.put(
            "graph_run_v2",
            scenario.initial_run.model_copy(
                update={
                    "promotion_approval_id": stale_approval.id,
                    "promotion_approval_request_digest": stale_approval.request_digest,
                }
            ),
            run_id=RUN_ID,
            revision=1,
        )

        run = _execute_revision_two(scenario)
        replay = scenario.orchestrator.replay(RUN_ID)
        inspected = inspect_graph_run(store, RUN_ID)

        assert run.status == "completed"
        assert run.generation == 1
        assert run.replan_count == 1
        assert run.promotion_approval_id is None
        assert run.promotion_approval_request_digest is None
        assert run.accepted_graph_revision_digest != revision_one_digest
        assert [item.accepted_revision.revision_number for item in replay.revision_history] == [
            1,
            2,
        ]
        revision_two = replay.revision_history[1]
        assert revision_two.previous_revision_digest == revision_one_digest
        assert revision_two.replan_trigger == "repair failed node"
        assert revision_two.replan_evidence == (scenario.evidence,)

        by_node = {item.node_id: item for item in replay.nodes}
        retained = replay.retained_node_bindings
        assert len(retained) == 1
        assert retained[0].node_id == "keep"
        assert retained[0].previous_revision_digest == revision_one_digest
        assert retained[0].accepted_graph_revision_digest == run.accepted_graph_revision_digest
        assert retained[0].previous_generation == 0
        assert retained[0].generation == 1
        assert by_node["keep"].retained_from_revision_digest == revision_one_digest
        assert by_node["keep"].generation == 1
        assert by_node["keep"].output_generation == 0
        assert scenario.calls.count(("keep", 0)) == 1
        assert ("keep", 1) not in scenario.calls

        assert scenario.calls == [
            ("broken", 0),
            ("keep", 0),
            ("broken", 1),
            ("join", 1),
        ]
        old_broken_request = scenario.requests[("broken", 0)]
        new_broken_request = scenario.requests[("broken", 1)]
        assert old_broken_request.id != new_broken_request.id
        assert old_broken_request.run_id != new_broken_request.run_id
        assert old_broken_request.content_digest != new_broken_request.content_digest
        assert new_broken_request.accepted_graph_revision_digest == (
            run.accepted_graph_revision_digest
        )
        revision_two_routes = [item for item in replay.routes if item.generation == 1]
        revision_two_reservations = [item for item in replay.reservations if item.generation == 1]
        assert [item.node_id for item in revision_two_routes] == ["broken", "join"]
        assert [item.node_id for item in revision_two_reservations] == ["broken", "join"]
        assert all(
            item.accepted_graph_revision_digest == run.accepted_graph_revision_digest
            for item in (*revision_two_routes, *revision_two_reservations)
        )

        join_request = scenario.requests[("join", 1)]
        predecessor_by_node = {item.node_id: item for item in join_request.predecessor_outputs}
        assert set(predecessor_by_node) == {"broken", "keep"}
        assert predecessor_by_node["keep"].generation == 1
        assert predecessor_by_node["keep"].result_generation == 0
        assert predecessor_by_node["keep"].accepted_graph_revision_digest == (
            run.accepted_graph_revision_digest
        )
        assert predecessor_by_node["keep"].worker_result_digest == (
            by_node["keep"].worker_result_digest
        )

        assert inspected["replan_count"] == 1
        assert [
            item["accepted_revision"]["revision_number"] for item in inspected["graph_revisions"]
        ] == [1, 2]
        assert inspected["graph_revisions"][1]["replan_trigger"] == ("repair failed node")
        assert inspected["graph_revisions"][1]["replan_evidence"] == [scenario.evidence]
        assert inspected["retained_node_bindings"][0]["node_id"] == "keep"
        assert {item["generation"] for item in inspected["node_history"]} == {0, 1}

        calls_before_readback = tuple(scenario.calls)
        assert scenario.orchestrator.replay(RUN_ID) == replay
        assert inspect_graph_run(store, RUN_ID) == inspected
        assert tuple(scenario.calls) == calls_before_readback
        assert replay.worker_invocations == 0
        assert replay.verification_invocations == 0
        assert replay.composition_invocations == 0
        assert replay.promotion_invocations == 0

        late_result = WorkerResult(
            id="late-revision-one-result",
            run_id=old_broken_request.run_id,
            created_at=NOW,
            request_digest=old_broken_request.content_digest or ZERO,
            status="succeeded",
            duration_seconds=0.01,
        )
        scenario.orchestrator._persist_result(
            _node("broken"),
            old_broken_request,
            NodeExecutionResult(
                worker_result=late_result,
                criterion_evidence=(
                    CriterionEvidence(
                        criterion_id="criterion-broken",
                        disposition="satisfied",
                        evidence_refs=(ZERO,),
                    ),
                ),
            ),
            {},
            {},
            revision_one_digest,
        )
        after_stale = scenario.orchestrator.replay(RUN_ID)
        after_by_node = {item.node_id: item for item in after_stale.nodes}
        assert after_stale.run == run
        assert after_by_node["broken"].worker_result_id != late_result.id
        assert len(after_stale.stale_results) == 1
        assert after_stale.stale_results[0].accepted_graph_revision_digest == (revision_one_digest)
        assert after_stale.stale_results[0].authoritative_generation == 1
        approvals = store.list_records("approval_v2", ApprovalRecord, run_id=RUN_ID)
        assert approvals == (stale_approval,)
        assert after_stale.run.promotion_approval_id is None
        assert after_stale.run.promotion_approval_request_digest is None


@pytest.mark.parametrize(
    "case",
    (
        "missing-ancestry",
        "wrong-ancestry",
        "empty-trigger",
        "empty-evidence",
        "invented-evidence",
        "cycle",
        "unavailable-capability",
        "changed-goal",
        "changed-policy",
        "changed-effective-policy",
        "changed-harness",
        "changed-routing",
    ),
)
def test_replan_rejects_invalid_authority_and_graphs(
    tmp_path: Path,
    case: str,
) -> None:
    with SQLiteStore(tmp_path / f"{case}.db") as store:
        scenario = _start(store)
        candidate = _revision_two_graph(scenario.limit, scenario.max_replans)
        previous: str | None = scenario.initial_run.accepted_graph_revision_digest
        evidence = (scenario.evidence,)
        trigger = "repair failed node"
        goal = scenario.goal
        policy = scenario.policy
        harness_digest = HARNESS
        effective_policy_digest = EFFECTIVE_POLICY
        orchestrator = scenario.orchestrator

        if case == "missing-ancestry":
            previous = None
        elif case == "wrong-ancestry":
            previous = "f" * 64
        elif case == "empty-trigger":
            trigger = ""
        elif case == "empty-evidence":
            evidence = ()
        elif case == "invented-evidence":
            evidence = ("e" * 64,)
        elif case == "cycle":
            candidate = scenario.initial_graph.model_copy(
                update={
                    "edges": (
                        Edge(id="keep-broken", source_id="keep", target_id="broken"),
                        Edge(id="broken-keep", source_id="broken", target_id="keep"),
                    ),
                    "entry_node_ids": ("keep",),
                    "terminal_node_ids": ("broken",),
                }
            )
        elif case == "unavailable-capability":
            candidate = scenario.initial_graph.model_copy(
                update={
                    "nodes": (
                        _node("keep").model_copy(
                            update={"required_capabilities": ("edit_intent", "process")}
                        ),
                        _node("broken", repaired=True),
                    )
                }
            )
        elif case == "changed-goal":
            goal = scenario.goal.model_copy(update={"statement": "changed goal"})
        elif case == "changed-policy":
            policy = scenario.policy.model_copy(update={"max_attempts": 3})
        elif case == "changed-effective-policy":
            effective_policy_digest = "3" * 64
        elif case == "changed-harness":
            harness_digest = "4" * 64
        elif case == "changed-routing":
            orchestrator = TaskOrchestrator(
                store,
                scenario.orchestrator.runner,
                (scenario.strategy,),
                max_concurrency=2,
            )

        proposal = _proposal(
            scenario,
            candidate,
            previous=previous,
            evidence=evidence,
            trigger=trigger,
            goal=goal,
            harness_digest=harness_digest,
            effective_policy_digest=effective_policy_digest,
        )
        calls_before = tuple(scenario.calls)
        with pytest.raises(ValueError):
            orchestrator.run(
                goal,
                proposal,
                policy,
                harness_digest=harness_digest,
                effective_policy_digest=effective_policy_digest,
                run_id=RUN_ID,
                available_capabilities=("process",),
                replan=True,
            )
        replay = scenario.orchestrator.replay(RUN_ID)
        assert tuple(scenario.calls) == calls_before
        assert replay.run.replan_count == 0
        assert [item.accepted_revision.revision_number for item in replay.revision_history] == [1]


def test_changed_pass_contract_is_rerun_instead_of_retained(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "changed-contract.db") as store:
        scenario = _start(store, limit=5)
        run = _execute_revision_two(
            scenario,
            graph=_revision_two_graph(5, 1, change_keep=True),
        )
        replay = scenario.orchestrator.replay(RUN_ID)

    assert run.status == "completed"
    assert replay.retained_node_bindings == ()
    assert scenario.calls.count(("keep", 0)) == 1
    assert scenario.calls.count(("keep", 1)) == 1
    assert len(replay.reservations) == 5
    assert len(replay.routes) == 5


def test_replan_limits_and_aggregate_budgets_do_not_reset(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "aggregate.db") as store:
        scenario = _start(store, limit=3)
        run = _execute_revision_two(scenario)
        replay = scenario.orchestrator.replay(RUN_ID)

        assert run.status == "failed"
        assert run.replan_count == 1
        assert len(scenario.calls) == 3
        assert len(replay.reservations) == 3
        assert sum(int(item.requested["worker_turns"]) for item in replay.reservations) == 3
        assert sum(int(item.requested["processes"]) for item in replay.reservations) == 3
        assert sum(float(item.requested["wall_seconds"]) for item in replay.reservations) == 3.0
        assert sum(int(item.requested["artifact_bytes"]) for item in replay.reservations) == 30
        final_remaining = replay.reservations[-1].remaining_budgets
        assert final_remaining["node_attempts"] == 0
        assert final_remaining["worker_turns"] == 0
        assert final_remaining["processes"] == 0
        assert final_remaining["wall_seconds"] == 0.0
        assert final_remaining["artifact_bytes"] == 0
        by_node = {item.node_id: item for item in replay.nodes}
        assert by_node["join"].failure_code == "DUPLICATE_OR_BUDGETED_CLAIM"

    with SQLiteStore(tmp_path / "max-replans.db") as store:
        scenario = _start(store)
        completed = _execute_revision_two(scenario)
        replay = scenario.orchestrator.replay(RUN_ID)
        current_broken = next(item for item in replay.nodes if item.node_id == "broken")
        assert current_broken.content_digest is not None
        store.put(
            "graph_run_v2",
            completed.model_copy(update={"status": "failed", "failure_code": "fixture"}),
            run_id=RUN_ID,
            revision=2,
        )
        proposal = _proposal(
            scenario,
            _revision_two_graph(4, 1),
            previous=completed.accepted_graph_revision_digest,
            evidence=(current_broken.content_digest,),
        )
        with pytest.raises(ValueError, match="replan authority"):
            scenario.orchestrator.run(
                scenario.goal,
                proposal,
                scenario.policy,
                harness_digest=HARNESS,
                effective_policy_digest=EFFECTIVE_POLICY,
                run_id=RUN_ID,
                available_capabilities=("process",),
                replan=True,
            )
        assert scenario.orchestrator.replay(RUN_ID).run.replan_count == 1

    with SQLiteStore(tmp_path / "older-evidence.db") as store:
        scenario = _start(store, max_replans=2)
        completed = _execute_revision_two(scenario)
        store.put(
            "graph_run_v2",
            completed.model_copy(update={"status": "failed", "failure_code": "fixture"}),
            run_id=RUN_ID,
            revision=2,
        )
        proposal = _proposal(
            scenario,
            _revision_two_graph(4, 2),
            previous=completed.accepted_graph_revision_digest,
            evidence=(scenario.evidence,),
        )
        with pytest.raises(ValueError, match="not authoritative"):
            scenario.orchestrator.run(
                scenario.goal,
                proposal,
                scenario.policy,
                harness_digest=HARNESS,
                effective_policy_digest=EFFECTIVE_POLICY,
                run_id=RUN_ID,
                available_capabilities=("process",),
                replan=True,
            )
        final = scenario.orchestrator.replay(RUN_ID)
        assert final.run.replan_count == 1
        assert [item.accepted_revision.revision_number for item in final.revision_history] == [1, 2]
