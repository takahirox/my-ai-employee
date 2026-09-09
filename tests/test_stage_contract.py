from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_employee.domain.v2 import DecisionOutcome, ExecutionResult, StableFailure, StableFailureCode
from ai_employee.parent_review import CliParentSemanticReviewer, parent_semantic_review_schema_json
from ai_employee.plan_review import CliPlanReviewer, PlanReviewInvocationError
from ai_employee.serialization import canonical_json
from ai_employee.stage_contract import ReferenceContract, StageContractError, schema_argv
from ai_employee.task_planning import CliProposedGraphPlanner
from ai_employee.task_review import CliTaskResultReviewer
from tests import test_parent_review as parent
from tests import test_plan_review_adapters as plan
from tests import test_task_review as task


def test_reference_schema_and_validator_preserve_exact_and_subset_semantics():
    contract = ReferenceContract(criteria=("a", "b"), nodes=("node",), evidence=("1" * 64,))
    schema = json.loads(contract.schema(parent_semantic_review_schema_json()))
    coverage = schema["properties"]["reviewed_criterion_ids"]
    assert coverage["items"]["enum"] == ["a", "b"]
    assert coverage["minItems"] == coverage["maxItems"] == 2
    props = schema["$defs"]["ParentSemanticFinding"]["properties"]
    assert props["criterion_ids"]["minItems"] == 1
    assert props["artifact_digests"]["maxItems"] == 0
    valid = {
        "reviewed_criterion_ids": ["b", "a"],
        "reviewed_node_ids": ["node"],
        "findings": [{"id": "new-finding", "criterion_ids": ["a"]}],
    }
    contract.validate(valid)
    for values in (["a"], ["a", "a"], ["a", "foreign"], ["a", "b", "extra"]):
        with pytest.raises(StageContractError):
            contract.validate({**valid, "reviewed_criterion_ids": values})
    with pytest.raises(StageContractError):
        contract.validate({**valid, "findings": [{"criterion_ids": ["foreign"]}]})
    # Schema generation is pure and one request cannot mutate another's schema.
    other = ReferenceContract(criteria=("other",)).schema(parent_semantic_review_schema_json())
    assert json.loads(other)["properties"]["reviewed_criterion_ids"]["items"]["enum"] == ["other"]
    assert (
        json.loads(parent_semantic_review_schema_json())["properties"]["reviewed_criterion_ids"][
            "items"
        ].get("enum")
        is None
    )


def test_schema_file_is_private_per_overlapping_invocation_and_removed(tmp_path):
    argv = ("codex", "--output-schema", str(tmp_path / "template.json"))
    with schema_argv(argv, b'{"a":1}') as first:
        with schema_argv(argv, b'{"b":2}') as second:
            assert first[-1] != second[-1]
            assert Path(first[-1]).read_bytes() == b'{"a":1}'
            assert Path(second[-1]).read_bytes() == b'{"b":2}'
        assert not Path(second[-1]).exists()
        assert Path(first[-1]).exists()
    assert not Path(first[-1]).exists()


