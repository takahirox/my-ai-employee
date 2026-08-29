from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    ExecutionStrategy,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    RoutingMode,
    SemanticAmbiguity,
    SemanticReasoningClass,
    SemanticScope,
    SemanticTaskProfile,
    SemanticTaskType,
)
from ai_employee.domain.v2 import (
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from ai_employee.plan_review import (
    CliPlanReviewer,
    PlanReviewFinding,
    PlanReviewFindingType,
    PlanReviewImpact,
    PlanReviewPayload,
    bind_plan_review,
)
from ai_employee.serialization import canonical_digest, canonical_json
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    TaskGraphAcceptance,
    one_node_graph,
)
from ai_employee.task_planning import (
    CliProposedGraphPlanner,
    ProposedGraph,
    ProposedGraphPayload,
    proposed_graph_schema_json,
)


def test_work_cli_defaults_to_adaptive_routing() -> None:
    args = cli.build_parser().parse_args(["work", "route this task"])

    assert args.routing_mode == "adaptive"
    assert args.strategy_set is None
    assert args.max_concurrency == 1


def test_claude_graph_planner_disables_tools_without_empty_argv() -> None:
    planner = CliProposedGraphPlanner(
        object(),  # type: ignore[arg-type]
        lambda _digest: b"",
        lambda _request: (_ for _ in ()).throw(AssertionError("must not execute")),
        run_id="planner-run",
        strategy=ExecutionStrategy(
            id="claude-fable-high",
            routing_mode=RoutingMode.ADAPTIVE,
            backend="claude_code_cli",
            model="claude-fable-5",
            effort="high",
            capabilities=("process",),
        ),
        executable="claude",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    argv = planner._argv()

    assert "--tools=" in argv
    assert "" not in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert argv[argv.index("--effort") + 1] == "high"


def test_claude_planner_revision_uses_minimal_environment_and_nonempty_argv() -> None:
    goal = Goal(id="revision-goal", statement="Complete one bounded task")
    strategy = ExecutionStrategy(
        id="claude-revision",
        routing_mode=RoutingMode.ADAPTIVE,
        backend="claude_code_cli",
        model="claude-fable-5",
        effort="high",
        capabilities=("process",),
    )
    original = ProposedGraph(
        id="revision-proposal",
        run_id="planner-run",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        goal_id=goal.id,
        goal_digest=canonical_digest(goal),
        graph=one_node_graph(
            goal,
            graph_id="revision-graph",
            node_id="revision-node",
            required_capabilities=("process",),
            max_wall_seconds=30.0,
        ),
        planner_strategy=strategy,
        effective_policy_digest="1" * 64,
        harness_digest="2" * 64,
    )
    finding = PlanReviewFinding(
        id="revision-finding",
        finding_type=PlanReviewFindingType.PREMATURE_GENERALIZATION,
        impact=PlanReviewImpact.BLOCKING,
        affected_node_ids=("revision-node",),
        goal_relation="The node is broader than the accepted goal.",
        smallest_correction="Limit the node to the accepted goal.",
    )
    requests: list[ProcessRequest] = []

    def deny(request: ProcessRequest) -> PolicyDecision:
        requests.append(request)
        return PolicyDecision(
            id="revision-policy",
            run_id=request.run_id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_digest=request.content_digest or "0" * 64,
            effective_policy_digest=original.effective_policy_digest,
            outcome=DecisionOutcome.DENY,
            reason_code="scripted_denial",
        )

    planner = CliProposedGraphPlanner(
        object(),  # type: ignore[arg-type]
        lambda _digest: b"",
        deny,
        run_id="planner-run",
        strategy=strategy,
        executable="claude",
        cwd=".",
        prompt_writer=lambda _value: "8" * 64,
    )

    with pytest.raises(ValueError, match="revision policy did not allow execution"):
        planner.revise(
            goal,
            original,
            (finding,),
            available_capabilities=("process",),
            max_nodes=1,
            max_wall_seconds=30.0,
        )

    assert len(requests) == 1
    assert "--tools=" in requests[0].argv
    assert "" not in requests[0].argv
    assert requests[0].inherit_environment == ("HOME", "USER")


def _capture_planner_prompt(
    goal: Goal,
    *,
    semantic_profile: SemanticTaskProfile | None = None,
    include_profile: bool = True,
    complexity: int = 1,
    scale: int = 1,
) -> tuple[dict[str, object], ProposedGraph]:
    profile = semantic_profile or SemanticTaskProfile(
        task_type=SemanticTaskType.MECHANICAL,
        reasoning_class=SemanticReasoningClass.MECHANICAL,
        scope=SemanticScope.BOUNDED,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("one explicit operation",),
    )
    graph = Graph(
        id=f"graph-{goal.id}",
        nodes=(
            Node(
                id=f"node-{goal.id}",
                kind=NodeKind.FUNCTION,
                name="Complete accepted goal",
                objective=goal.statement,
                output_contract=OutputContract(id=f"contract-{goal.id}"),
                required_capabilities=("process",),
                semantic_profile=profile if include_profile else None,
                complexity=complexity,
                scale=scale,
                completion_criteria=(
                    CompletionCriterion(
                        id=f"criterion-{goal.id}",
                        description="the accepted goal is complete",
                    ),
                ),
            ),
        ),
        entry_node_ids=(f"node-{goal.id}",),
        terminal_node_ids=(f"node-{goal.id}",),
        budget=Budget(max_attempts=1, max_nodes=1, max_wall_seconds=30.0),
    )
    output_digest = "9" * 64
    output = canonical_json(ProposedGraphPayload(goal_id=goal.id, graph=graph)).encode()
    captured: list[bytes] = []

    class ScriptedPlannerExecutor:
        def execute(
            self,
            request: ProcessRequest,
            decision: PolicyDecision,
            _cancellation: object,
        ) -> ExecutionResult:
            assert all(request.argv)
            assert decision.request_digest == request.content_digest
            return ExecutionResult(
                id="planner-execution-1",
                run_id=request.run_id,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                request_digest=request.content_digest or "0" * 64,
                status="succeeded",
                exit_code=0,
                duration_seconds=0.01,
                stdout_artifact_digest=output_digest,
            )

    def write_prompt(value: bytes) -> str:
        captured.append(value)
        return "8" * 64

    def read_output(digest: str) -> bytes:
        assert digest == output_digest
        return output

    def allow(request: ProcessRequest) -> PolicyDecision:
        return PolicyDecision(
            id="planner-policy-1",
            run_id=request.run_id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            request_digest=request.content_digest or "0" * 64,
            effective_policy_digest="1" * 64,
            outcome=DecisionOutcome.ALLOW,
            reason_code="policy_allowed",
        )

    planner = CliProposedGraphPlanner(
        ScriptedPlannerExecutor(),
        read_output,
        allow,
        run_id="planner-run",
        strategy=ExecutionStrategy(
            id="planner-strategy",
            routing_mode=RoutingMode.ADAPTIVE,
            backend="ollama_cli",
            model="scripted-model",
            effort="low",
            capabilities=("process",),
        ),
        executable="ollama",
        cwd=".",
        prompt_writer=write_prompt,
    )
    proposal = planner.plan(
        goal,
        available_capabilities=("process",),
        effective_policy_digest="1" * 64,
        harness_digest="2" * 64,
        max_nodes=1,
        max_wall_seconds=30.0,
    )

    assert len(captured) == 1
    return json.loads(captured[0]), proposal


def test_planner_prompt_defaults_to_minimal_sufficient_and_preserves_explicit_breadth() -> None:
    local_goal = Goal(id="goal-local-fix", statement="Fix the local parser bug")
    broad_goal = Goal(
        id="goal-exhaustive-audit",
        statement="Exhaustively audit every authentication path for security defects",
    )

    local_prompt, _ = _capture_planner_prompt(local_goal)
    broad_prompt, _ = _capture_planner_prompt(broad_goal)
    instruction = local_prompt["instruction"]

    assert local_prompt["protocol"] == broad_prompt["protocol"] == "fleet-proposed-graph/2"
    assert local_prompt["goal"] == local_goal.model_dump(mode="json")
    assert broad_prompt["goal"] == broad_goal.model_dump(mode="json")
    assert local_prompt["response_schema"] == broad_prompt["response_schema"]
    assert local_prompt["response_schema"] == json.loads(proposed_graph_schema_json())
    assert isinstance(instruction, str)
    assert instruction == broad_prompt["instruction"]
    assert "minimal_sufficient is the default" in instruction
    assert "speculative framework, abstraction, extension point" in instruction
    assert "required tests, verification, error handling, or compatibility" in instruction
    assert "current accepted-Goal criterion" in instruction
    assert "concrete repository evidence" in instruction
    assert "explicit in the accepted Goal" in instruction
    assert "relevant node objectives and completion criteria" in instruction


def test_planner_requires_profiles_and_overwrites_numeric_compatibility() -> None:
    profile = SemanticTaskProfile(
        task_type=SemanticTaskType.ARCHITECTURE,
        reasoning_class=SemanticReasoningClass.DEEP,
        scope=SemanticScope.BROAD,
        ambiguity=SemanticAmbiguity.LOW,
        reasons=("cross-component contracts",),
    )
    _, proposal = _capture_planner_prompt(
        Goal(id="goal-profiled", statement="Choose compatible contracts"),
        semantic_profile=profile,
        complexity=1,
        scale=1,
    )
    node = proposal.graph.nodes[0]
    assert (node.complexity, node.scale) == (1, 1)
    assert node.semantic_profile == profile
    with pytest.raises(ValueError, match="missing semantic_profile"):
        _capture_planner_prompt(
            Goal(id="goal-unprofiled", statement="Choose compatible contracts"),
            include_profile=False,
        )


def _write_routing_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".fleet").mkdir()
    (repository / "README.md").write_text("routing fixture\n", encoding="utf-8")
    (repository / ".fleet" / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "worker": {
                    "allowed": ["codex_cli"],
                    "allowed_strategy_ids": ["luna", "sol"],
                    "adaptive_routing": True,
                    "local_backend": False,
                },
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(("git", "-C", str(repository), "init", "-q"), check=True)
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "-c",
            "user.email=fleet@example.invalid",
            "-c",
            "user.name=Fleet Test",
            "commit",
            "-qm",
            "base",
        ),
        check=True,
    )

    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        """#!/bin/sh
if [ "$1" = "--version" ]; then
  printf '%s\n' 'codex test'
  exit 0
fi
if [ "$1" = "--help" ]; then
  printf '%s\n' 'Usage: codex exec'
  exit 0
fi
printf '%s' '{"schema_version":"1","task_type":"architecture",'
printf '%s' '"reasoning_class":"deep","scope":"broad","ambiguity":"low",'
printf '%s\n' '"reasons":["multiple dependent implementation steps"]}'
exit 0
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    operator_config = tmp_path / "operator.json"
    operator_config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workers": {"codex_cli": {"executable": str(fake_codex)}},
                "routing": {
                    "default_strategy_set": "codex-all",
                    "default_assessment_strategy": "sol",
                    "strategy_sets": {
                        "small-only": ["luna"],
                        "codex-all": ["luna", "sol"],
                    },
                    "strategies": [
                        {
                            "id": "luna",
                            "backend": "codex_cli",
                            "model": "gpt-5.6-luna",
                            "effort": "medium",
                            "planner_eligible": True,
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 1,
                            "max_complexity": 2,
                            "min_scale": 1,
                            "max_scale": 2,
                            "max_risk": 0,
                        },
                        {
                            "id": "sol",
                            "backend": "codex_cli",
                            "model": "gpt-5.6-sol",
                            "effort": "high",
                            "planner_eligible": True,
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 1,
                            "max_complexity": 10,
                            "min_scale": 1,
                            "max_scale": 10,
                            "max_risk": 10,
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, operator_config, tmp_path / "fleet.db"


def test_fixed_routing_uses_the_degenerate_authoritative_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator_config, db_path = _write_routing_fixture(tmp_path)
    goal = "Inspect the request; implement each change; verify the persisted routing"

    result = cli.main(
        [
            "work",
            goal,
            "--repo",
            str(repository),
            "--operator-config",
            str(operator_config),
            "--db",
            str(db_path),
            "--plan-only",
            "--routing-mode",
            "fixed",
            "--strategy",
            "sol",
            "--max-concurrency",
            "1",
        ]
    )

    assert result == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "planned"
    with SQLiteStore(db_path) as store:
        graph_run = store.get("graph_run_v2", emitted["run_id"], GraphRunRecord)
        acceptance = store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=emitted["run_id"]
        )[0]
        with pytest.raises(KeyError):
            store.get_work_run(emitted["run_id"])

    assert graph_run.status == "planned"
    assert len(acceptance.accepted_revision.graph.nodes) == 1
    assert acceptance.accepted_revision.graph.entry_node_ids == (
        acceptance.accepted_revision.graph.nodes[0].id,
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--routing-mode", "adaptive", "--model", "gpt-5.6-sol"),
            "--routing-mode cannot be combined with --model",
        ),
        (
            ("--routing-mode", "adaptive", "--strategy", "sol"),
            "--routing-mode adaptive rejects --strategy",
        ),
        (
            ("--routing-mode", "adaptive", "--worker", "ollama_cli"),
            "--routing-mode cannot be combined with --worker",
        ),
        (
            ("--routing-mode", "legacy", "--strategy-set", "codex-all"),
            "--strategy-set requires fixed or adaptive routing",
        ),
        (
            (
                "--routing-mode",
                "fixed",
                "--strategy",
                "sol",
                "--assessment-strategy",
                "sol",
            ),
            "--assessment-strategy requires adaptive routing",
        ),
    ),
)
def test_routing_rejects_conflicting_cli_overrides_before_execution(
    arguments: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        cli.main(["work", "route this task", *arguments])


def test_adaptive_planning_uses_graph_authority_at_max_concurrency_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, operator_config, db_path = _write_routing_fixture(tmp_path)

    def criterion(name: str) -> CompletionCriterion:
        return CompletionCriterion(id=f"criterion-{name}", description=f"{name} is complete")

    def node(name: str, complexity: int) -> Node:
        return Node(
            id=name,
            kind=NodeKind.FUNCTION,
            name=name,
            objective=f"complete {name}",
            output_contract=OutputContract(id=f"contract-{name}"),
            required_capabilities=("edit_intent", "process"),
            completion_criteria=(criterion(name),),
            complexity=complexity,
            scale=complexity,
        )

    graph = Graph(
        id="planned-fork-join",
        nodes=(node("a", 2), node("b", 8), node("c", 8)),
        edges=(
            Edge(id="a-c", source_id="a", target_id="c"),
            Edge(id="b-c", source_id="b", target_id="c"),
        ),
        entry_node_ids=("a", "b"),
        terminal_node_ids=("c",),
        budget=Budget(max_attempts=3, max_nodes=3, max_wall_seconds=30.0),
    )

    def fake_plan(
        self: CliProposedGraphPlanner,
        goal: Goal,
        *,
        available_capabilities: Sequence[str],
        effective_policy_digest: str,
        harness_digest: str,
        max_nodes: int,
        max_wall_seconds: float,
    ) -> ProposedGraph:
        del available_capabilities, max_nodes, max_wall_seconds
        return ProposedGraph(
            id="proposal-fork-join",
            run_id=self.run_id,
            created_at="2026-01-01T00:00:00Z",
            goal_id=goal.id,
            goal_digest=canonical_digest(goal),
            graph=graph,
            planner_strategy=self.strategy,
            effective_policy_digest=effective_policy_digest,
            harness_digest=harness_digest,
            planner_routing=self.planner_routing,
        )

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", fake_plan)

    def fake_review(
        self: CliPlanReviewer,
        goal: Goal,
        proposed_graph: ProposedGraph,
        *,
        review_round: int,
        available_capabilities: Sequence[str],
        max_nodes: int,
        max_wall_seconds: float,
    ):
        del available_capabilities, max_nodes, max_wall_seconds
        return bind_plan_review(
            PlanReviewPayload(findings=()),
            record_id="cli-plan-review",
            run_id=self.run_id,
            created_at="2026-01-01T00:00:00Z",
            review_round=review_round,  # type: ignore[arg-type]
            goal=goal,
            proposed_graph=proposed_graph,
            reviewer_strategy=self.strategy,
        )

    monkeypatch.setattr(CliPlanReviewer, "review", fake_review)

    result = cli.main(
        [
            "work",
            "Run two independent changes, then join their verified results",
            "--repo",
            str(repository),
            "--operator-config",
            str(operator_config),
            "--db",
            str(db_path),
            "--plan-only",
            "--max-concurrency",
            "1",
        ]
    )

    assert result == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "planned"
    assert emitted["stable_code"] is None
    with SQLiteStore(db_path) as store:
        proposal = store.list_records("proposed_graph_v2", ProposedGraph, run_id=emitted["run_id"])[
            0
        ]
        acceptance = store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=emitted["run_id"]
        )[0]
    assert proposal.graph == graph
    assert acceptance.proposed_graph_digest == proposal.content_digest
    assert acceptance.accepted_revision.graph == graph
    assert proposal.planner_strategy.model == "gpt-5.6-sol"
    assert proposal.planner_strategy.effort == "high"
    assert proposal.planner_routing is not None
    assert proposal.planner_routing.candidate_strategy_ids == ("luna", "sol")
    assert proposal.planner_routing.eligible_strategy_ids == ("sol",)
    assert proposal.planner_routing.assessment.semantic_profile is not None
    assert proposal.planner_routing.selected_strategy.id == "sol"


def test_adaptive_planner_failure_is_closed_and_stable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, operator_config, db_path = _write_routing_fixture(tmp_path)

    def failed_plan(
        self: CliProposedGraphPlanner,
        goal: Goal,
        *,
        available_capabilities: Sequence[str],
        effective_policy_digest: str,
        harness_digest: str,
        max_nodes: int,
        max_wall_seconds: float,
    ) -> ProposedGraph:
        del (
            self,
            goal,
            available_capabilities,
            effective_policy_digest,
            harness_digest,
            max_nodes,
            max_wall_seconds,
        )
        raise ValueError("invalid planner schema")

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", failed_plan)
    result = cli.main(
        [
            "work",
            "plan this adaptively",
            "--repo",
            str(repository),
            "--operator-config",
            str(operator_config),
            "--db",
            str(db_path),
        ]
    )

    assert result == 7
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "failed"
    assert emitted["stable_code"] == "GRAPH_PLANNER_FAILED"
