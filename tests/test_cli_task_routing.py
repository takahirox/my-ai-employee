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
)
from ai_employee.domain.v2 import (
    DecisionOutcome,
    ExecutionResult,
    PolicyDecision,
    ProcessRequest,
)
from ai_employee.serialization import canonical_digest, canonical_json
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import GraphRunRecord, TaskGraphAcceptance
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


def _capture_planner_prompt(goal: Goal) -> dict[str, object]:
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

    assert proposal.graph == graph
    assert len(captured) == 1
    return json.loads(captured[0])


def test_planner_prompt_defaults_to_minimal_sufficient_and_preserves_explicit_breadth() -> None:
    local_goal = Goal(id="goal-local-fix", statement="Fix the local parser bug")
    broad_goal = Goal(
        id="goal-exhaustive-audit",
        statement="Exhaustively audit every authentication path for security defects",
    )

    local_prompt = _capture_planner_prompt(local_goal)
    broad_prompt = _capture_planner_prompt(broad_goal)
    instruction = local_prompt["instruction"]

    assert local_prompt["protocol"] == broad_prompt["protocol"] == "fleet-proposed-graph/1"
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
printf '%s' '{"complexity":8,"scale":6,'
printf '%s' '"required_capabilities":["edit_intent","process"],'
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
        )

    monkeypatch.setattr(CliProposedGraphPlanner, "plan", fake_plan)

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
