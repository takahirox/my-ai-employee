from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import (
    Budget,
    CompletionCriterion,
    Edge,
    Graph,
    Node,
    NodeKind,
    OutputContract,
)
from ai_employee.inspector import inspect_work_run
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import NodeRouteRecord
from ai_employee.task_planning import CliProposedGraphPlanner, ProposedGraph


def test_work_cli_defaults_to_adaptive_routing() -> None:
    args = cli.build_parser().parse_args(["work", "route this task"])

    assert args.routing_mode == "adaptive"
    assert args.strategy_set is None
    assert args.max_concurrency == 1


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
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, operator_config, tmp_path / "fleet.db"


def test_default_adaptive_routing_persists_selected_strategy(
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
            "--max-concurrency",
            "1",
        ]
    )

    assert result == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "planned"
    with SQLiteStore(db_path) as store:
        projection = inspect_work_run(store, emitted["run_id"])

    assessment = projection["routing"]["assessment"]
    selected = projection["routing"]["selected_strategy"]
    assessor = projection["routing"]["assessment_strategy"]
    assert projection["routing"]["strategy_set"] == "codex-all"
    assert projection["state"] == "planned"
    assert projection["run"]["worker"] == "codex_cli"
    assert selected["backend"] == "codex_cli"
    assert selected["model"] == "gpt-5.6-sol"
    assert selected["effort"] == "high"
    assert assessor["id"] == "sol"
    assert assessor["model"] == "gpt-5.6-sol"
    assert assessor["effort"] == "high"
    assert assessment["complexity"] > 2
    assert assessment["scale"] > 2


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


def test_default_adaptive_graph_execution_fails_closed_after_persisting_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, operator_config, db_path = _write_routing_fixture(tmp_path)

    def criterion(name: str) -> CompletionCriterion:
        return CompletionCriterion(
            id=f"criterion-{name}", description=f"{name} is complete"
        )

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
        goal: object,
        *,
        available_capabilities: object,
        effective_policy_digest: str,
        harness_digest: str,
        max_nodes: int,
        max_wall_seconds: float,
    ) -> ProposedGraph:
        del available_capabilities, max_nodes, max_wall_seconds
        assert hasattr(goal, "id")
        assert hasattr(goal, "statement")
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
            "--max-concurrency",
            "2",
        ]
    )

    assert result == 5
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "failed"
    assert emitted["stable_code"] == "GRAPH_EXECUTION_UNAVAILABLE"
    with SQLiteStore(db_path) as store:
        routes = store.list_records(
            "node_route_v2", NodeRouteRecord, run_id=emitted["run_id"]
        )
        proposal = store.list_records(
            "proposed_graph_v2", ProposedGraph, run_id=emitted["run_id"]
        )[0]
    assert routes == ()
    assert proposal.graph == graph
    assert proposal.planner_strategy.model == "gpt-5.6-sol"
    assert proposal.planner_strategy.effort == "high"