@pytest.mark.parametrize("stage", ["parent", "task", "plan"])
@pytest.mark.parametrize("backend", ["codex_cli", "claude_code_cli", "ollama_cli"])
def test_actual_backend_schema_matches_prompt(tmp_path, stage, backend):
    prompts = []
    paths = []

    class Captured(Exception):
        pass

    class Executor:
        def execute(self, request, decision, cancellation):
            prompt = json.loads(prompts[-1])
            schema = prompt["response_schema"]
            if backend == "codex_cli":
                path = Path(request.argv[request.argv.index("--output-schema") + 1])
                paths.append(path)
                assert json.loads(path.read_bytes()) == schema
            elif backend == "claude_code_cli":
                assert json.loads(request.argv[request.argv.index("--json-schema") + 1]) == schema
            if stage != "plan":
                assert (
                    schema["properties"]["reviewed_criterion_ids"]["items"]["enum"]
                    == prompt["response_contract"]["criterion_ids"]
                )
            raise Captured()

    common = dict(
        executable="unused",
        cwd=".",
        prompt_writer=lambda data: (prompts.append(data), "8" * 64)[1],
        output_schema_path=str(tmp_path / "schema.json"),
    )
    if stage == "parent":
        req = parent._request(parent._strategy(backend))
        reviewer = CliParentSemanticReviewer(
            Executor(),
            lambda _: b"",
            lambda _: b"diff --git a/a.py b/a.py\n+INTEGRATED = False\n",
            parent._allow,
            run_id=parent.RUN,
            strategy=req.reviewer_strategy,
            **common,
        )

        def invoke():
            return reviewer.review(req)
    elif stage == "task":
        req = task._request(task._strategy(backend))
        reviewer = CliTaskResultReviewer(
            Executor(),
            lambda _: b"",
            task._allow,
            run_id=req.run_id,
            strategy=req.reviewer_strategy,
            **common,
        )

        def invoke():
            return reviewer.review(req)
    else:
        goal, proposal = plan._proposal()
        reviewer = CliPlanReviewer(
            Executor(),
            lambda _: b"",
            lambda r: plan._decision(r, DecisionOutcome.ALLOW),
            run_id=proposal.run_id,
            strategy=plan._strategy(backend),
            **common,
        )

        def invoke():
            return reviewer.review(
                goal,
                proposal,
                review_round=0,
                available_capabilities=("process",),
                max_nodes=4,
                max_wall_seconds=30,
            )

    with pytest.raises(Captured):
        invoke()
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize("stage", ["plan", "task", "planner"])
@pytest.mark.parametrize("wrong", ["run_id", "request_digest"])
def test_wrong_process_binding_is_rejected_before_output(stage, wrong):
    class Executor:
        def execute(self, request, decision, cancellation):
            return ExecutionResult(
                id="execution",
                run_id="foreign" if wrong == "run_id" else request.run_id,
                created_at=task.NOW,
                request_digest="7" * 64 if wrong == "request_digest" else request.content_digest,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest="9" * 64,
            )

    def read(_):
        pytest.fail("foreign output must not be read")

    common = dict(executable="unused", cwd=".", prompt_writer=lambda _: "8" * 64)
    if stage == "task":
        req = task._request()
        reviewer = CliTaskResultReviewer(
            Executor(),
            read,
            task._allow,
            run_id=req.run_id,
            strategy=req.reviewer_strategy,
            **common,
        )

        def invoke():
            return reviewer.review(req)
    else:
        goal, proposal = plan._proposal()
        args = (Executor(), read, lambda r: plan._decision(r, DecisionOutcome.ALLOW))
        if stage == "plan":
            reviewer = CliPlanReviewer(
                *args, run_id=proposal.run_id, strategy=plan._strategy(), **common
            )

            def invoke():
                return reviewer.review(
                    goal,
                    proposal,
                    review_round=0,
                    available_capabilities=("process",),
                    max_nodes=4,
                    max_wall_seconds=30,
                )
        else:
            reviewer = CliProposedGraphPlanner(
                *args, run_id=proposal.run_id, strategy=plan._strategy(), **common
            )

            def invoke():
                return reviewer.plan(
                    goal,
                    available_capabilities=("process",),
                    effective_policy_digest=plan.POLICY_DIGEST,
                    harness_digest=plan.HARNESS_DIGEST,
                    max_nodes=4,
                    max_wall_seconds=30,
                )

    with pytest.raises((StageContractError, PlanReviewInvocationError)):
        invoke()


@pytest.mark.parametrize(
    "field", ["graph_run_id", "attempt", "harness_digest", "effective_policy_digest"]
)
def test_task_review_checks_child_binding_before_prompt(field):
    req = task._request()
    child = req.worker_request.model_copy(
        update={field: 1 if field == "attempt" else "7" * 64, "content_digest": None}
    )
    # Keep digest consistency; only the parent-child relationship is wrong.
    result = req.worker_result.model_copy(
        update={"request_digest": child.content_digest, "content_digest": None}
    )
    req = req.model_copy(
        update={
            "worker_request": child,
            "worker_request_digest": child.content_digest,
            "worker_result": result,
            "worker_result_digest": result.content_digest,
            "content_digest": None,
        }
    )
    reviewer = CliTaskResultReviewer(
        task._Executor(),
        lambda _: b"",
        task._allow,
        run_id=req.run_id,
        strategy=req.reviewer_strategy,
        executable="unused",
        cwd=".",
        prompt_writer=lambda _: pytest.fail("unbound child must not reach prompt"),
    )
    with pytest.raises(StageContractError, match="TASK_REVIEW_REQUEST_BINDING_INVALID"):
        reviewer.review(req)


def test_parent_multi_criterion_output_order_and_subset_remain_valid():
    req = parent._request(criterion_ids=("a", "b"))
    payload = {
        "schema_version": "2",
        "findings": [],
        "reviewed_criterion_ids": ["b", "a"],
        "reviewed_node_ids": ["b", "a"],
        "limitations": [],
    }
    reviewer = CliParentSemanticReviewer(
        parent._Executor(),
        lambda _: canonical_json(payload).encode(),
        lambda _: b"diff --git a/a.py b/a.py\n+INTEGRATED = False\n",
        parent._allow,
        run_id=parent.RUN,
        strategy=req.reviewer_strategy,
        executable="unused",
        cwd=".",
        prompt_writer=lambda _: "8" * 64,
    )
    result = reviewer.review(req)
    assert result.reviewed_criterion_ids == ("a", "b")


