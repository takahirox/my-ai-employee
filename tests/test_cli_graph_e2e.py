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
from ai_employee.config import load_operator_config
from ai_employee.domain import (
    AcceptedGraphRevision,
    HarnessApprovals,
    HarnessNetwork,
    HarnessReview,
    RoutingMode,
    SemanticTaskType,
)
from ai_employee.domain.harness import NetworkMode as HarnessNetworkMode
from ai_employee.domain.v2 import (
    ApprovalRecord,
    ApprovalRequest,
    DecisionOutcome,
    PolicyDecision,
    PromotionRecord,
    WorkerRequest,
)
from ai_employee.graph_composition import GraphPatchCompositionRecord
from ai_employee.graph_evaluation import (
    GraphCandidateEvaluator,
    ParentCandidateEvaluationRecord,
    ParentCandidateEvaluationRequest,
    ParentVerificationBinding,
)
from ai_employee.graph_execution import GraphExecutionService
from ai_employee.inspector import inspect_graph_run
from ai_employee.orchestration import WorkRun
from ai_employee.project import discover_project_harness
from ai_employee.promotion_approval import (
    PromotionApprovalTrustKernel,
    PromotionPolicyDecision,
    validate_exact_parent_evidence_store,
)
from ai_employee.run_explanation import explain_any_run
from ai_employee.serialization import canonical_digest, project_harness_digest
from ai_employee.services_v2 import DigestApprovalService
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import (
    GraphRunRecord,
    NodeRouteRecord,
    NodeSemanticAssessmentRecord,
    TaskGraphAcceptance,
)
from ai_employee.task_planning import ProposedGraph
from ai_employee.task_review import TaskReviewDecision


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _fixture(tmp_path: Path, *, task_review: bool = True) -> tuple[Path, Path, Path, Path]:
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
                node_a = prompt["goal"] == "change a.txt"
                structured = {
                    "schema_version": "1",
                    "task_type": "mechanical" if node_a else "architecture",
                    "reasoning_class": "mechanical" if node_a else "deep",
                    "scope": "bounded" if node_a else "multi_component",
                    "ambiguity": "low",
                    "reasons": ["independent accepted-node assessment"],
                }
            elif protocol == "fleet-plan-review/2":
                structured = {"schema_version": "2", "findings": []}
            elif protocol == "fleet-task-result-review/2":
                structured = {
                    "schema_version": "2",
                    "findings": [],
                    "reviewed_criterion_ids": prompt["request"]["criterion_ids"],
                    "limitations": [],
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
                            "task_type": "mechanical",
                            "reasoning_class": "mechanical",
                            "scope": "bounded",
                            "ambiguity": "low",
                            "reasons": ["planner hint only"],
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
            node_a = prompt["goal"] == "change a.txt"
            print(json.dumps({
                "schema_version": "1",
                "task_type": "mechanical" if node_a else "architecture",
                "reasoning_class": "mechanical" if node_a else "deep",
                "scope": "bounded" if node_a else "multi_component",
                "ambiguity": "low",
                "reasons": ["independent accepted-node assessment"],
            }))
            raise SystemExit(0)
        if protocol == "fleet-plan-review/2":
            print(json.dumps({"schema_version": "2", "findings": []}))
            raise SystemExit(0)
        if protocol == "fleet-task-result-review/2":
            print(json.dumps({
                "schema_version": "2",
                "findings": [],
                "reviewed_criterion_ids": prompt["request"]["criterion_ids"],
                "limitations": [],
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
                        "task_type": "mechanical",
                        "reasoning_class": "mechanical",
                        "scope": "bounded",
                        "ambiguity": "low",
                        "reasons": ["planner hint only"],
                    },
                    "complexity": complexity,
                    "scale": complexity,
                }
            print(json.dumps({
                "schema_version": "2",
                "goal_id": prompt["goal"]["id"],
                "graph": {
                    "id": "cli-fork-join",
                    "nodes": [node("a", 8), node("b", 1), node("c", 1)],
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
                "verification": {
                    "required": ["parent-test"],
                    **({"review": {"independent_task_review": True}} if task_review else {}),
                },
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
                    **({"default_task_reviewer_strategy": "planner"} if task_review else {}),
                    "strategy_sets": {"all": ["low", "high", "planner"]},
                    "strategies": [
                        {
                            "id": "low",
                            "backend": "codex_cli",
                            "model": "low-model",
                            "effort": "medium",
                            "planner_eligible": True,
                            "capabilities": ["edit_intent", "process"],
                            "max_complexity": 2,
                            "max_scale": 2,
                        },
                        {
                            "id": "high",
                            "backend": "codex_cli",
                            "model": "high-model",
                            "effort": "high",
                            "planner_eligible": True,
                            "capabilities": ["edit_intent", "process"],
                            "min_complexity": 3,
                            "min_scale": 3,
                        },
                        {
                            "id": "planner",
                            "backend": "codex_cli",
                            "model": "planner-model",
                            "effort": "high",
                            "capabilities": ["edit_intent", "process"],
                            "planner_eligible": True,
                            **({"task_reviewer_eligible": True} if task_review else {}),
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    return repository, operator, tmp_path / "fleet.db", state


def _enable_policy_auto_approval(
    repository: Path, operator: Path, *, project_opt_in: bool = True
) -> None:
    if project_opt_in:
        harness_path = repository / ".fleet" / "project.json"
        harness = json.loads(harness_path.read_text(encoding="utf-8"))
        harness["approvals"] = {"promotion": "policy"}
        harness_path.write_text(json.dumps(harness), encoding="utf-8")
        subprocess.run(("git", "-C", str(repository), "add", ".fleet/project.json"), check=True)
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
                "enable bounded auto approval",
            ),
            check=True,
        )
    config = json.loads(operator.read_text(encoding="utf-8"))
    config["promotion_auto_approval"] = {
        "mode": "policy",
        "allowed_repositories": [str(repository.resolve())],
        "max_risk": 0,
        "max_changed_files": 5,
        "max_patch_bytes": 100000,
    }
    operator.write_text(json.dumps(config), encoding="utf-8")


def test_one_sided_policy_opt_in_persists_manual_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, _state = _fixture(tmp_path, task_review=False)
    _enable_policy_auto_approval(repository, operator, project_opt_in=False)
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
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    with SQLiteStore(database) as store:
        approval = store.list_records("approval_v2", ApprovalRecord, run_id=run_id)[0]
        policy = store.list_records(
            "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run_id
        )[0]
        explained = explain_any_run(store, run_id)
    assert approval.decision == "pending"
    assert approval.authorization_kind == "manual"
    assert policy.decision == "manual_required"
    assert policy.reason_code == "project_policy_opt_in_missing"
    assert explained["final_outcome"]["promotion_approval"]["authorization_kind"] == "manual"


def test_policy_auto_approval_is_explicit_bound_and_still_requires_promote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, _state = _fixture(tmp_path, task_review=False)
    _enable_policy_auto_approval(repository, operator)

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
    assert emitted["status"] == "ready_to_promote"
    run_id = emitted["run_id"]

    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", run_id, GraphRunRecord)
        acceptance = store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )[0]
        composition = store.get(
            "graph_patch_composition_v2",
            run.composition_id or "missing",
            GraphPatchCompositionRecord,
        )
        evaluation = store.get(
            "parent_candidate_evaluation_v2",
            run.parent_evaluation_id or "missing",
            ParentCandidateEvaluationRecord,
        )
        approvals = store.list_records("approval_v2", ApprovalRecord, run_id=run_id)
        decisions = store.list_records(
            "promotion_policy_decision_v2", PromotionPolicyDecision, run_id=run_id
        )
        assert len(approvals) == len(decisions) == 1
        approval = approvals[0]
        authority = decisions[0]
        inspected = inspect_graph_run(store, run_id)
        explained = explain_any_run(store, run_id)
        harness = discover_project_harness(repository)
        replay = GraphCandidateEvaluator(
            store,
            None,  # type: ignore[arg-type]
            harness,
            lambda _snapshot: None,  # type: ignore[arg-type,return-value]
            lambda _request: None,  # type: ignore[arg-type,return-value]
        ).replay(evaluation.id)
        operator_config = load_operator_config(operator)
        assert (
            validate_exact_parent_evidence_store(
                store, run, acceptance.accepted_revision, evaluation, harness
            )
            == replay
        )
        duplicate_ledger = replay.evaluation_ledgers[0].model_copy(
            update={"id": "duplicate-parent-evaluation-ledger"}
        )
        store.put("evaluation_evidence_ledger_v2", duplicate_ledger, run_id=run_id)
        with pytest.raises(ValueError, match="ambiguous"):
            validate_exact_parent_evidence_store(
                store, run, acceptance.accepted_revision, evaluation, harness
            )
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind=? AND record_id=?",
                ("evaluation_evidence_ledger_v2", duplicate_ledger.id),
            )
        foreign_ledger = replay.evaluation_ledgers[0].model_copy(
            update={"id": "foreign-parent-evaluation-ledger", "run_id": "foreign-run"}
        )
        store.put("evaluation_evidence_ledger_v2", foreign_ledger, run_id=run_id)
        with pytest.raises(ValueError, match=r"foreign|ambiguous"):
            validate_exact_parent_evidence_store(
                store, run, acceptance.accepted_revision, evaluation, harness
            )
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind=? AND record_id=?",
                ("evaluation_evidence_ledger_v2", foreign_ledger.id),
            )
        parent_request = next(
            item
            for item in store.list_records(
                "parent_candidate_evaluation_request_v2",
                ParentCandidateEvaluationRequest,
                run_id=run_id,
            )
            if item.content_digest == evaluation.request_digest
        )
        forged_binding = ParentVerificationBinding.model_validate(
            {
                **parent_request.verification_bindings[0].model_dump(mode="python"),
                "harness_evaluator_id": "unrequested-self-consistent-evaluator",
            },
            strict=True,
        )
        forged_request = ParentCandidateEvaluationRequest.model_validate(
            {
                **parent_request.model_dump(mode="python"),
                "id": "forged-parent-evaluation-request",
                "verification_bindings": (
                    forged_binding,
                    *parent_request.verification_bindings[1:],
                ),
                "content_digest": None,
            },
            strict=True,
        )
        forged_evaluation = ParentCandidateEvaluationRecord.model_validate(
            {
                **evaluation.model_dump(mode="python"),
                "id": "forged-parent-evaluation",
                "request_digest": forged_request.content_digest,
                "content_digest": None,
            },
            strict=True,
        )
        store.put("parent_candidate_evaluation_request_v2", forged_request, run_id=run_id)
        store.put("parent_candidate_evaluation_v2", forged_evaluation, run_id=run_id)
        with pytest.raises(ValueError, match="required Harness order"):
            validate_exact_parent_evidence_store(
                store,
                run,
                acceptance.accepted_revision,
                forged_evaluation,
                harness,
            )
        with store.transaction() as connection:
            connection.execute(
                "DELETE FROM records WHERE kind=? AND record_id=?",
                ("approval_v2", approval.id),
            )
        crashed_run = run.model_copy(
            update={
                "status": "failed",
                "failure_code": "PARENT_EVALUATION_UNAVAILABLE",
                "composition_id": None,
                "composition_digest": None,
                "parent_candidate_artifact_id": None,
                "parent_candidate_digest": None,
                "parent_evaluation_id": None,
                "parent_evaluation_digest": None,
                "goal_evaluator_digest": None,
                "promotion_approval_id": None,
                "promotion_approval_request_digest": None,
            }
        )
        store.put("graph_run_v2", crashed_run, run_id=run_id, revision=crashed_run.generation + 1)
        recovery_service = GraphExecutionService(
            store,
            lambda *_args: None,  # type: ignore[arg-type,return-value]
            None,
            run.execution_strategies,
            repository=run.repository or "missing",
            base_commit=run.base_commit or "0" * 40,
            routing_mode=run.routing_mode,
            fixed_strategy_id=run.fixed_strategy_id,
            allowed_strategy_ids=run.allowed_strategy_ids,
            allowed_backends=run.allowed_backends,
            local_backend_allowed=run.local_backend_allowed,
            parent_evaluator=GraphCandidateEvaluator(
                store,
                None,  # type: ignore[arg-type]
                harness,
                lambda _snapshot: None,  # type: ignore[arg-type,return-value]
                lambda _request: None,  # type: ignore[arg-type,return-value]
            ),
            approval_service=DigestApprovalService(store, operator_label="local-operator"),
            promotion_approval_policy=PromotionApprovalTrustKernel(
                harness,
                operator_config.promotion_auto_approval,
                harness_digest=run.harness_digest,
                operator_config_digest=run.operator_config_digest or "0" * 64,
            ),
            operator_config_digest=run.operator_config_digest,
            operator_config_path=run.operator_config_path,
            strategy_set=run.strategy_set,
        )
        recovered_run = recovery_service._recover_policy_approval_pointer(crashed_run)
        assert recovered_run is not None
        assert recovered_run.status == "ready_to_promote"
        assert recovered_run.parent_evaluation_digest == evaluation.content_digest
        approval = store.get(
            "approval_v2", recovered_run.promotion_approval_id or "missing", ApprovalRecord
        )
        run = recovered_run
        approval_request = next(
            item
            for item in store.list_records("approval_request_v2", ApprovalRequest, run_id=run_id)
            if item.request_digest == approval.request_digest
        )
        approval_decision = next(
            item
            for item in store.list_records("policy_decision_v2", PolicyDecision, run_id=run_id)
            if item.request_digest == approval.request_digest
            and item.outcome is DecisionOutcome.APPROVAL_REQUIRED
        )
        assert (
            DigestApprovalService(store, operator_label="ignored").request_policy_auto(
                approval_request, approval_decision, authority
            )
            == approval
        )
        with pytest.raises(ValueError, match="stale or mismatched"):
            DigestApprovalService(store, operator_label="ignored").request_policy_auto(
                approval_request,
                approval_decision,
                authority.model_copy(update={"run_id": "foreign-run"}),
            )
        duplicate = approval.model_copy(update={"id": "duplicate-policy-approval"})
        store.put("approval_v2", duplicate, run_id=run_id)
        with pytest.raises(ValueError, match="ambiguous"):
            DigestApprovalService(store, operator_label="ignored").request_policy_auto(
                approval_request, approval_decision, authority
            )
    assert authority.decision == "policy_auto_approved"
    assert authority.reason_code == "eligible_low_risk_exact_evidence"
    assert approval.decision == "approved"
    assert approval.authorization_kind == "policy_auto"
    assert approval.authorization_digest == authority.content_digest
    assert approval.accepted_graph_revision_digest == run.accepted_graph_revision_digest
    assert approval.parent_evaluation_digest == run.parent_evaluation_digest
    assert approval.verification_evidence_digests
    assert approval.evaluation_evidence_digests
    assert inspected["promotion_policy_decisions"][0]["content_digest"] == authority.content_digest
    story = explained["final_outcome"]["promotion_approval"]
    assert story["binding"] == "bound"
    assert story["authorization_kind"] == "policy_auto"
    assert story["reason_code"] == "eligible_low_risk_exact_evidence"
    assert all((repository / f"{name}.txt").read_text() == f"{name}-before\n" for name in "abc")
    assert cli.main(["replay", run_id, "--db", str(database)]) == 0
    replayed_output = json.loads(capsys.readouterr().out)
    assert replayed_output["promotion_invocations"] == 0
    assert (
        replayed_output["inspection"]["promotion_policy_decisions"][0]["content_digest"]
        == authority.content_digest
    )
    assert all((repository / f"{name}.txt").read_text() == f"{name}-before\n" for name in "abc")

    def resolve_with(
        *,
        selected_harness: object = harness,
        policy: object = operator_config.promotion_auto_approval,
        selected_replay: object = replay,
        selected_run: GraphRunRecord = run,
        selected_acceptance: object = acceptance.accepted_revision,
        selected_composition: GraphPatchCompositionRecord = composition,
        selected_evaluation: ParentCandidateEvaluationRecord = evaluation,
    ) -> PromotionPolicyDecision:
        return PromotionApprovalTrustKernel(
            selected_harness,  # type: ignore[arg-type]
            policy,  # type: ignore[arg-type]
            harness_digest=run.harness_digest,
            operator_config_digest=run.operator_config_digest or "0" * 64,
        ).resolve(
            selected_run,
            selected_acceptance,  # type: ignore[arg-type]
            selected_composition,
            selected_evaluation,
            selected_replay,  # type: ignore[arg-type]
            evidence_storage_valid=True,
        )

    assert resolve_with(selected_replay=None).reason_code == "evidence_replay_unavailable"
    assert (
        resolve_with(
            selected_harness=harness.model_copy(
                update={"approvals": HarnessApprovals(promotion="required")}
            )
        ).reason_code
        == "project_policy_opt_in_missing"
    )
    assert (
        resolve_with(
            policy=operator_config.promotion_auto_approval.model_copy(
                update={"allowed_repositories": ()}
            )
        ).reason_code
        == "repository_not_allowed"
    )
    assert (
        resolve_with(
            selected_harness=harness.model_copy(
                update={"network": HarnessNetwork(mode=HarnessNetworkMode.RESTRICTED)}
            )
        ).reason_code
        == "network_or_install_side_effect"
    )
    assert (
        resolve_with(
            policy=operator_config.promotion_auto_approval.model_copy(
                update={"max_changed_files": 1}
            )
        ).reason_code
        == "changed_file_limit_exceeded"
    )
    assert (
        resolve_with(
            policy=operator_config.promotion_auto_approval.model_copy(update={"max_patch_bytes": 1})
        ).reason_code
        == "patch_size_limit_exceeded"
    )
    assert (
        resolve_with(
            selected_harness=harness.model_copy(
                update={
                    "verification": harness.verification.model_copy(
                        update={
                            "review": HarnessReview(parent_semantic_review=True),
                        }
                    )
                }
            )
        ).reason_code
        == "semantic_review_not_clean"
    )

    first_binding = composition.ordered_inputs[0].model_copy(
        update={"paths": (".fleet/policy.yaml",)}
    )
    protected_composition = GraphPatchCompositionRecord.model_validate(
        {
            **composition.model_dump(mode="python"),
            "ordered_inputs": (first_binding, *composition.ordered_inputs[1:]),
            "content_digest": None,
        },
        strict=True,
    )
    protected_evaluation = ParentCandidateEvaluationRecord.model_validate(
        {
            **evaluation.model_dump(mode="python"),
            "composition_record_digest": protected_composition.content_digest,
            "content_digest": None,
        },
        strict=True,
    )
    protected_run = run.model_copy(
        update={
            "composition_digest": protected_composition.content_digest,
            "parent_evaluation_digest": protected_evaluation.content_digest,
        }
    )
    protected_replay = replay.model_copy(update={"record": protected_evaluation})
    assert (
        resolve_with(
            selected_run=protected_run,
            selected_composition=protected_composition,
            selected_evaluation=protected_evaluation,
            selected_replay=protected_replay,
        ).reason_code
        == "protected_or_control_path"
    )

    first_node = acceptance.accepted_revision.graph.nodes[0].model_copy(update={"risk": 3})
    high_risk_graph = acceptance.accepted_revision.graph.model_copy(
        update={"nodes": (first_node, *acceptance.accepted_revision.graph.nodes[1:])}
    )
    high_risk_acceptance = AcceptedGraphRevision(
        revision_number=acceptance.accepted_revision.revision_number,
        graph=high_risk_graph,
    )
    high_risk_evaluation = ParentCandidateEvaluationRecord.model_validate(
        {
            **evaluation.model_dump(mode="python"),
            "accepted_graph_revision_digest": high_risk_acceptance.content_digest,
            "content_digest": None,
        },
        strict=True,
    )
    high_risk_run = run.model_copy(
        update={
            "accepted_graph_revision_digest": high_risk_acceptance.content_digest,
            "parent_evaluation_digest": high_risk_evaluation.content_digest,
        }
    )
    high_risk_replay = replay.model_copy(update={"record": high_risk_evaluation})
    assert (
        resolve_with(
            selected_run=high_risk_run,
            selected_acceptance=high_risk_acceptance,
            selected_evaluation=high_risk_evaluation,
            selected_replay=high_risk_replay,
        ).reason_code
        == "risk_limit_exceeded"
    )

    original_operator = operator.read_text(encoding="utf-8")
    stale_operator = json.loads(original_operator)
    stale_operator["promotion_auto_approval"]["max_changed_files"] = 1
    operator.write_text(json.dumps(stale_operator), encoding="utf-8")
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
    assert json.loads(capsys.readouterr().out)["stable_code"] == "STALE_PROMOTION_APPROVAL"
    assert all((repository / f"{name}.txt").read_text() == f"{name}-before\n" for name in "abc")
    operator.write_text(original_operator, encoding="utf-8")
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
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert all((repository / f"{name}.txt").read_text() == f"{name}-after\n" for name in "abc")


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
        assessments = store.list_records(
            "node_semantic_assessment_v2", NodeSemanticAssessmentRecord, run_id=run_id
        )
        requests = store.list_records("worker_request_v2", WorkerRequest)
        task_reviews = store.list_records(
            "task_review_decision_v2", TaskReviewDecision, run_id=run_id
        )
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
        inspected = inspect_graph_run(store, run_id)

    assert graph_run.status == "ready_to_promote"
    assert acceptance.harness_digest == project_harness_digest(harness)
    assert acceptance.proposed_graph_digest == proposal.content_digest
    assert inspected["plan_review"]["status"] == "accepted"
    assert proposal.planner_strategy.id == "high"
    assert proposal.planner_strategy.model == "high-model"
    assert proposal.planner_strategy.effort == "high"
    assert proposal.planner_routing is not None
    assert graph_run.planner_routing == proposal.planner_routing
    assert proposal.planner_routing.assessment_strategy.id == "planner"
    assert proposal.planner_routing.assessment_digest == canonical_digest(
        proposal.planner_routing.assessment
    )
    assert proposal.planner_routing.candidate_strategy_ids == ("high", "low", "planner")
    assert proposal.planner_routing.eligible_strategy_ids == ("high", "planner")
    assert proposal.planner_routing.selected_strategy.id == "high"
    assert inspected["planner_routing"]["selected_strategy"]["id"] == "high"
    assert {item.node_id for item in assessments} == {"a", "b", "c"}
    assert len({item.content_digest for item in assessments}) == 3
    route_by_node = {item.node_id: item for item in routes}
    assert set(route_by_node) == {"a", "b", "c"}
    assert route_by_node["a"].selected_strategy.model == "low-model"
    assert route_by_node["a"].assessment.semantic_profile is not None
    assert route_by_node["a"].assessment.semantic_profile.task_type is SemanticTaskType.MECHANICAL
    assert route_by_node["a"].planner_hints is not None
    assert route_by_node["a"].planner_hints.complexity == 8
    assert route_by_node["a"].semantic_assessment_digest is not None
    assert route_by_node["a"].selected_strategy.effort == "medium"
    for name in ("b", "c"):
        assert route_by_node[name].selected_strategy.model == "high-model"
        assert route_by_node[name].selected_strategy.effort == "high"
        assert route_by_node[name].assessment.semantic_profile is not None
        assert route_by_node[name].planner_hints is not None
        assert route_by_node[name].planner_hints.complexity == 1
        assert route_by_node[name].routing_facts is not None
        assert route_by_node[name].routing_facts.required_capabilities == (
            "edit_intent",
            "process",
        )
    request_by_node = {item.node_id: item for item in requests if item.graph_run_id == run_id}
    assert set(request_by_node) == {"a", "b", "c"}
    assert len({item.id for item in request_by_node.values()}) == 3
    assert len({item.run_id for item in request_by_node.values()}) == 3
    assert len(task_reviews) == 3
    assert all(item.action.value == "PASS" for item in task_reviews)
    assert len(inspected["task_reviews"]["decisions"]) == 3
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
    assert len(replay["inspection"]["node_semantic_assessments"]) == 3
    assert cli.main(["inspect", run_id, "--db", str(database)]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"

    with SQLiteStore(database) as store:
        assert len(store.list_records("promotion_v2", PromotionRecord, run_id=run_id)) == 1


def test_cli_resumes_paused_graph_with_exact_persisted_operator_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, state = _fixture(tmp_path, task_review=False)
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
        assessments_before = store.list_records(
            "node_semantic_assessment_v2", NodeSemanticAssessmentRecord, run_id=run_id
        )
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
        assessments = store.list_records(
            "node_semantic_assessment_v2", NodeSemanticAssessmentRecord, run_id=run_id
        )
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
    assert resumed.planner_routing == paused.planner_routing
    assert len(proposals) == 1
    assert [item.content_digest for item in assessments] == [
        item.content_digest for item in assessments_before
    ]
    assert len(compositions) == 1
    assert len(evaluations) == 1
    assert resumed.planner_routing == paused.planner_routing
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


def test_cli_simple_goal_selects_cheaper_planner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, _state = _fixture(tmp_path)

    assert (
        cli.main(
            [
                "work",
                "change a.txt",
                "--repo",
                str(repository),
                "--operator-config",
                str(operator),
                "--db",
                str(database),
                "--plan-only",
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    with SQLiteStore(database) as store:
        proposal = store.list_records("proposed_graph_v2", ProposedGraph, run_id=emitted["run_id"])[
            0
        ]

    assert proposal.planner_strategy.id == "low"
    assert proposal.planner_routing is not None
    assert proposal.planner_routing.selected_strategy.id == "low"


def test_cli_explicit_fixed_planner_uses_exact_eligible_strategy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, operator, database, _state = _fixture(tmp_path)

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
                "--plan-only",
                "--planner-strategy",
                "planner",
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    with SQLiteStore(database) as store:
        run = store.get("graph_run_v2", emitted["run_id"], GraphRunRecord)
        proposal = store.list_records("proposed_graph_v2", ProposedGraph, run_id=emitted["run_id"])[
            0
        ]

    assert proposal.planner_strategy.id == "planner"
    assert proposal.planner_routing is not None
    assert proposal.planner_routing.selection_mode is RoutingMode.FIXED
    assert proposal.planner_routing.selected_strategy.id == "planner"
    assert run.planner_routing == proposal.planner_routing


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
    with SQLiteStore(database) as store:
        approved_explanation = explain_any_run(store, run_id)
    assert approved_explanation["current_state"]["promotion_approval_state"] == "approved"
    assert approved_explanation["final_outcome"]["disposition"] == ("accepted_awaiting_promotion")
    assert approved_explanation["final_outcome"]["next_action"] == (
        "explicitly promote the approved exact candidate patch"
    )
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
