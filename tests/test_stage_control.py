from datetime import timedelta

import pytest

import ai_employee.task_orchestration as scheduling
from ai_employee.domain.v2 import DecisionOutcome, ExecutionResult, PolicyDecision
from ai_employee.serialization import canonical_json
from ai_employee.stage_control import StageCancellation
from ai_employee.storage import SQLiteStore
from ai_employee.task_orchestration import TaskOrchestrator
from ai_employee.task_review import CliTaskResultReviewer, TaskReviewPayload
from tests import test_closed_loop_orchestration as fixture


@pytest.mark.parametrize("cancel", [False, True])
def test_cli_task_review_polls_owned_control_and_rejects_late_cancelled_pass(
    tmp_path, monkeypatch, cancel
):
    clock = [fixture.NOW]
    monkeypatch.setattr(scheduling, "now", lambda: clock[0])
    goal, graph, node = fixture._inputs()
    node = node.model_copy(
        update={"resource_budget": node.resource_budget.model_copy(update={"wall_seconds": 30.0})}
    )
    graph = graph.model_copy(
        update={
            "nodes": (node,),
            "budget": graph.budget.model_copy(update={"max_wall_seconds": 60.0}),
        }
    )
    output = canonical_json(
        TaskReviewPayload(findings=(), reviewed_criterion_ids=("criterion-fix",), limitations=())
    ).encode()
    polls = []
    with SQLiteStore(tmp_path / "review.db") as store:

        class Executor:
            def execute(self, request, decision, cancellation):
                assert decision.outcome is DecisionOutcome.ALLOW
                for step in range(4):
                    clock[0] += timedelta(seconds=5)
                    if cancel and step == 1:
                        store.request_control("slow-review", "cancel")
                    polls.append(cancellation.cancelled())
                    if polls[-1]:
                        break
                # Even a successful late response must not authorize completion.
                return ExecutionResult(
                    id="review-execution",
                    run_id=request.run_id,
                    created_at=clock[0],
                    request_digest=request.content_digest,
                    status="succeeded",
                    stdout_artifact_digest="9" * 64,
                    duration_seconds=20.0,
                )

        def allow(request):
            return PolicyDecision(
                id="allow-review",
                run_id=request.run_id,
                created_at=clock[0],
                request_digest=request.content_digest,
                effective_policy_digest=fixture.POLICY,
                outcome=DecisionOutcome.ALLOW,
                reason_code="declared_review",
            )

        reviewer = CliTaskResultReviewer(
            Executor(),
            lambda _digest: output,
            allow,
            run_id="slow-review",
            strategy=fixture._strategy().model_copy(
                update={"id": "review-fixture", "backend": "ollama_cli"}
            ),
            executable="ollama",
            cwd=".",
            prompt_writer=lambda _value: "8" * 64,
        )
        orchestrator = TaskOrchestrator(
            store,
            lambda _node, request, _strategy: fixture._result(request, "satisfied"),
            (fixture._strategy(), reviewer.strategy),
            local_backend_allowed=True,
            task_reviewer=reviewer,
            independent_task_review=True,
            clock=lambda: clock[0],
            lease_duration_seconds=15.0,
        )
        run = orchestrator.run(
            goal,
            graph,
            fixture.ExecutionPolicy(max_nodes=1, max_attempts=4, max_wall_seconds=60.0),
            harness_digest=fixture.HARNESS,
            effective_policy_digest=fixture.POLICY,
            run_id="slow-review",
            available_capabilities=("process",),
        )
        assert run.status == ("cancelled" if cancel else "completed")
        assert store.current_run_owner(run.id)["status"] == "closed"
        assert polls == ([False, True] if cancel else [False] * 4)
        replay = orchestrator.replay(run.id)
        assert len(replay.task_review_results) == (0 if cancel else 1)
        assert len(replay.task_review_decisions) == (0 if cancel else 1)
    assert not StageCancellation().cancelled()
