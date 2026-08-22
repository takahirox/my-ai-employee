from __future__ import annotations

import unittest
from datetime import UTC, datetime

from ai_employee.demo import demo_goal, demo_graph, run_demo
from ai_employee.domain import (
    Artifact,
    ExecutionPolicy,
    Goal,
    Reference,
    ResultEnvelope,
    ResultStatus,
    Run,
    RunState,
    VerificationEvidence,
)
from ai_employee.graph import accept_graph
from ai_employee.inspector import inspect_run
from ai_employee.runtime import DeterministicRuntime, NodeProposal
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore


class RuntimeTests(unittest.TestCase):
    def test_end_to_end_demo_repairs_persists_completes_and_replays(self) -> None:
        with SQLiteStore(":memory:") as store:
            outcome = run_demo(store, run_id="demo-test")
            self.assertEqual(outcome.run.state, RunState.SUCCEEDED)
            self.assertEqual(
                [result.status for _, result in outcome.results].count(ResultStatus.FAILED), 1
            )
            self.assertTrue(outcome.coverage.complete)
            replay = DeterministicRuntime({}, store=store).replay("demo-test")
            self.assertEqual(replay.invoked_handlers, 0)
            self.assertEqual(replay.result_count, 6)
            projection = inspect_run(store, "demo-test")
            self.assertEqual(projection["graph"]["revision"], 1)
            self.assertTrue(projection["graph"]["stable"])

    def test_completion_is_refused_without_mandatory_evidence(self) -> None:
        goal, requirement = demo_goal()
        goal = goal.model_copy(update={"completion_criteria": goal.completion_criteria})
        graph = demo_graph()
        policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)
        run = Run(
            id="blocked-run", goal=goal, accepted_graph=accept_graph(graph, policy), policy=policy
        )

        def always(context):
            return ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=ResultStatus.SUCCEEDED,
                value={"ok": True},
            )

        outcome = DeterministicRuntime({node.id: always for node in graph.nodes}).execute(
            run, requirements=(requirement,)
        )
        self.assertEqual(outcome.run.state, RunState.BLOCKED)
        self.assertIn("missing evidence", outcome.run.failure.message)

    def test_pause_checkpoint_and_resume(self) -> None:
        graph = demo_graph().model_copy(
            update={
                "edges": demo_graph().edges[:1],
                "nodes": demo_graph().nodes[:2],
                "terminal_node_ids": ("gate",),
                "budget": demo_graph().budget.model_copy(update={"max_attempts": 4}),
            }
        )
        policy = ExecutionPolicy(max_nodes=10, max_attempts=4, max_wall_seconds=60.0)
        run = Run(
            id="pause-run",
            goal=Goal(id="pause-goal", statement="pause and resume"),
            accepted_graph=accept_graph(graph, policy),
            policy=policy,
        )

        def always(context):
            return ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=ResultStatus.SUCCEEDED,
                value={"ok": True},
            )

        with SQLiteStore(":memory:") as store:
            runtime = DeterministicRuntime({node.id: always for node in graph.nodes}, store=store)
            paused = runtime.execute(run, pause_after_nodes=1)
            self.assertTrue(paused.paused)
            self.assertEqual(paused.run.state, RunState.PAUSED)
            resumed = runtime.execute(paused.run, resume=True)
            self.assertEqual(resumed.run.state, RunState.SUCCEEDED)

    def test_resume_preserves_persisted_artifacts_and_evidence(self) -> None:
        graph = demo_graph()
        goal, requirement = demo_goal()
        policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)
        run = Run(
            id="resume-facts",
            goal=goal,
            accepted_graph=accept_graph(graph, policy),
            policy=policy,
        )

        def handler(context):
            envelope = ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=ResultStatus.SUCCEEDED,
                value={"ok": True},
            )
            if context.node.id == "prepare":
                artifact = Artifact(
                    id="artifact-demo",
                    run_id=run.id,
                    media_type="application/json",
                    digest=canonical_digest({"demo": True}),
                    size_bytes=13,
                    locator="memory:artifact-demo",
                    created_at=datetime.now(UTC),
                    producer_node_id=context.node.id,
                )
                return NodeProposal(
                    envelope.model_copy(
                        update={
                            "artifact_refs": (
                                Reference(
                                    kind="artifact",
                                    target_id=artifact.id,
                                    digest=artifact.digest,
                                ),
                            ),
                        }
                    ),
                    artifacts=(artifact,),
                )
            if context.node.id == "verify":
                evidence = VerificationEvidence(
                    id="evidence-demo",
                    requirement_ids=(requirement.id,),
                    kind="offline-check",
                    passed=True,
                    summary="persisted evidence",
                    produced_at=datetime.now(UTC),
                    producer=context.node.id,
                )
                return NodeProposal(
                    envelope.model_copy(
                        update={
                            "evidence_refs": (Reference(kind="evidence", target_id=evidence.id),),
                        }
                    ),
                    evidence=(evidence,),
                )
            return envelope

        with SQLiteStore(":memory:") as store:
            runtime = DeterministicRuntime({node.id: handler for node in graph.nodes}, store=store)
            paused = runtime.execute(
                run,
                requirements=(requirement,),
                pause_after_nodes=3,
            )
            self.assertTrue(paused.paused)
            resumed = runtime.execute(
                paused.run,
                requirements=(requirement,),
                resume=True,
            )
            self.assertEqual(resumed.run.state, RunState.SUCCEEDED)
            self.assertEqual([item.id for item in resumed.artifacts], ["artifact-demo"])
            self.assertEqual([item.id for item in resumed.evidence], ["evidence-demo"])

    def test_failed_terminal_node_refuses_completion(self) -> None:
        source = demo_graph()
        graph = source.model_copy(
            update={
                "nodes": source.nodes[:1],
                "edges": (),
                "entry_node_ids": ("prepare",),
                "terminal_node_ids": ("prepare",),
            }
        )
        policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)
        run = Run(
            id="failed-terminal",
            goal=Goal(id="terminal-goal", statement="must fail"),
            accepted_graph=accept_graph(graph, policy),
            policy=policy,
        )

        def fail(context):
            return ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=ResultStatus.FAILED,
                value={"ok": False},
            )

        outcome = DeterministicRuntime({"prepare": fail}).execute(run)
        self.assertEqual(outcome.run.state, RunState.BLOCKED)
        self.assertIn("terminal nodes have not succeeded", outcome.run.failure.message)

    def test_node_iteration_budget_is_enforced(self) -> None:
        source = demo_graph()
        graph = source.model_copy(
            update={
                "nodes": tuple(
                    node.model_copy(update={"max_iterations": 1}) if node.id == "gate" else node
                    for node in source.nodes
                ),
            }
        )
        policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)
        run = Run(
            id="iteration-budget",
            goal=Goal(id="iteration-goal", statement="stay bounded"),
            accepted_graph=accept_graph(graph, policy),
            policy=policy,
        )

        def handler(context):
            return ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=(
                    ResultStatus.FAILED
                    if context.node.id == "gate" and context.attempt == 0
                    else ResultStatus.SUCCEEDED
                ),
                value={"ok": context.node.id != "gate" or context.attempt > 0},
            )

        outcome = DeterministicRuntime({node.id: handler for node in graph.nodes}).execute(run)
        self.assertEqual(outcome.run.state, RunState.EXHAUSTED)
        self.assertEqual(outcome.run.failure.code, "node_iteration_budget_exhausted")


if __name__ == "__main__":
    unittest.main()
