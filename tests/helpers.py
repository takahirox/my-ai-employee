from __future__ import annotations

from datetime import datetime, timezone

from ai_employee.domain import (
    AcceptedGraphRevision,
    ContractKind,
    ExecutionPolicy,
    Graph,
    Goal,
    Node,
    NodeKind,
    OutputContract,
    Run,
    Task,
    TaskProfile,
    TransitionProvenance,
)
from ai_employee.domain.enums import ContextRole

DIGEST = "0" * 64


def output_contract() -> OutputContract:
    return OutputContract(
        id="contract.result",
        expected_type=ContractKind.OBJECT,
        required_fields=("answer",),
    )


def node() -> Node:
    return Node(
        id="node.one",
        kind=NodeKind.FUNCTION,
        name="Deterministic function",
        output_contract=output_contract(),
        configuration={"ordered": [1, 2, {"safe": True}]},
    )


def graph() -> Graph:
    return Graph(
        id="graph.demo",
        nodes=(node(),),
        entry_node_ids=("node.one",),
        terminal_node_ids=("node.one",),
    )


def accepted_graph() -> AcceptedGraphRevision:
    return AcceptedGraphRevision(revision_number=1, graph=graph())


def run() -> Run:
    return Run(
        id="run.demo",
        goal=Goal(id="goal.demo", statement="Produce a deterministic result"),
        accepted_graph=accepted_graph(),
        policy=ExecutionPolicy(),
    )


def task() -> Task:
    return Task(
        id="task.demo",
        title="Perform bounded work",
        profile=TaskProfile(id="profile.worker", role=ContextRole.WORKER),
        output_contract=output_contract(),
    )


def provenance() -> TransitionProvenance:
    return TransitionProvenance(
        cause="unit test",
        rule_version="transition.v1",
        actor="runtime",
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        graph_digest=DIGEST,
        policy_digest=DIGEST,
        input_digest=DIGEST,
        evidence_digest=DIGEST,
    )
