"""Read-only Inspector projection and tiny local HTTP server."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .domain import Artifact, ContextPackage, ExecutionMetrics, Node, Run, VerificationEvidence
from .serialization import canonical_json
from .storage import SQLiteStore


def inspect_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Project all v0.1 inspection facts without exposing mutation methods."""

    run = store.get("run", run_id, Run)
    nodes = store.list_records("node", Node, run_id=run_id)
    latest_nodes: dict[str, Node] = {}
    for node in nodes:
        latest_nodes[node.id] = node
    artifacts = store.list_records("artifact", Artifact, run_id=run_id)
    evidence = store.list_records("evidence", VerificationEvidence, run_id=run_id)
    metrics = store.list_records("metrics", ExecutionMetrics, run_id=run_id)
    contexts = store.list_records("context", ContextPackage, run_id=run_id)
    requirements = run.goal.completion_criteria
    requirement_ids = {
        requirement_id
        for criterion in requirements
        for requirement_id in criterion.verification_requirement_ids
    }
    # The full typed requirements may live in an EvidencePack; this projection
    # still exposes references and persisted evidence when absent.
    events = store.events(run_id)
    results = [event.payload for event in events if event.event_type == "node.result"]
    routing = [
        item.custom for item in metrics if item.custom is not None
    ]
    return {
        "run_id": run.id,
        "state": run.state.value,
        "generation": run.generation,
        "graph": {
            "revision": run.accepted_graph.revision_number,
            "digest": run.accepted_graph.content_digest,
            "stable": True,
            "candidate": None,
            "nodes": [
                {"id": node.id, "kind": node.kind.value, "state": latest_nodes.get(node.id, node).state.value}
                for node in run.accepted_graph.graph.nodes
            ],
        },
        "transitions": [item.model_dump(mode="json") for item in run.transitions],
        "node_transitions": [
            item.model_dump(mode="json")
            for node in latest_nodes.values() for item in node.transitions
        ],
        "gates": [item for item in results if item.get("node_id") in {
            node.id for node in run.accepted_graph.graph.nodes if node.kind.value == "gate"
        }],
        "artifacts": [item.model_dump(mode="json") for item in artifacts],
        "contracts": [node.output_contract.model_dump(mode="json") for node in run.accepted_graph.graph.nodes],
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "evidence_requirement_refs": sorted(requirement_ids),
        "review_decision": None,
        "context_provenance": [item.model_dump(mode="json") for item in contexts],
        "routing_reasons": routing,
        "metrics": [item.model_dump(mode="json") for item in metrics],
        "events": [item.model_dump(mode="json") for item in events],
    }


def compare_runs(store: SQLiteStore, left_id: str, right_id: str) -> dict[str, Any]:
    left = inspect_run(store, left_id)
    right = inspect_run(store, right_id)
    return {
        "left": {"run_id": left_id, "state": left["state"], "metrics": left["metrics"]},
        "right": {"run_id": right_id, "state": right["state"], "metrics": right["metrics"]},
        "same_graph_digest": left["graph"]["digest"] == right["graph"]["digest"],
    }


def serve(store: SQLiteStore, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve a local read-only JSON/HTML Inspector until interrupted."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.removeprefix("/api/runs/")
                try:
                    body = canonical_json(inspect_run(store, run_id)).encode()
                except KeyError:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = "application/json"
            elif parsed.path == "/":
                body = _INDEX.encode()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()


_INDEX = """<!doctype html><meta charset=utf-8><title>Fleet Inspector</title>
<style>body{font:14px system-ui;max-width:1000px;margin:2rem auto}input{width:24rem}pre{white-space:pre-wrap}</style>
<h1>Fleet Inspector</h1><p>Read-only local projection</p>
<input id=r placeholder="run id"><button onclick="loadRun()">Inspect</button><pre id=o></pre>
<script>async function loadRun(){let r=document.querySelector('#r').value;
let x=await fetch('/api/runs/'+encodeURIComponent(r));document.querySelector('#o').textContent=
x.ok?JSON.stringify(await x.json(),null,2):'Run not found'}</script>"""