@pytest.mark.parametrize(
    "wrong", ["run_id", "request_digest", "effective_policy_digest", "outcome"]
)
def test_initial_planner_rejects_foreign_or_denied_policy_without_execution(wrong):
    goal, proposal = plan._proposal()

    def decide(request):
        decision = plan._decision(request, DecisionOutcome.ALLOW)
        return decision.model_copy(
            update={wrong: DecisionOutcome.DENY if wrong == "outcome" else "7" * 64}
        )

    planner = CliProposedGraphPlanner(
        object(),
        lambda _: pytest.fail("no output"),
        decide,
        run_id=proposal.run_id,
        strategy=plan._strategy(),
        executable="unused",
        cwd=".",
        prompt_writer=lambda _: "8" * 64,
    )
    with pytest.raises(StageContractError, match="STAGE_POLICY_BINDING_INVALID"):
        planner.plan(
            goal,
            available_capabilities=("process",),
            effective_policy_digest=plan.POLICY_DIGEST,
            harness_digest=plan.HARNESS_DIGEST,
            max_nodes=4,
            max_wall_seconds=30,
        )


@pytest.mark.parametrize("wrong", ["run_id", "request_digest"])
def test_revision_rejects_foreign_process_before_output(wrong):
    from ai_employee.plan_review import PlanReviewFinding, PlanReviewFindingType, PlanReviewImpact

    goal, proposal = plan._proposal()

    class Executor:
        def execute(self, request, decision, cancellation):
            return ExecutionResult(
                id="revision-execution",
                run_id="foreign" if wrong == "run_id" else request.run_id,
                request_digest="7" * 64 if wrong == "request_digest" else request.content_digest,
                created_at=task.NOW,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest="9" * 64,
            )

    finding = PlanReviewFinding(
        id="finding",
        finding_type=PlanReviewFindingType.MISSING_GOAL_COVERAGE,
        impact=PlanReviewImpact.BLOCKING,
        affected_node_ids=(),
        goal_relation="Goal coverage missing",
        smallest_correction="Cover original requirement",
    )
    planner = CliProposedGraphPlanner(
        Executor(),
        lambda _: pytest.fail("foreign stdout"),
        lambda r: plan._decision(r, DecisionOutcome.ALLOW),
        run_id=proposal.run_id,
        strategy=proposal.planner_strategy,
        executable="unused",
        cwd=".",
        prompt_writer=lambda _: "8" * 64,
    )
    with pytest.raises(StageContractError, match="STAGE_PROCESS_BINDING_INVALID"):
        planner.revise(
            goal,
            proposal,
            (finding,),
            available_capabilities=("process",),
            max_nodes=4,
            max_wall_seconds=30,
        )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_worker_rejects_foreign_failure_before_trusting_failure_code(status):
    from ai_employee.worker_adapters import CodexCliWorkerAdapter

    class Executor:
        def execute(self, request, decision, cancellation):
            return ExecutionResult(
                id="worker-execution",
                run_id="foreign",
                request_digest=request.content_digest,
                created_at=task.NOW,
                status=status,
                duration_seconds=0.01,
                stdout_artifact_digest="9" * 64,
                failure=(
                    StableFailure(code=StableFailureCode.CANCELLED, message="cancelled")
                    if status == "failed"
                    else None
                ),
            )

    req = task._request().worker_request
    adapter = CodexCliWorkerAdapter(
        Executor(), lambda _: pytest.fail("foreign stdout"), task._allow, run_id=req.run_id
    )
    result = adapter.propose(req, None)
    assert result.boundary_diagnostic.code == "WORKER_PROCESS_BINDING_INVALID"


@pytest.mark.parametrize("bad_call", [1, 2, 3])
def test_worker_probe_checks_each_process_before_reading_output(bad_call):
    from ai_employee.worker_adapters import CodexCliWorkerAdapter

    class Executor:
        count = 0

        def execute(self, request, decision, cancellation):
            self.count += 1
            return ExecutionResult(
                id=f"probe-{self.count}",
                run_id="foreign" if self.count == bad_call else request.run_id,
                request_digest=request.content_digest,
                created_at=task.NOW,
                status="succeeded",
                duration_seconds=0.01,
                stdout_artifact_digest="9" * 64,
            )

    adapter = CodexCliWorkerAdapter(
        Executor(),
        lambda _: (
            b"codex-cli 0.153.4 exec" if bad_call == 3 else pytest.fail("unbound probe output")
        ),
        task._allow,
        run_id="probe",
        scratch_directory="/tmp/scratch",
        observation_repository="/tmp/source",
        observation_hosts=(),
    )
    with pytest.raises(StageContractError, match="STAGE_PROCESS_BINDING_INVALID"):
        adapter.probe()
