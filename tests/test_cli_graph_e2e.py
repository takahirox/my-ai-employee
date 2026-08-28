from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_employee import cli
from ai_employee.domain import SemanticTaskType
from ai_employee.domain.v2 import ApprovalRecord, PromotionRecord, WorkerRequest
from ai_employee.graph_composition import GraphPatchCompositionRecord
from ai_employee.graph_evaluation import (
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationRequest,
)
from ai_employee.orchestration import WorkRun
from ai_employee.project import discover_project_harness
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeRouteRecord,
    TaskGraphAcceptance,
)
from ai_employee.task_planning import ProposedGraph


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".fleet").mkdir()
    for name in ("a", "b", "c"):
        (repository / f"{name}.txt").write_text(f"{name}-before\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()

    planner = tmp_path / "fake-assessment-planner"
    _write_executable(
        planner,
        textwrap.dedent(
            """
            import json
            import sys

            prompt = json.load(sys.stdin)
            protocol = prompt["protocol"]
            if protocol == "fleet-semantic-task-assessment/2":
                structured = {
                    "schema_version": "1",
                    "task_type": "architecture",
                    "reasoning_class": "deep",
                    "scope": "multi_component",
                    "ambiguity": "low",
                    "reasons": ["bounded fork and join"],
                }
            elif protocol == "fleet-proposed-graph/2":
                def node(name, complexity):
                    return {
                        "id": name,
                        "kind": "function",
                        "name": name,
                        "objective": f"change {name}.txt",
                        "output_contract": {"id": f"contract-{name}"},
                        "required_capabilities": ["edit_intent", "process"],
                        "completion_criteria": [
                            {
                                "id": f"criterion-{name}",
                                "description": f"{name} produced an exact patch",
                                "required_artifact_ids": ["workspace_patch"],
                            }
                        ],
                        "semantic_profile": {
                            "task_type": "mechanical" if name == "a" else "architecture",
                            "reasoning_class": "mechanical" if name == "a" else "deep",
                            "scope": "bounded" if name == "a" else "broad",
                            "ambiguity": "low",
                            "reasons": ["bounded fixture route"],
                        },
                        "complexity": complexity,
                        "scale": complexity,
                    }

                structured = {
                    "schema_version": "2",
                    "goal_id": prompt["goal"]["id"],
                    "graph": {
                        "id": "cli-fork-join",
                        "nodes": [node("a", 2), node("b", 8), node("c", 8)],
                        "edges": [
                            {"id": "a-c", "source_id": "a", "target_id": "c"},
                            {"id": "b-c", "source_id": "b", "target_id": "c"},
                        ],
                        "entry_node_ids": ["a", "b"],
                        "terminal_node_ids": ["c"],
                        "budget": {
                            "max_attempts": 3,
                            "max_nodes": 3,
                            "max_wall_seconds": 30.0,
                        },
                    },
                }
            else:
                raise SystemExit(f"unexpected protocol: {protocol}")
            print(json.dumps({"structured_output": structured}))
            """
        ),
    )

    worker = tmp_path / "fake-worker"
    worker_body = textwrap.dedent(
        """
        import json
        import sys
        import time
        from pathlib import Path

        state = Path(__STATE__)
        if sys.argv[1:] == ["--version"]:
            print("fake-worker 1")
            raise SystemExit(0)
        if sys.argv[1:] == ["--help"]:
            print("Usage: fake-worker exec")
            raise SystemExit(0)
        prompt = json.load(sys.stdin)
        protocol = prompt["protocol"]
        if protocol == "fleet-semantic-task-assessment/2":
            print(json.dumps({
                "schema_version": "1",
                "task_type": "architecture",
                "reasoning_class": "deep",
                "scope": "multi_component",
                "ambiguity": "low",
                "reasons": ["bounded fork and join"],
            }))
            raise SystemExit(0)
        if protocol == "fleet-proposed-graph/2":
            def node(node_name, complexity):
                return {
                    "id": node_name,
                    "kind": "function",
                    "name": node_name,
                    "objective": f"change {node_name}.txt",
                    "output_contract": {"id": f"contract-{node_name}"},
                    "required_capabilities": ["edit_intent", "process"],
                    "completion_criteria": [{
                        "id": f"criterion-{node_name}",
                        "description": f"{node_name} produced an exact patch",
                        "required_artifact_ids": ["workspace_patch"],
                    }],
                    "semantic_profile": {
                        "task_type": "mechanical" if node_name == "a" else "architecture",
                        "reasoning_class": "mechanical" if node_name == "a" else "deep",
                        "scope": "bounded" if node_name == "a" else "broad",
                        "ambiguity": "low",
                        "reasons": ["bounded fixture route"],
                    },
                    "complexity": complexity,
                    "scale": complexity,
                }
            print(json.dumps({
                "schema_version": "2",
                "goal_id": prompt["goal"]["id"],
                "graph": {
                    "id": "cli-fork-join",
                    "nodes": [node("a", 2), node("b", 8), node("c", 8)],
                    "edges": [
                        {"id": "a-c", "source_id": "a", "target_id": "c"},
                        {"id": "b-c", "source_id": "b", "target_id": "c"},
                    ],
                    "entry_node_ids": ["a", "b"],
                    "terminal_node_ids": ["c"],
                    "budget": {
                        "max_attempts": 3,
                        "max_nodes": 3,
                        "max_wall_seconds": 30.0,
                    },
                },
            }))
            raise SystemExit(0)
        if protocol != "fleet-worker-proposal/2":
            raise SystemExit(f"unexpected worker protocol: {protocol}")
        name = Path(prompt["goal"].split()[-1]).stem
        if name in {"a", "b"}:
            (state / f"{name}.started").write_text("started")
            peer = "b" if name == "a" else "a"
            deadline = time.monotonic() + 5
            while not (state / f"{peer}.started").exists():
                if time.monotonic() >= deadline:
                    raise SystemExit("fork nodes were not concurrent")
                time.sleep(0.01)
            while (state / "hold").exists() and not (state / "release").exists():
                if time.monotonic() >= deadline:
                    raise SystemExit("timed out waiting for lifecycle control")
                time.sleep(0.01)
        else:
            if not all((state / f"{parent}.done").exists() for parent in ("a", "b")):
                raise SystemExit("join node ran before both parents completed")
        patch = (
            f"diff --git a/{name}.txt b/{name}.txt\\n"
            f"--- a/{name}.txt\\n"
            f"+++ b/{name}.txt\\n"
            "@@ -1 +1 @@\\n"
            f"-{name}-before\\n"
            f"+{name}-after\\n"
        )
        created = "2026-01-01T00:00:00Z"
        run_id = prompt["run_id"]
        envelope = {
            "schema_version": "2",
            "proposals": [
                {
                    "schema_version": "2",
                    "id": f"proposal-{name}",
                    "run_id": run_id,
                    "created_at": created,
                    "worker_id": "fake",
                    "kind": "edit_intent",
                    "payload": {
                        "schema_version": "2",
                        "id": f"edit-{name}",
                        "run_id": run_id,
                        "created_at": created,
                        "paths": [f"{name}.txt"],
                        "summary": f"change {name}",
                        "unified_diff": patch,
                    },
                    "reason": "deterministic CLI fixture edit",
                    "expected_artifact_kinds": ["workspace_patch"],
                }
            ],
            "assistant_note": "",
            "usage_json": "{}",
        }
        print(json.dumps(envelope))
        (state / f"{name}.done").write_text("done")
        """
    ).replace("__STATE__", repr(str(state)))
    _write_executable(worker, worker_body)

    verifier = tmp_path / "fake-parent-verifier"
    verifier_body = textwrap.dedent(
        """
        from pathlib import Path

        state = Path(__STATE__)
        root = Path.cwd()
        for name in ("a", "b", "c"):
            if (root / f"{name}.txt").read_text() != f"{name}-after\\n":
                raise SystemExit(f"composed candidate missing {name}")
        (state / "parent.verified").write_text(str(root))
        """
    ).replace("__STATE__", repr(str(state)))
    _write_executable(verifier, verifier_body)

    (repository / ".fleet" / "project.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "commands": {"parent-test": {"argv": [str(verifier)]}},
                "paths": {
                    "writable": ["a.txt", "b.txt", "c.txt"],
                    "protected": [".git/**"],
                },
                "verification": {"required": ["parent-test"]},
                "worker": {
                    "allowed": ["codex_cli"],
                    "allowed_strategy_ids": ["low", "high", "planner"],
                    "adaptive_routing": True,
                    "local_backend": False,
                },
                "budgets": {"wall_seconds": 30.0, "processes": 20},
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

    operator = tmp_path / "operator.json"
    operator.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workers": {
                    "codex_cli": {"executable": str(worker)},
                },
                "routing": {
                    "default_strategy_set": "all",
                    "default_assessment_strategy": "planner",
                    "strategy_sets": {"all": ["low", "high", "planner"]},
                    "strategies": [
                        {
                            "id": "low",
                            "backend": "codex_cli",
                            "model": "low-model",
                            "effort": "medium",
                            "capabilities": ["edit_intent", "process"],
                            "max_complexity": 2,
                            "max_scale": 2,
                        },
                        {
                            "id": "high",
                            "backend": "codex_cli",
                            "model": "high-model",
                            "effort": "high",
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 3,
                            "min_scale": 3,
                        },
                        {
                            "id": "planner",
                            "backend": "codex_cli",
                            "model": "planner-model",
                            "effort": "high",
                            "capabilities": ["process"],
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, operator, tmp_path / "fleet.db", state


def test_cli_graph_handoff_inspects_approves_promotes_and_replays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, state = _fixture(tmp_path)
    original_head = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()

    result = cli.main(
        [
            "work",
            "change a and b concurrently, then change c",
            "--repo",
            str(repository),
            "--operator-config",
            str(operator),
            "--db",
            str(database),
            "--max-concurrency",
            "2",
            "--non-interactive",
        ]
    )

    assert result == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["status"] == "ready_to_promote"
    assert emitted["stable_code"] is None
    run_id = emitted["run_id"]
    harness = discover_project_harness(repository)
    assert len(harness.evaluators) == 1
    assert harness.evaluators[0].provider_id == "process.harness"

    with SQLiteStore(database) as store:
        graph_run = store.get("graph_run_v2", run_id, GraphRunRecord)
        acceptance = store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )[0]
        proposal = store.list_records("proposed_graph_v2", ProposedGraph, run_id=run_id)[0]
        routes = store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)
        requests = store.list_records("worker_request_v2", WorkerRequest)
        work_runs = store.list_records("work_run_v2", WorkRun)
        composition = store.get(
            "graph_patch_composition_v2",
            graph_run.composition_id or "missing",
            GraphPatchCompositionRecord,
        )
        evaluation = store.get(
            "parent_candidate_evaluation_v2",
            graph_run.parent_evaluation_id or "missing",
            ParentCandidateEvaluationRecord,
        )
        evaluation_request = store.get(
            "parent_candidate_evaluation_request_v2",
            next(
                item.id
                for item in store.list_records(
                    "parent_candidate_evaluation_request_v2",
                    ParentCandidateEvaluationRequest,
                    run_id=run_id,
                )
            ),
            ParentCandidateEvaluationRequest,
        )
        approvals = store.list_records("approval_v2", ApprovalRecord, run_id=run_id)

    assert graph_run.status == "ready_to_promote"
    assert acceptance.harness_digest == canonical_digest(harness)
    assert acceptance.proposed_graph_digest == proposal.content_digest
    assert proposal.planner_strategy.id == "planner"
    assert proposal.planner_strategy.model == "planner-model"
    assert proposal.planner_strategy.effort == "high"
    route_by_node = {item.node_id: item for item in routes}
    assert set(route_by_node) == {"a", "b", "c"}
    assert route_by_node["a"].selected_strategy.model == "low-model"
    assert route_by_node["a"].assessment.semantic_profile is not None
    assert route_by_node["a"].assessment.semantic_profile.task_type is SemanticTaskType.MECHANICAL
    assert route_by_node["a"].selected_strategy.effort == "medium"
    for name in ("b", "c"):
        assert route_by_node[name].selected_strategy.model == "high-model"
        assert route_by_node[name].selected_strategy.effort == "high"
        assert route_by_node[name].assessment.semantic_profile is not None
    request_by_node = {item.node_id: item for item in requests if item.graph_run_id == run_id}
    assert set(request_by_node) == {"a", "b", "c"}
    assert len({item.id for item in request_by_node.values()}) == 3
    assert len({item.run_id for item in request_by_node.values()}) == 3
    assert all(
        item.accepted_graph_revision_digest == graph_run.accepted_graph_revision_digest
        for item in request_by_node.values()
    )
    work_by_node = {item.node_id: item for item in work_runs}
    assert set(work_by_node) == {"a", "b", "c"}
    assert all(item.pending_approval_id is None for item in work_by_node.values())
    assert len(approvals) == 1
    approval = approvals[0]
    assert graph_run.promotion_approval_id == approval.id
    assert graph_run.promotion_approval_request_digest == graph_run.parent_candidate_digest
    assert tuple(item.node_id for item in composition.ordered_inputs) == ("a", "b", "c")
    assert evaluation.status == "ready_to_promote"
    assert evaluation.decision.value == "PASS"
    assert len(evaluation_request.verification_bindings) == 1
    binding = evaluation_request.verification_bindings[0]
    assert binding.specification.provider_id == "process.harness"
    assert binding.process_request.id != binding.harness_command_ref
    assert evaluation.verification_request_digests == (binding.process_request.content_digest,)
    composition_root = Path(composition.composition_workspace.isolated_worktree)
    assert Path((state / "parent.verified").read_text()) == composition_root
    assert all((state / f"{name}.started").exists() for name in ("a", "b"))
    assert all((state / f"{name}.done").exists() for name in ("a", "b", "c"))
    assert (
        subprocess.check_output(
            ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
        ).strip()
        == original_head
    )
    assert (
        subprocess.check_output(("git", "-C", str(repository), "status", "--porcelain"), text=True)
        == ""
    )
    for name in ("a", "b", "c"):
        assert (repository / f"{name}.txt").read_text() == f"{name}-before\n"

    assert cli.main(["inspect", run_id, "--db", str(database)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["kind"] == "graph_run"
    assert inspected["state"] == "ready_to_promote"
    assert {item["node_id"] for item in inspected["routes"]} == {"a", "b", "c"}
    assert all(item["assessment"]["semantic_profile"] is not None for item in inspected["routes"])
    assert inspected["candidate_patch"]["id"] == graph_run.parent_candidate_artifact_id
    assert len(inspected["approvals"]) == 1

    assert cli.main(["diff", run_id, "--stat", "--db", str(database)]) == 0
    stat = json.loads(capsys.readouterr().out)
    assert stat["run_id"] == run_id
    assert stat["bytes"] > 0
    assert cli.main(["diff", run_id, "--db", str(database)]) == 0
    candidate_diff = capsys.readouterr().out
    assert all(f"diff --git a/{name}.txt b/{name}.txt" in candidate_diff for name in "abc")

    assert (
        cli.main(
            [
                "promote",
                run_id,
                "--patch-digest",
                graph_run.parent_candidate_digest or "missing",
                "--db",
                str(database),
            ]
        )
        == 4
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["stable_code"] == "PROMOTION_APPROVAL_REQUIRED"
    assert all((repository / f"{name}.txt").read_text() == f"{name}-before\n" for name in "abc")

    assert (
        cli.main(
            [
                "approvals",
                "approve",
                approval.id,
                "--request-digest",
                approval.request_digest,
                "--db",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    promote_argv = [
        "promote",
        run_id,
        "--patch-digest",
        graph_run.parent_candidate_digest or "missing",
        "--db",
        str(database),
    ]
    assert cli.main(promote_argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert all((repository / f"{name}.txt").read_text() == f"{name}-after\n" for name in "abc")
    promoted_diff = subprocess.check_output(
        ("git", "-C", str(repository), "diff", "--binary", "HEAD", "--"), text=True
    )

    assert cli.main(promote_argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert (
        subprocess.check_output(
            ("git", "-C", str(repository), "diff", "--binary", "HEAD", "--"),
            text=True,
        )
        == promoted_diff
    )
    assert cli.main(["replay", run_id, "--db", str(database)]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["kind"] == "graph_replay"
    assert replay["promotion_invocations"] == 0
    assert cli.main(["inspect", run_id, "--db", str(database)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"

    with SQLiteStore(database) as store:
        assert len(store.list_records("promotion_v2", PromotionRecord, run_id=run_id)) == 1


def test_cli_resumes_paused_graph_with_exact_persisted_operator_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, state = _fixture(tmp_path)
    (state / "hold").write_text("hold", encoding="utf-8")
    argv = [
        "work",
        "change a and b concurrently, then change c",
        "--repo",
        str(repository),
        "--operator-config",
        str(operator),
        "--db",
        str(database),
        "--max-concurrency",
        "2",
        "--non-interactive",
    ]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cli.main, argv)
        deadline = time.monotonic() + 10
        while not all((state / f"{name}.started").exists() for name in ("a", "b")):
            if time.monotonic() >= deadline:
                pytest.fail("fork nodes did not start")
            time.sleep(0.01)
        with SQLiteStore(database) as store:
            running = store.list_records("graph_run_v2", GraphRunRecord)
            assert len(running) == 1
            run_id = running[0].id
            store.request_control(run_id, "pause")
        (state / "release").write_text("release", encoding="utf-8")
        assert future.result(timeout=10) == 5

    paused_output = json.loads(capsys.readouterr().out)
    assert paused_output["status"] == "paused"
    assert paused_output["stable_code"] == "GRAPH_PAUSED"
    with SQLiteStore(database) as store:
        paused = store.get("graph_run_v2", run_id, GraphRunRecord)
        requests_before = store.list_records("worker_request_v2", WorkerRequest)
        proposals_before = store.list_records("proposed_graph_v2", ProposedGraph, run_id=run_id)
        compositions_before = store.list_records(
            "graph_patch_composition_v2",
            GraphPatchCompositionRecord,
            run_id=run_id,
        )

    assert paused.status == "paused"
    assert paused.generation == 0
    assert paused.operator_config_path == str(operator.resolve())
    assert {item.node_id for item in requests_before} == {"a", "b"}
    assert len(proposals_before) == 1
    assert compositions_before == ()
    assert not (state / "c.done").exists()

    assert cli.main(["resume", run_id, "--db", str(database)]) == 0
    resumed_output = json.loads(capsys.readouterr().out)
    assert resumed_output["status"] == "ready_to_promote"
    with SQLiteStore(database) as store:
        resumed = store.get("graph_run_v2", run_id, GraphRunRecord)
        requests = store.list_records("worker_request_v2", WorkerRequest)
        proposals = store.list_records("proposed_graph_v2", ProposedGraph, run_id=run_id)
        work_runs = store.list_records("work_run_v2", WorkRun)
        compositions = store.list_records(
            "graph_patch_composition_v2",
            GraphPatchCompositionRecord,
            run_id=run_id,
        )
        evaluations = store.list_records(
            "parent_candidate_evaluation_v2",
            ParentCandidateEvaluationRecord,
            run_id=run_id,
        )

    assert resumed.generation == 1
    assert len(proposals) == 1
    assert len(compositions) == 1
    assert len(evaluations) == 1
    assert [item.node_id for item in requests].count("a") == 1
    assert [item.node_id for item in requests].count("b") == 1
    assert [item.node_id for item in requests].count("c") == 1
    assert [item.node_id for item in work_runs].count("a") == 1
    assert [item.node_id for item in work_runs].count("b") == 1
    assert [item.node_id for item in work_runs].count("c") == 1
    join_request = next(item for item in requests if item.node_id == "c")
    assert join_request.generation == 1
    assert {item.generation for item in join_request.predecessor_outputs} == {1}
    assert {item.result_generation for item in join_request.predecessor_outputs} == {0}
    assert (state / "c.done").exists()


def test_cli_graph_promotion_repository_conflict_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, _ = _fixture(tmp_path)
    assert (
        cli.main(
            [
                "work",
                "change a and b concurrently, then change c",
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--db",
                str(database),
                "--max-concurrency",
                "2",
                "--non-interactive",
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    run_id = emitted["run_id"]
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", run_id, GraphRunRecord)
        approval = store.get("approval_v2", run.promotion_approval_id or "missing", ApprovalRecord)
    assert (
        cli.main(
            [
                "approvals",
                "approve",
                approval.id,
                "--request-digest",
                approval.request_digest,
                "--db",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    (repository / "a.txt").write_text("operator-conflict\n", encoding="utf-8")

    assert (
        cli.main(
            [
                "promote",
                run_id,
                "--patch-digest",
                run.parent_candidate_digest or "missing",
                "--db",
                str(database),
            ]
        )
        == 8
    )
    failure = json.loads(capsys.readouterr().out)
    assert failure["stable_code"] == "WORKSPACE_CONFLICT"
    assert (repository / "a.txt").read_text() == "operator-conflict\n"
    assert (repository / "b.txt").read_text() == "b-before\n"
    assert (repository / "c.txt").read_text() == "c-before\n"
    with SQLiteStore(database) as store:
        assert store.get("graph_run_v2", run_id, GraphRunRecord).status == "ready_to_promote"
        assert store.list_records("promotion_v2", PromotionRecord, run_id=run_id) == ()
