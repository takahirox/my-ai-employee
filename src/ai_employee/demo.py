"""Deterministic offline end-to-end Trust Kernel demonstration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .context import ContextCompiler
from .domain import (
    Artifact,
    Budget,
    CompletionCriterion,
    ContextRole,
    Edge,
    ExecutionPolicy,
    Failure,
    FailureKind,
    Goal,
    Graph,
    Node,
    NodeKind,
    OutputContract,
    Reference,
    ResultEnvelope,
    ResultStatus,
    Run,
    VerificationEvidence,
    VerificationRequirement,
)
from .domain.base import freeze_json
from .graph import accept_graph
from .runtime import (
    DeterministicRuntime,
    NodeExecutionContext,
    NodeHandler,
    NodeProposal,
    RuntimeOutcome,
)
from .serialization import canonical_digest
from .storage import SQLiteStore


def demo_graph() -> Graph:
    contracts = {
        name: OutputContract(
            id=f"contract-{name}",
            required_fields=("ok",),
            allow_additional_fields=False,
        )
        for name in ("prepare", "gate", "repair", "verify", "complete")
    }
    nodes = tuple(
        Node(
            id=name,
            kind=kind,
            name=name.replace("-", " ").title(),
            output_contract=contracts[name],
            max_iterations=2 if name == "gate" else 1,
        )
        for name, kind in (
            ("prepare", NodeKind.FUNCTION),
            ("gate", NodeKind.GATE),
            ("repair", NodeKind.FUNCTION),
            ("verify", NodeKind.PREDICATE),
            ("complete", NodeKind.SYSTEM),
        )
    )
    return Graph(
        id="demo-graph",
        nodes=nodes,
        edges=(
            Edge(id="prepare-gate", source_id="prepare", target_id="gate"),
            Edge(id="gate-repair", source_id="gate", target_id="repair", condition="failed"),
            Edge(
                id="repair-gate", source_id="repair", target_id="gate", loop=True, max_traversals=1
            ),
            Edge(id="gate-verify", source_id="gate", target_id="verify", condition="succeeded"),
            Edge(id="verify-complete", source_id="verify", target_id="complete"),
        ),
        entry_node_ids=("prepare",),
        terminal_node_ids=("complete",),
        budget=Budget(
            max_attempts=8,
            max_retries=1,
            max_loop_iterations=2,
            max_nodes=10,
            max_wall_seconds=60.0,
        ),
    )


def demo_goal() -> tuple[Goal, VerificationRequirement]:
    requirement = VerificationRequirement(
        id="requirement-demo",
        description="offline verification must pass",
        mandatory=True,
        accepted_evidence_kinds=("offline-check",),
    )
    criterion = CompletionCriterion(
        id="criterion-demo",
        description="artifact and evidence exist",
        mandatory=True,
        verification_requirement_ids=(requirement.id,),
        required_artifact_ids=("artifact-demo",),
    )
    return Goal(
        id="goal-demo",
        statement="Demonstrate deterministic repair and evidence-gated completion",
        completion_criteria=(criterion,),
        budget=Budget(max_attempts=8, max_retries=1),
    ), requirement


def _handlers(run_id: str) -> Mapping[str | NodeKind, NodeHandler]:
    def success(context: NodeExecutionContext) -> ResultEnvelope:
        return ResultEnvelope(
            contract_id=context.node.output_contract.id,
            status=ResultStatus.SUCCEEDED,
            value=freeze_json({"ok": True}),
        )

    def prepare(context: NodeExecutionContext) -> NodeProposal:
        artifact = Artifact(
            id="artifact-demo",
            run_id=run_id,
            media_type="application/json",
            digest=canonical_digest({"demo": True}),
            size_bytes=13,
            locator="memory:artifact-demo",
            created_at=datetime.now(UTC),
            producer_node_id=context.node.id,
        )
        envelope = ResultEnvelope(
            contract_id=context.node.output_contract.id,
            status=ResultStatus.SUCCEEDED,
            value=freeze_json({"ok": True}),
            artifact_refs=(
                Reference(kind="artifact", target_id=artifact.id, digest=artifact.digest),
            ),
        )
        return NodeProposal(envelope, artifacts=(artifact,))

    def gate(context: NodeExecutionContext) -> ResultEnvelope:
        if context.attempt == 0:
            failure = Failure(
                id="failure-demo-gate",
                kind=FailureKind.VERIFICATION,
                code="demo_gate_failed",
                message="expected first gate failure",
                retryable=True,
            )
            return ResultEnvelope(
                contract_id=context.node.output_contract.id,
                status=ResultStatus.FAILED,
                value=freeze_json({"ok": False}),
                failures=(failure,),
            )
        return success(context)

    def verify(context: NodeExecutionContext) -> NodeProposal:
        evidence = VerificationEvidence(
            id="evidence-demo",
            requirement_ids=("requirement-demo",),
            kind="offline-check",
            passed=True,
            summary="deterministic offline assertion passed",
            produced_at=datetime.now(UTC),
            producer=context.node.id,
        )
        envelope = ResultEnvelope(
            contract_id=context.node.output_contract.id,
            status=ResultStatus.SUCCEEDED,
            value=freeze_json({"ok": True}),
            evidence_refs=(Reference(kind="evidence", target_id=evidence.id),),
        )
        return NodeProposal(envelope, evidence=(evidence,))

    return {
        "prepare": prepare,
        "gate": gate,
        "repair": success,
        "verify": verify,
        "complete": success,
    }


def run_demo(store: SQLiteStore, *, run_id: str = "demo-run") -> RuntimeOutcome:
    goal, requirement = demo_goal()
    policy = ExecutionPolicy(max_nodes=10, max_attempts=8, max_wall_seconds=60.0)
    accepted = accept_graph(demo_graph(), policy)
    run = Run(id=run_id, goal=goal, accepted_graph=accepted, policy=policy)
    store.save_graph(run_id, accepted)
    compiler = ContextCompiler()
    source = {"goal-demo": goal, "demo-graph": accepted}
    refs = (
        Reference(kind="document", target_id="goal-demo"),
        Reference(kind="document", target_id="demo-graph", digest=canonical_digest(accepted)),
    )
    for role in ContextRole:
        package = compiler.compile(
            package_id=f"context-{role.value}",
            run_id=run_id,
            role=role,
            sources=source,
            references=refs,
        )
        store.put("context", package, run_id=run_id)
    runtime = DeterministicRuntime(_handlers(run_id), store=store)
    return runtime.execute(run, requirements=(requirement,))


def run_demo_path(path: str | Path, *, run_id: str = "demo-run") -> RuntimeOutcome:
    with SQLiteStore(path) as store:
        return run_demo(store, run_id=run_id)
