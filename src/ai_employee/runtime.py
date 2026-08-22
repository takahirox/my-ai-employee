"""Bounded single-process deterministic graph scheduler and replay support."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from .domain import (
    Artifact, EvidenceCoverage, Event, Failure, FailureKind, Node, NodeKind, NodeState,
    ResultEnvelope, ResultStatus, Run, RunState,
    TransitionProvenance, VerificationEvidence, VerificationRequirement,
    transition_node, transition_run,
)
from .evidence import aggregate_coverage, assess_completion
from .serialization import canonical_digest, canonical_json
from .storage import SQLiteStore


@dataclass(frozen=True)
class NodeExecutionContext:
    run: Run
    node: Node
    attempt: int
    previous_results: tuple[tuple[str, ResultEnvelope], ...]


@dataclass(frozen=True)
class NodeProposal:
    """Untrusted worker output submitted to the deterministic runtime."""

    envelope: ResultEnvelope
    artifacts: tuple[Artifact, ...] = ()
    evidence: tuple[VerificationEvidence, ...] = ()


NodeHandler = Callable[[NodeExecutionContext], ResultEnvelope | NodeProposal]


@dataclass(frozen=True)
class RuntimeOutcome:
    run: Run
    results: tuple[tuple[str, ResultEnvelope], ...]
    artifacts: tuple[Artifact, ...]
    evidence: tuple[VerificationEvidence, ...]
    coverage: EvidenceCoverage
    paused: bool = False


@dataclass(frozen=True)
class ReplayReport:
    run_id: str
    event_count: int
    result_count: int
    control_digest: str
    invoked_handlers: int = 0


class DeterministicRuntime:
    """The only component authorized to advance graph and run state."""

    def __init__(
        self,
        handlers: Mapping[str | NodeKind, NodeHandler],
        *,
        store: SQLiteStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.handlers = dict(handlers)
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        run: Run,
        *,
        requirements: tuple[VerificationRequirement, ...] = (),
        pause_after_nodes: int | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        resume: bool = False,
    ) -> RuntimeOutcome:
        """Execute accepted nodes with bounded retries, loops, pause, and cancellation."""

        graph = run.accepted_graph.graph
        graph_revision = run.accepted_graph.revision_number
        if resume:
            if self.store is None:
                raise ValueError("resume requires a persistence store")
            generation, checkpoint = self.store.load_checkpoint(run.id)
            if generation != run.generation:
                raise ValueError("checkpoint rejected by generation fence")
            queue = deque(str(item) for item in checkpoint["queue"])
            traversal_counts = defaultdict(int, {
                str(key): int(value) for key, value in checkpoint["traversals"].items()
            })
            attempts = defaultdict(int, {
                str(key): int(value) for key, value in checkpoint["attempts"].items()
            })
            results = [
                (
                    str(item["node_id"]),
                    ResultEnvelope.model_validate_json(
                        json.dumps(item["envelope"], ensure_ascii=False), strict=True
                    ),
                )
                for item in checkpoint["results"]
            ]
            artifacts = list(self.store.list_records("artifact", Artifact, run_id=run.id))
            evidence = list(
                self.store.list_records("evidence", VerificationEvidence, run_id=run.id)
            )
            elapsed_before = float(checkpoint.get("elapsed_wall_seconds", 0.0))
            self.store.clear_control(run.id)
            # A resumed execution is a new generation. Any result produced by
            # pre-pause work after this point is fenced out by transition APIs.
            run = run.model_copy(update={"generation": run.generation + 1})
        else:
            queue = deque(sorted(graph.entry_node_ids))
            traversal_counts: defaultdict[str, int] = defaultdict(int)
            attempts: defaultdict[str, int] = defaultdict(int)
            results: list[tuple[str, ResultEnvelope]] = []
            artifacts = []
            evidence = []
            elapsed_before = 0.0

        if run.state is RunState.CREATED:
            run = self._transition_run(run, RunState.READY, "accepted graph ready")
        if run.state in {RunState.READY, RunState.PAUSED}:
            run = self._transition_run(run, RunState.RUNNING, "scheduler started")
        if run.state is not RunState.RUNNING:
            raise ValueError(f"run must be created, ready, or paused, got {run.state.value}")
        self._persist_run(run)

        nodes = {node.id: node for node in graph.nodes}
        edges = defaultdict(list)
        for edge in sorted(graph.edges, key=lambda item: item.id):
            edges[edge.source_id].append(edge)
        started = monotonic()
        executed = 0
        while queue:
            elapsed = elapsed_before + monotonic() - started
            requested_control = self.store.control(run.id) if self.store is not None else None
            if requested_control == "pause":
                run = self._transition_run(run, RunState.PAUSED, "pause requested")
                self._checkpoint(
                    run, queue, traversal_counts, attempts, results,
                    elapsed_wall_seconds=elapsed,
                )
                self._persist_run(run)
                outcome = self._outcome(run, results, artifacts, evidence, requirements)
                return RuntimeOutcome(**{**outcome.__dict__, "paused": True})
            if requested_control == "cancel" or (cancel_requested is not None and cancel_requested()):
                run = self._transition_run(run, RunState.CANCELLING, "cancellation requested")
                run = self._transition_run(run, RunState.CANCELLED, "cancellation acknowledged")
                self._persist_run(run)
                return self._outcome(run, results, artifacts, evidence, requirements)
            if elapsed > min(graph.budget.max_wall_seconds, run.policy.max_wall_seconds):
                return self._exhaust(run, "time_budget_exhausted", results, artifacts, evidence, requirements)
            if sum(attempts.values()) >= min(graph.budget.max_attempts, run.policy.max_attempts):
                return self._exhaust(run, "attempt_budget_exhausted", results, artifacts, evidence, requirements)
            if pause_after_nodes is not None and executed >= pause_after_nodes:
                run = self._transition_run(run, RunState.PAUSED, "pause boundary reached")
                self._checkpoint(
                    run, queue, traversal_counts, attempts, results,
                    elapsed_wall_seconds=elapsed,
                )
                self._persist_run(run)
                outcome = self._outcome(run, results, artifacts, evidence, requirements)
                return RuntimeOutcome(**{**outcome.__dict__, "paused": True})

            node_id = queue.popleft()
            base_node = nodes[node_id]
            if attempts[node_id] >= base_node.max_iterations:
                return self._exhaust(
                    run, "node_iteration_budget_exhausted",
                    results, artifacts, evidence, requirements,
                )
            attempt = attempts[node_id]
            attempts[node_id] += 1
            active = base_node.model_copy(update={
                "state": NodeState.PENDING, "attempt": attempt,
                "generation": run.generation, "graph_revision": graph_revision,
                "transitions": (), "failure": None,
            })
            active = self._transition_node(active, NodeState.READY, run, "dependencies satisfied")
            active = self._transition_node(active, NodeState.RUNNING, run, "handler dispatched")
            proposal = self._invoke(run, active, attempt, tuple(results))
            envelope, accepted_artifacts, accepted_evidence = self._validate_proposal(
                run, active, proposal,
            )

            if (
                envelope.status is ResultStatus.FAILED
                and attempt < min(active.retry_limit, graph.budget.max_retries)
                and any(failure.retryable for failure in envelope.failures)
            ):
                failure = envelope.failures[0] if envelope.failures else self._failure(
                    "retryable_result", "node returned a failed result", retryable=True
                )
                active = self._transition_node(active, NodeState.FAILED, run, "attempt failed", failure)
                self._persist_node(run.id, active)
                results.append((node_id, envelope))
                self._event(run, "node.result", {"node_id": node_id, "attempt": attempt, "envelope": envelope})
                queue.appendleft(node_id)
                continue

            target_state = NodeState.SUCCEEDED
            transition_failure = None
            if envelope.status is ResultStatus.BLOCKED:
                target_state = NodeState.BLOCKED
                transition_failure = envelope.failures[0] if envelope.failures else self._failure(
                    "node_blocked", "node returned blocked"
                )
            elif envelope.status is ResultStatus.CANCELLED:
                target_state = NodeState.CANCELLED
            elif envelope.status is ResultStatus.FAILED:
                target_state = NodeState.FAILED
                transition_failure = envelope.failures[0] if envelope.failures else self._failure(
                    "node_failed", "node returned failed"
                )
            active = self._transition_node(active, target_state, run, "structured result accepted", transition_failure)
            self._persist_node(run.id, active)
            results.append((node_id, envelope))
            artifacts.extend(accepted_artifacts)
            evidence.extend(accepted_evidence)
            self._event(run, "node.result", {"node_id": node_id, "attempt": attempt, "envelope": envelope})
            executed += 1

            selected = [edge for edge in edges[node_id] if _condition_matches(edge.condition, envelope.status)]
            for edge in selected:
                traversal_counts[edge.id] += 1
                bound = edge.max_traversals if edge.loop else 1
                if traversal_counts[edge.id] > (bound or 1):
                    return self._exhaust(run, "loop_budget_exhausted", results, artifacts, evidence, requirements)
                queue.append(edge.target_id)
                self._event(run, "edge.traversed", {"edge_id": edge.id, "target_id": edge.target_id})

        coverage = aggregate_coverage(requirements, evidence)
        gate_ids = {node.id for node in graph.nodes if node.kind is NodeKind.GATE}
        latest_results = {node_id: result for node_id, result in results}
        successful_gates = {
            node_id for node_id in gate_ids
            if node_id in latest_results and latest_results[node_id].status is ResultStatus.SUCCEEDED
        }
        terminal_nodes_succeeded = all(
            node_id in latest_results
            and latest_results[node_id].status is ResultStatus.SUCCEEDED
            for node_id in graph.terminal_node_ids
        )
        findings = tuple(finding for _, result in results for finding in result.findings)
        completion = assess_completion(
            criteria=run.goal.completion_criteria, coverage=coverage, artifacts=artifacts,
            mandatory_gates_passed=gate_ids <= successful_gates,
            terminal_nodes_succeeded=terminal_nodes_succeeded, findings=findings,
        )
        if completion.complete:
            run = self._transition_run(run, RunState.SUCCEEDED, "completion facts satisfied")
        else:
            failure = Failure(
                id="completion_failure", kind=FailureKind.VERIFICATION, code="completion_incomplete",
                message="; ".join(completion.reasons), retryable=True,
                details={"reasons": completion.reasons},
            )
            run = self._transition_run(run, RunState.BLOCKED, "completion refused", failure)
        self._persist_run(run)
        return RuntimeOutcome(run, tuple(results), tuple(artifacts), tuple(evidence), coverage)

    def replay(self, run_id: str) -> ReplayReport:
        """Replay stored control decisions from envelopes without invoking handlers."""

        if self.store is None:
            raise ValueError("replay requires a persistence store")
        events = self.store.events(run_id)
        controls = [
            {"type": event.event_type, "payload": event.payload}
            for event in events if event.event_type in {"node.result", "edge.traversed", "run.transition"}
        ]
        return ReplayReport(
            run_id=run_id, event_count=len(events),
            result_count=sum(event.event_type == "node.result" for event in events),
            control_digest=canonical_digest(controls), invoked_handlers=0,
        )

    def _invoke(
        self, run: Run, node: Node, attempt: int,
        results: tuple[tuple[str, ResultEnvelope], ...],
    ) -> ResultEnvelope | NodeProposal:
        handler = self.handlers.get(node.id) or self.handlers.get(node.kind)
        if handler is None:
            return ResultEnvelope(
                contract_id=node.output_contract.id, status=ResultStatus.FAILED,
                failures=(self._failure("missing_handler", f"no handler for node {node.id}"),),
            )
        try:
            return handler(NodeExecutionContext(run, node, attempt, results))
        except Exception as exc:  # worker exceptions are converted at the trust boundary
            return ResultEnvelope(
                contract_id=node.output_contract.id, status=ResultStatus.FAILED,
                failures=(self._failure("handler_exception", f"{type(exc).__name__}: {exc}"),),
            )

    def _validate_proposal(
        self, run: Run, node: Node, proposal: ResultEnvelope | NodeProposal,
    ) -> tuple[ResultEnvelope, tuple[Artifact, ...], tuple[VerificationEvidence, ...]]:
        wrapped = proposal if isinstance(proposal, NodeProposal) else NodeProposal(proposal)
        try:
            wrapped.envelope.validate_contract(node.output_contract)
        except ValueError as exc:
            envelope = ResultEnvelope(
                contract_id=node.output_contract.id, status=ResultStatus.FAILED,
                failures=(Failure(
                    id="invalid_output", kind=FailureKind.INVALID_OUTPUT, code="contract_violation",
                    message=str(exc), retryable=True,
                ),),
            )
            return envelope, (), ()
        referenced_artifacts = {item.target_id for item in wrapped.envelope.artifact_refs}
        accepted_artifacts = tuple(
            item for item in wrapped.artifacts
            if item.id in referenced_artifacts and item.run_id == run.id
            and item.producer_node_id in {None, node.id}
        )
        referenced_evidence = {item.target_id for item in wrapped.envelope.evidence_refs}
        accepted_evidence = tuple(
            item for item in wrapped.evidence
            if item.id in referenced_evidence and item.producer == node.id
        )
        for artifact in accepted_artifacts:
            if self.store is not None:
                self.store.save_artifact(artifact)
        for item in accepted_evidence:
            if self.store is not None:
                self.store.save_evidence(run.id, item)
        return wrapped.envelope, accepted_artifacts, accepted_evidence

    def _transition_run(
        self, run: Run, target: RunState, cause: str, failure: Failure | None = None,
    ) -> Run:
        updated = transition_run(
            run, target, self._provenance(run, cause), expected_generation=run.generation,
            expected_graph_revision=run.accepted_graph.revision_number, failure=failure,
        )
        self._event(updated, "run.transition", {"transition": updated.transitions[-1]})
        return updated

    def _transition_node(
        self, node: Node, target: NodeState, run: Run, cause: str,
        failure: Failure | None = None,
    ) -> Node:
        return transition_node(
            node, target, self._provenance(run, cause), expected_generation=run.generation,
            expected_graph_revision=run.accepted_graph.revision_number, failure=failure,
        )

    def _provenance(self, run: Run, cause: str) -> TransitionProvenance:
        return TransitionProvenance(
            cause=cause, rule_version="runtime-v1", actor="runtime", timestamp=self.clock(),
            graph_digest=run.accepted_graph.content_digest,
            policy_digest=canonical_digest(run.policy), input_digest=canonical_digest(run.goal),
            evidence_digest=canonical_digest([]),
        )

    def _event(self, run: Run, event_type: str, payload: object) -> None:
        if self.store is None:
            return
        sequence = len(self.store.events(run.id)) + 1
        self.store.append_event(Event(
            id=f"{run.id}-event-{sequence}", run_id=run.id, event_type=event_type,
            timestamp=self.clock(), actor="runtime", payload=json.loads(canonical_json(payload)),
        ))

    def _checkpoint(
        self, run: Run, queue: deque[str], traversals: Mapping[str, int],
        attempts: Mapping[str, int], results: list[tuple[str, ResultEnvelope]],
        *, elapsed_wall_seconds: float,
    ) -> None:
        if self.store is None:
            return
        self.store.checkpoint(run.id, run.generation, {
            "queue": list(queue), "traversals": dict(traversals), "attempts": dict(attempts),
            "results": [{"node_id": node_id, "envelope": envelope} for node_id, envelope in results],
            "elapsed_wall_seconds": elapsed_wall_seconds,
        })

    def _persist_run(self, run: Run) -> None:
        if self.store is not None:
            self.store.save_run(run)

    def _persist_node(self, run_id: str, node: Node) -> None:
        if self.store is not None:
            self.store.save_node(run_id, node)

    def _exhaust(self, run: Run, code: str, results: list[tuple[str, ResultEnvelope]], artifacts: list[Artifact], evidence: list[VerificationEvidence], requirements: tuple[VerificationRequirement, ...]) -> RuntimeOutcome:
        failure = Failure(
            id="runtime_exhausted", kind=FailureKind.RESOURCE_EXHAUSTION, code=code,
            message=code.replace("_", " "), retryable=False,
        )
        run = self._transition_run(run, RunState.EXHAUSTED, code, failure)
        self._persist_run(run)
        return self._outcome(run, results, artifacts, evidence, requirements)

    @staticmethod
    def _failure(code: str, message: str, *, retryable: bool = False) -> Failure:
        return Failure(
            id=f"failure-{code}", kind=FailureKind.EXECUTION, code=code,
            message=message, retryable=retryable,
        )

    @staticmethod
    def _outcome(run: Run, results: list[tuple[str, ResultEnvelope]], artifacts: list[Artifact], evidence: list[VerificationEvidence], requirements: tuple[VerificationRequirement, ...]) -> RuntimeOutcome:
        return RuntimeOutcome(
            run, tuple(results), tuple(artifacts), tuple(evidence), aggregate_coverage(requirements, evidence)
        )


def _condition_matches(condition: str | None, status: ResultStatus) -> bool:
    if condition is None:
        return status is ResultStatus.SUCCEEDED
    normalized = condition.strip().lower()
    return normalized in {"always", status.value}
