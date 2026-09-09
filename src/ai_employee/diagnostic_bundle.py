"""Bounded, read-only snapshots of related diagnostic records for disposable trials.

Only a structural projection leaves the database. Arbitrary strings and artifact
bodies are omitted; identifier references are consistently pseudonymized.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain.v2 import StableFailureCode

KINDS = frozenset(
    {
        "worker_request_v2",
        "worker_result_v2",
        "worker_boundary_diagnostic_v2",
        "model_process_diagnostic_v2",
        "worker_availability_v2",
        "model_progress_v2",
        "node_execution_v2",
        "work_run_v2",
        "graph_run_v2",
        "action_result_v2",
        "parent_semantic_review_request_v2",
        "parent_semantic_review_result_v2",
        "parent_semantic_review_decision_v2",
        "parent_review_failure_v2",
        "task_review_decision_v2",
        "stale_parent_semantic_review_result_v2",
        "parent_semantic_repair_request_v2",
        "parent_candidate_evaluation_request_v2",
        "parent_candidate_evaluation_v2",
        "evaluation_evidence_ledger_v2",
        "verification_result_v2",
        "artifact_descriptor_v2",
        "plan_review_failure_evidence_v2",
        "worker_timeout_authority_v2",
        "loop_transition_v2",
        "node_watchdog_v2",
        "node_control_propagation_v2",
        "node_evidence_v2",
        "node_evaluator_v2",
        "graph_patch_composition_v2",
        "execution_profile_v2",
        "execution_profile_timing_v2",
        "adaptive_execution_decision_v2",
    }
)
CODES = {value.value for value in StableFailureCode} | {
    "PARENT_REVIEW_REQUEST_BINDING_FAILED",
    "PARENT_REVIEW_FAILED",
    "STAGE_POLICY_BINDING_INVALID",
    "STAGE_PROCESS_BINDING_INVALID",
    "TASK_REVIEW_REQUEST_BINDING_INVALID",
    "TASK_REVIEW_INVALID_JSON",
    "TASK_REVIEW_INVALID_SCHEMA",
    "TASK_REVIEW_REFERENCE_MISMATCH",
    "TASK_REVIEW_PROCESS_FAILED",
    "TASK_REVIEW_FAILED",
    "contract_mismatch",
    "PARENT_REVIEW_INVALID_JSON",
    "PARENT_REVIEW_INVALID_SCHEMA",
    "PARENT_REVIEW_CRITERIA_MISMATCH",
    "PARENT_REVIEW_NODES_MISMATCH",
    "PARENT_REVIEW_CONTRACT_MISMATCH",
    "PARENT_SEMANTIC_REVIEW_UNAVAILABLE",
    "PARENT_SEMANTIC_COVERAGE_LIMITED",
    "PARENT_SEMANTIC_BINDING_MISMATCH",
    "PARENT_VERIFICATION_FAILED",
    "PARENT_CANDIDATE_STALE",
    "NODE_EXECUTION_FAILED",
    "REPAIR_BUDGET_EXHAUSTED",
    "TIMEOUT",
    "CANCELLED",
    "GRAPH_CANCELLED",
    "GRAPH_PAUSED",
    "RUN_WALL_BUDGET_EXCEEDED",
    "WATCHDOG_TIMEOUT",
    "WATCHDOG_TIMEOUT:CLEANUP_UNCONFIRMED",
    "WORKER_ENVELOPE_MALFORMED",
    "EDIT_INTENT_DIFF_INVALID",
    "existing_path_in_new_file_proposal",
    "stale_workspace_baseline",
}
ENUMS = {
    "lightweight",
    "adaptive",
    "fixed",
    "direct",
    "planned",
    "selected",
    "required",
    "omitted",
    "goal_assessment",
    "planning",
    "node_assessment",
    "task_review",
    "artifact_review",
    "verification_and_approval",
    "invocation_start",
    "before_first_worker",
    "invocation",
    "read_only_or_local_reversible",
    "external_or_protected_change",
    "succeeded",
    "failed",
    "cancelled",
    "running",
    "pending",
    "ready_to_promote",
    "completed",
    "blocked",
    "queued",
    "timed_out",
    "PASS",
    "FAIL",
    "REPAIR",
    "ESCALATE",
    "worker",
    "planner",
    "plan_review",
    "parent_review",
    "parent_semantic_review",
    "probe",
    "process",
    "transport",
    "envelope",
    "typed_result",
    "runner",
    "pre_dispatch",
    "request_binding",
    "review",
    "response_parse",
    "response_contract",
    "ValueError",
    "TypeError",
    "KeyError",
    "OSError",
    "RuntimeError",
    "Other",
    "codex_cli",
    "claude_code_cli",
    "ollama_cli",
    "available",
    "unavailable",
    "usage_limit",
    "rate_limit",
    "authentication",
    "invalid_request",
    "invalid_json",
    "unknown",
    "cancel",
    "signal_sent",
    "cleanup_confirmed",
    "cleanup_failed",
    "REPLAN",
    "RETRY",
    "satisfied",
    "uncovered",
    "conflicted",
    "public",
    "redacted",
    "secret",
    "workspace_patch",
    "process_stdout",
    "process_stderr",
} | CODES
CONTAINERS = {
    "choice",
    "adaptive_execution",
    "effective_stages",
    "timings",
    "stages",
    "recommendation",
    "failure",
    "details",
    "resource_usage",
    "criterion_evidence",
    "node_bindings",
    "artifact_descriptors",
    "verification_bindings",
    "observation_manifest",
    "artifacts",
    "transport_failures",
    "findings",
    "proposals",
    "payload",
    "candidate",
    "candidate_artifact",
    "candidate_descriptor",
    "deterministic_ledgers",
    "verification_results",
    "evidence",
}
NUMBERS = {
    "seconds",
    "timing_complete",
    "active_invocation_wall_seconds",
    "completed_invocation_wall_seconds",
    "elapsed_through_last_invocation_seconds",
    "scope_clear",
    "criteria_clear",
    "coordinated_work_required",
    "planning_requested",
    "generation",
    "attempt",
    "review_attempt",
    "sequence",
    "output_generation",
    "exit_code",
    "duration_seconds",
    "size_bytes",
    "stdout_bytes",
    "stderr_bytes",
    "retryable",
    "expected_criteria",
    "received_criteria",
    "expected_nodes",
    "received_nodes",
    "file_count",
    "hunk_count",
    "complete",
    "propagated",
    "cleanup_confirmed",
    "consumed",
    "limit",
    "allowance_seconds",
    "cleanup_grace_seconds",
    "effective_timeout_seconds",
}
STRINGS = {
    "profile",
    "routing_mode",
    "path",
    "phase",
    "effect_scope",
    "status",
    "stage",
    "code",
    "failure_code",
    "reason_code",
    "cause",
    "exception_kind",
    "action",
    "adapter",
    "availability",
    "logical_kind",
    "redaction_state",
    "outcome",
    "disposition",
}
REFERENCES = {
    "decision_digest",
    "assessment_digest",
    "profile_digest",
    "execution_profile_digest",
    "operator_config_digest",
    "initial_worker_strategy_id",
    "fixed_strategy_id",
    "id",
    "run_id",
    "graph_run_id",
    "node_id",
    "work_run_id",
    "child_run_id",
    "worker_request_id",
    "worker_result_id",
    "process_request_id",
    "process_result_id",
    "producer_action_id",
    "workspace_id",
    "evidence_id",
    "evaluator_id",
    "criterion_id",
    "reviewed_criterion_ids",
    "reviewed_node_ids",
    "criterion_ids",
    "node_ids",
    "request_digest",
    "result_digest",
    "content_digest",
    "response_digest",
    "candidate_digest",
    "candidate_artifact_digest",
    "candidate_descriptor_digest",
    "worker_request_digest",
    "worker_result_digest",
    "process_request_digest",
    "process_result_digest",
    "accepted_graph_revision_digest",
    "effective_policy_digest",
    "harness_digest",
    "artifact_digest",
    "artifact_digests",
    "evidence_digest",
    "evidence_digests",
    "evidence_refs",
    "verification_result_digests",
    "deterministic_ledger_digests",
    "workspace_digest",
    "patch_digest",
    "patch_descriptor_digest",
    "patch_artifact_id",
    "expected_candidate_digest",
    "result_candidate_digest",
    "composition_record_digest",
    "stdout_artifact_digest",
    "stderr_artifact_digest",
    "verification_result_digest",
    "candidate_patch_digest",
    "completed_action_digests",
}
MAX_RECORDS = 10_000
MAX_PAYLOAD = 1_000_000
MAX_BUNDLE = 8_000_000
MAX_SCAN_BYTES = 16_000_000


def reference(value: str) -> str:
    return "ref-" + hashlib.sha256(value.encode()).hexdigest()


def project(value: Any, key: str = "", depth: int = 0, *, limits: set[str] | None = None) -> Any:
    """No free-form string survives by matching the name of a trusted field."""
    if depth > 12:
        if limits is not None:
            limits.add("projection_depth")
        return None
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if not isinstance(name, str):
                continue
            if (
                name in CONTAINERS | NUMBERS | STRINGS
                or name in {"id", "created_at", "transitioned_at"}
                or name in REFERENCES
            ):
                projected = project(item, name, depth + 1, limits=limits)
                if projected is not None:
                    result[name] = projected
        return result
    if isinstance(value, list):
        if len(value) > 256 and limits is not None:
            limits.add("array_items")
        return [
            project(item, key.removesuffix("s"), depth + 1, limits=limits) for item in value[:256]
        ]
    if isinstance(value, str):
        if key == "id" or key.endswith("_id"):
            return reference(value)
        if (key.endswith("_digest") or key == "evidence_ref") and re.fullmatch(
            r"[0-9a-f]{64}", value
        ):
            return value
        if key in {"created_at", "transitioned_at"}:
            try:
                parsed = datetime.fromisoformat(value)
                return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
            except (ValueError, OverflowError):
                return None
        return value if value in ENUMS and key in STRINGS | {"transport_failure"} else None
    if key in NUMBERS and type(value) in (int, float, bool):
        return value
    return None


def snapshot(database: Path, root_run_id: str) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "root_run_id": reference(root_run_id),
        "snapshot_at": datetime.now(UTC).isoformat(),
        "state": "running",
        "records": [],
        "omissions": [
            "free_text_and_artifact_bodies",
            "unlisted_fields_and_kinds",
            "arrays_bounded_to_256_and_depth_to_12",
        ],
        "limits": {
            "records": MAX_RECORDS,
            "payload_bytes": MAX_PAYLOAD,
            "bundle_bytes": MAX_BUNDLE,
            "scan_bytes": MAX_SCAN_BYTES,
            "array_items": 256,
            "projection_depth": 12,
        },
        "truncated": False,
    }
    if not database.is_file():
        bundle["collection_status"] = "database_not_created"
        return bundle
    # mode=ro neither creates nor migrates the database and never changes execution state.
    with closing(
        sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    ) as connection:
        connection.execute("BEGIN")
        related = {root_run_id}
        pending = [root_run_id]
        scanned = 0
        scanned_rows = 0
        while (
            pending
            and len(related) < 256
            and scanned < MAX_SCAN_BYTES
            and scanned_rows < MAX_RECORDS
        ):
            parent = pending.pop()
            rows = connection.execute(
                "SELECT substr(payload,1,?),length(CAST(payload AS BLOB)) "
                "FROM records WHERE run_id=? "
                "AND kind IN ('worker_request_v2','node_execution_v2') ORDER BY rowid LIMIT ?",
                (MAX_PAYLOAD, parent, MAX_RECORDS + 1),
            )
            for encoded, length in rows:
                scanned += len(encoded.encode())
                scanned_rows += 1
                if scanned > MAX_SCAN_BYTES or scanned_rows > MAX_RECORDS:
                    bundle["truncated"] = True
                    break
                if length > MAX_PAYLOAD:
                    bundle["truncated"] = True
                    continue
                try:
                    record = json.loads(encoded)
                except ValueError:
                    bundle["truncated"] = True
                    continue
                # These two runtime records bind a dispatched child to its parent.
                if not isinstance(record, dict):
                    bundle["truncated"] = True
                    continue
                child = record.get("work_run_id")
                if record.get("graph_run_id") == parent:
                    child = record.get("run_id")
                if isinstance(child, str) and child not in related:
                    related.add(child)
                    pending.append(child)
                    if len(related) >= 256:
                        break
        if pending:
            bundle["truncated"] = True
        bundle["related_run_ids"] = sorted(reference(item) for item in related)
        placeholders = ",".join("?" for _ in related)
        kinds = ",".join("?" for _ in KINDS)
        rows = connection.execute(
            f"SELECT rowid,kind,run_id,revision,length(CAST(payload AS BLOB)),substr(payload,1,?) "
            f"FROM records WHERE run_id IN ({placeholders}) AND kind IN ({kinds}) "
            "ORDER BY rowid LIMIT ?",
            (MAX_PAYLOAD, *sorted(related), *sorted(KINDS), MAX_RECORDS + 1),
        )
        size = 0
        projection_limits: set[str] = set()
        for sequence, kind, run_id, revision, length, encoded in rows:
            scanned += len(encoded.encode())
            if scanned > MAX_SCAN_BYTES:
                bundle["truncated"] = True
                break
            if len(bundle["records"]) >= MAX_RECORDS:
                bundle["truncated"] = True
                break
            if length > MAX_PAYLOAD:
                bundle["truncated"] = True
                continue
            try:
                raw = json.loads(encoded)
                if not isinstance(raw, dict):
                    bundle["truncated"] = True
                    continue
                payload = project(raw, limits=projection_limits)
            except (TypeError, ValueError):
                bundle["truncated"] = True
                continue
            item = {
                "storage_sequence": sequence,
                "kind": kind,
                "run_id": reference(run_id),
                "revision": revision,
                "record": payload,
            }
            size += len(json.dumps(item).encode())
            if size > MAX_BUNDLE - 100_000:
                bundle["truncated"] = True
                break
            bundle["records"].append(item)
        bundle["projection_limits_reached"] = sorted(projection_limits)
        if projection_limits:
            bundle["truncated"] = True
        # Keep the original storage sequence too; time alone is not causality.
        bundle["records"].sort(
            key=lambda item: (item["record"].get("created_at", ""), item["storage_sequence"])
        )
    bundle["collection_status"] = "partial" if bundle["truncated"] else "collected"
    return bundle


def record_export_error(logs: Path) -> None:
    # If the output medium itself is unavailable, even this marker cannot be retained.
    with suppress(OSError):
        (logs / "fleet-diagnostic-export-error.json").write_text(
            '{"code":"DIAGNOSTIC_EXPORT_FAILED","last_snapshot_retained":true}'
        )


class DiagnosticCollector:
    """Periodically replace one export atomically; retain the last successful snapshot."""

    def __init__(self, database: Path, logs: Path, run_id: str, *, interval: float = 2.0):
        self.database, self.logs, self.run_id = database.resolve(), logs, run_id
        self.interval = interval
        self.has_collected = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def __enter__(self) -> DiagnosticCollector:
        self.collect()
        self.thread.start()
        return self

    def collect(self, state: str = "running") -> None:
        try:
            bundle = snapshot(self.database, self.run_id)
            if bundle["collection_status"] == "database_not_created" and self.has_collected:
                record_export_error(self.logs)
                return
            bundle["state"] = state
            target = self.logs / "fleet-diagnostic-bundle.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(bundle, separators=(",", ":")))
            temporary.replace(target)
            self.has_collected = bundle["collection_status"] != "database_not_created"
        except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
            # Do not replace good evidence with an empty snapshot or leak exception text.
            record_export_error(self.logs)

    def _loop(self) -> None:
        while not self.stop.wait(self.interval):
            self.collect()

    def __exit__(self, exc_type: object, *_args: object) -> None:
        self.stop.set()
        self.thread.join()
        self.collect("interrupted" if exc_type else "controller_finished")
