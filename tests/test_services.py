from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from ai_employee.context import ROLE_DEFAULTS, ContextCompiler
from ai_employee.domain import (
    CompletionCriterion,
    ContextRole,
    ExecutionStrategy,
    Finding,
    MergeDecisionState,
    Reference,
    ReviewAssessment,
    RoutingMode,
    Severity,
    VerificationEvidence,
    VerificationRequirement,
)
from ai_employee.evidence import (
    aggregate_coverage,
    assess_completion,
    build_evidence_pack,
    decide_merge,
)
from ai_employee.inspector import _freshness_event, _InspectorServer
from ai_employee.project import discover_project_profile
from ai_employee.routing import record_outcome, select_strategy
from ai_employee.serialization import canonical_digest
from ai_employee.storage import SQLiteStore


def _open_sse(
    server: _InspectorServer,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    host, port = server.server_address
    connection = http.client.HTTPConnection(host, port, timeout=2.0)
    connection.request("GET", "/api/events")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"
    return connection, response


def _next_sse_event(response: http.client.HTTPResponse) -> bytes:
    lines: list[bytes] = []
    while True:
        line = response.readline()
        if not line:
            raise AssertionError("SSE connection closed before a freshness event")
        if line == b"\n":
            event = b"".join(lines)
            lines.clear()
            if event.startswith(b"event: freshness\n"):
                return event
        else:
            lines.append(line)


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        time.sleep(0.01)


class ServiceTests(unittest.TestCase):
    def test_inspector_sse_detects_wal_commits_for_multiple_clients(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory, "fleet.db")
            with SQLiteStore(database):
                pass
            with (
                patch("ai_employee.inspector._SSE_POLL_SECONDS", 0.02),
                patch("ai_employee.inspector._SSE_COALESCE_SECONDS", 0.1),
            ):
                server = _InspectorServer(("127.0.0.1", 0), str(database))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                first_connection, first = _open_sse(server)
                second_connection, second = _open_sse(server)
                try:
                    _wait_until(lambda: server.monitor.client_count == 2)
                    host, port = server.server_address
                    request = http.client.HTTPConnection(host, port, timeout=2.0)
                    request.request("GET", "/api/runs")
                    response = request.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {"runs": []})
                    request.close()

                    request = http.client.HTTPConnection(host, port, timeout=2.0)
                    request.request("POST", "/api/runs")
                    response = request.getresponse()
                    self.assertEqual(response.status, 501)
                    response.read()
                    request.close()

                    with sqlite3.connect(database) as writer:
                        self.assertEqual(
                            writer.execute("PRAGMA journal_mode").fetchone()[0],
                            "wal",
                        )
                        for index in range(3):
                            writer.execute(
                                "INSERT OR REPLACE INTO fleet_meta(key,value) VALUES(?,?)",
                                (f"private-marker-{index}", "must-not-leak"),
                            )
                            writer.commit()

                    first_event = _next_sse_event(first)
                    second_event = _next_sse_event(second)
                    expected = {"type": "freshness"}
                    for event in (first_event, second_event):
                        self.assertLessEqual(len(event), 256)
                        self.assertNotIn(b"private-marker", event)
                        self.assertNotIn(b"must-not-leak", event)
                        self.assertEqual(
                            json.loads(event.split(b"data: ", 1)[1]),
                            expected,
                        )
                    time.sleep(0.2)
                    self.assertEqual(server.monitor.sequence, 1)
                    self.assertLessEqual(len(b": heartbeat\n\n"), 256)
                    self.assertLessEqual(len(_freshness_event()), 256)
                finally:
                    first.close()
                    first_connection.close()
                    second.close()
                    second_connection.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2.0)
                self.assertFalse(thread.is_alive())
                self.assertFalse(server.monitor.thread_alive)

    def test_inspector_sse_bounds_clients_reconnects_and_cleans_up_disconnects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory, "fleet.db")
            with SQLiteStore(database):
                pass
            with (
                patch("ai_employee.inspector._SSE_HEARTBEAT_SECONDS", 0.05),
                patch("ai_employee.inspector._SSE_POLL_SECONDS", 0.02),
                patch("ai_employee.inspector._SSE_COALESCE_SECONDS", 0.05),
                patch("ai_employee.inspector._SSE_MAX_CLIENTS", 1),
            ):
                server = _InspectorServer(("127.0.0.1", 0), str(database))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                first_connection, first = _open_sse(server)
                reconnected_connection = None
                reconnected = None
                try:
                    _wait_until(lambda: server.monitor.client_count == 1)
                    host, port = server.server_address
                    refused = http.client.HTTPConnection(host, port, timeout=2.0)
                    refused.request("GET", "/api/events")
                    refused_response = refused.getresponse()
                    self.assertEqual(refused_response.status, 503)
                    refused_response.read()
                    refused.close()

                    first.close()
                    first_connection.close()
                    _wait_until(lambda: server.monitor.client_count == 0)
                    reconnected_connection, reconnected = _open_sse(server)
                    with sqlite3.connect(database) as writer:
                        writer.execute(
                            "INSERT OR REPLACE INTO fleet_meta(key,value) VALUES(?,?)",
                            ("reconnect-marker", "redacted"),
                        )
                        writer.commit()
                    event = _next_sse_event(reconnected)
                    self.assertEqual(
                        json.loads(event.split(b"data: ", 1)[1]),
                        {"type": "freshness"},
                    )
                finally:
                    if reconnected is not None:
                        reconnected.close()
                    if reconnected_connection is not None:
                        reconnected_connection.close()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2.0)
                _wait_until(lambda: server.monitor.client_count == 0)
                self.assertFalse(server.monitor.thread_alive)

    def test_context_defaults_are_role_scoped_and_pull_on_demand(self) -> None:
        source = {"doc": {"secret": "only by reference"}}
        reference = Reference(
            kind="document", target_id="doc", digest=canonical_digest(source["doc"])
        )
        package = ContextCompiler().compile(
            package_id="context-test",
            run_id="run-test",
            role=ContextRole.WORKER,
            sources=source,
            references=(reference,),
        )
        self.assertEqual(package.authoritative_refs, (reference,))
        self.assertEqual(dict(package.inline_items), {})
        self.assertFalse(ROLE_DEFAULTS[ContextRole.WORKER].include_history)
        self.assertEqual(ContextCompiler.resolve(reference, source), source["doc"])

    def test_inference_is_provisional_and_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "pyproject.toml").write_text("[project]\nname='x'\n")
            profile = discover_project_profile(directory)
            self.assertTrue(profile.rules[0].provisional)
            self.assertFalse(Path(directory, ".fleet").exists())

    def test_evidence_gates_merge_and_completion(self) -> None:
        requirement = VerificationRequirement(
            id="requirement",
            description="must pass",
            accepted_evidence_kinds=("test",),
        )
        evidence = VerificationEvidence(
            id="evidence",
            requirement_ids=(requirement.id,),
            kind="test",
            passed=True,
            summary="passed",
            produced_at=datetime.now(UTC),
            producer="verifier",
        )
        coverage = aggregate_coverage((requirement,), (evidence,))
        self.assertTrue(coverage.complete)
        assessment = assess_completion(
            criteria=(
                CompletionCriterion(
                    id="criterion",
                    description="verified",
                    verification_requirement_ids=(requirement.id,),
                ),
            ),
            coverage=coverage,
            artifacts=(),
            mandatory_gates_passed=True,
        )
        self.assertTrue(assessment.complete)
        pack = build_evidence_pack(
            pack_id="pack",
            run_id="run",
            contract_ids=(),
            requirements=(requirement,),
            evidence=(evidence,),
        )
        decision = decide_merge(
            pack,
            mandatory_approval_required=True,
            mandatory_approval_satisfied=False,
        )
        self.assertEqual(decision.state, MergeDecisionState.HUMAN_REVIEW_REQUIRED)

    def test_blocking_review_requires_changes(self) -> None:
        finding = Finding(
            id="finding",
            code="broken",
            severity=Severity.HIGH,
            summary="broken",
            blocking=True,
        )
        review = ReviewAssessment(
            id="review",
            reviewer="reviewer",
            approved=False,
            blocking_findings=(finding,),
            summary="changes required",
            assessed_at=datetime.now(UTC),
        )
        pack = build_evidence_pack(
            pack_id="pack-review",
            run_id="run",
            contract_ids=(),
            requirements=(),
            evidence=(),
            reviews=(review,),
        )
        decision = decide_merge(
            pack, mandatory_approval_required=False, mandatory_approval_satisfied=False
        )
        self.assertEqual(decision.state, MergeDecisionState.CHANGES_REQUIRED)

    def test_adaptive_routing_uses_fallback_then_explainable_history(self) -> None:
        strategies = tuple(
            ExecutionStrategy(
                id=name,
                routing_mode=RoutingMode.ADAPTIVE,
                backend="local",
                model=name,
            )
            for name in ("a", "b")
        )
        selected = select_strategy(strategies, mode=RoutingMode.ADAPTIVE)
        self.assertEqual(selected.id, "a")
        self.assertIn("insufficient history", selected.routing_reasons[0])
        histories = []
        for strategy, successes in (("a", 1), ("b", 3)):
            value = None
            for index in range(3):
                value = record_outcome(
                    value,
                    strategy_id=strategy,
                    succeeded=index < successes,
                    duration_seconds=1.0,
                    cost=0.0,
                )
            histories.append(value)
        selected = select_strategy(strategies, mode=RoutingMode.ADAPTIVE, performances=histories)
        self.assertEqual(selected.id, "b")
        self.assertIn("success_rate=1.000", selected.routing_reasons)
        with SQLiteStore(":memory:") as store:
            store.save_performance("project", histories[1])
            self.assertEqual(store.performance("project")[0].sample_count, 3)


if __name__ == "__main__":
    unittest.main()
