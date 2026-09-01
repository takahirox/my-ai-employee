"""Deterministic read-only incident classification over persisted Inspector facts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def doctor_from_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Build a copyable incident bundle without changing runtime state."""

    run_id = str(projection.get("run_id") or projection.get("id") or "not_recorded")
    incidents: list[dict[str, Any]] = []

    def add(code: str, source: Mapping[str, Any], **facts: object) -> None:
        item = {
            "code": code,
            "node_id": source.get("node_id"),
            "generation": source.get("generation"),
            "attempt": source.get("attempt"),
            "source_id": source.get("id"),
            "source_digest": source.get("content_digest"),
            **facts,
        }
        key = (code, item["node_id"], item["generation"], item["attempt"], item["source_id"])
        if not any(
            (
                existing["code"],
                existing.get("node_id"),
                existing.get("generation"),
                existing.get("attempt"),
                existing.get("source_id"),
            )
            == key
            for existing in incidents
        ):
            incidents.append(item)

    authorities = _records(projection.get("worker_timeout_authorities"))
    ownership_value = projection.get("run_ownership")
    ownership = dict(ownership_value) if isinstance(ownership_value, Mapping) else {}
    owner_sources = _records(ownership.get("owners"))
    owner_source_ids = [item.get("id") for item in owner_sources]
    owner_source_digests = [item.get("content_digest") for item in owner_sources]
    owner_facts = {
        "run_id": run_id,
        "graph_revision_digest": ownership.get("graph_revision_digest"),
        "execution_attempt": ownership.get("execution_attempt"),
        "owner_instance_id": ownership.get("owner_instance_id"),
        "last_heartbeat": ownership.get("last_heartbeat"),
        "lease_expiry": ownership.get("lease_expiry"),
        "source_record_ids": owner_source_ids,
        "source_record_digests": owner_source_digests,
        "child_run_ids": ownership.get("terminal_child_run_ids", []),
    }
    diagnostic_code = ownership.get("diagnostic_code")
    if diagnostic_code in {"RUN_OWNER_ABSENT", "RUN_LEASE_EXPIRED"}:
        add(str(diagnostic_code), ownership, **owner_facts)
    for item in _records(ownership.get("conflicts")):
        add("RUN_OWNER_CONFLICT", item, **owner_facts)
    for item in _records(ownership.get("fence_violations")):
        add("OWNER_FENCE_VIOLATION", item, operation=item.get("operation"), **owner_facts)
    child_run_ids = ownership.get("terminal_child_run_ids")
    parent_nonterminal = projection.get("state") == "running"
    if parent_nonterminal and isinstance(child_run_ids, list) and child_run_ids:
        add("CHILD_TERMINAL_PARENT_NONTERMINAL", ownership, **owner_facts)
    if parent_nonterminal and ownership.get("state") in {
        "orphaned",
        "parent_terminalization_missing",
    }:
        add("PARENT_TERMINALIZATION_MISSING", ownership, **owner_facts)
    for item in authorities:
        if float(item.get("effective_timeout_seconds") or 0) > float(
            item.get("node_attempt_timeout_seconds") or 0
        ):
            add("DEADLINE_NOT_PROPAGATED", item)
    for item in _records(projection.get("worker_budget_preflights")):
        add(
            "WORKER_BUDGET_INADEQUATE",
            item,
            denied_authorities=item.get("denied_authorities"),
            timeout_profile_digest=item.get("timeout_profile_digest"),
        )
    for item in _records(projection.get("worker_attempt_heartbeats")):
        if item.get("no_observable_progress"):
            add(
                "NO_OBSERVABLE_WORKER_PROGRESS",
                item,
                elapsed_seconds=item.get("elapsed_seconds"),
                remaining_seconds=item.get("remaining_seconds"),
                hard_timeout_reached=item.get("hard_timeout_reached"),
                early_cancel_authorized=item.get("early_cancel_authorized"),
            )
    for item in _records(projection.get("timeout_recoveries")):
        if item.get("action") != "same_strategy_retry":
            add(
                "TIMEOUT_RECOVERY_NOT_STARTED",
                item,
                action=item.get("action"),
                normal_acceptance_required=item.get("normal_acceptance_required"),
                alternate_fallback_authorized=item.get("alternate_fallback_authorized"),
            )
    watchdogs = _records(projection.get("node_watchdogs"))
    for item in watchdogs:
        add("WATCHDOG_TIMEOUT", item, outcome=item.get("outcome"))
        if item.get("outcome") == "cleanup_failed":
            add("PROCESS_GROUP_CLEANUP_FAILED", item)
    propagation_history = _records(projection.get("node_control_propagations"))
    latest_propagations: dict[tuple[object, object, object], dict[str, Any]] = {}
    for item in propagation_history:
        latest_propagations[
            (item.get("child_run_id"), item.get("generation"), item.get("attempt"))
        ] = item
    propagations = list(latest_propagations.values())
    for item in propagations:
        if not item.get("propagated"):
            add("CANCEL_NOT_PROPAGATED", item)
        if item.get("propagated") and not item.get("cleanup_confirmed"):
            add("PROCESS_GROUP_CLEANUP_FAILED", item)
    diagnostic_codes = {
        "WORKER_STRUCTURED_OUTPUT_MISSING": "STRUCTURED_OUTPUT_MISSING",
        "WORKER_ENVELOPE_MALFORMED": "ENVELOPE_INVALID",
        "DIFF_HUNK_AMBIGUOUS": "DIFF_HUNK_AMBIGUOUS",
        "PROCESS_GROUP_CLEANUP_FAILED": "PROCESS_GROUP_CLEANUP_FAILED",
    }
    child_outcomes = projection.get("child_worker_outcomes")
    child_outcomes = dict(child_outcomes) if isinstance(child_outcomes, Mapping) else {}
    diagnostics = _records(projection.get("worker_boundary_diagnostics")) + _records(
        child_outcomes.get("diagnostics")
    )
    for item in diagnostics:
        if code := diagnostic_codes.get(str(item.get("code"))):
            add(
                code,
                item,
                process_status=item.get("process_status"),
                effective_timeout_seconds=item.get("effective_timeout_seconds"),
            )
    diagnostic_failures = _records(projection.get("diagnostic_persistence_failures"))
    for item in diagnostic_failures:
        add("DIAGNOSTIC_PERSISTENCE_FAILED", item)
    for item in _records(child_outcomes.get("process_results")):
        failure = item.get("failure")
        failure_code = failure.get("code") if isinstance(failure, Mapping) else None
        if failure_code == "PROCESS_GROUP_CLEANUP_FAILED":
            add("PROCESS_GROUP_CLEANUP_FAILED", item)
    for item in _records(projection.get("nodes")):
        if item.get("failure_code") == "WORKER_BOUNDARY_ERROR" and not item.get(
            "worker_result_digest"
        ):
            add("WORKER_RESULT_ABSENT", item)
    for item in _records(projection.get("loop_transitions")):
        if item.get("action") == "ESCALATE" and "REPAIR" in str(item.get("reason_code")):
            add("REPAIR_EXHAUSTED", item)
    incidents.sort(
        key=lambda item: (
            str(item["code"]),
            str(item.get("node_id") or ""),
            int(item.get("generation") or 0),
            int(item.get("attempt") or 0),
            str(item.get("source_id") or ""),
        )
    )
    return {
        "schema_version": "1",
        "run_id": run_id,
        "state": projection.get("state", "not_recorded"),
        "authority": "read_only_classification",
        "incidents": incidents,
        "deadline_authorities": authorities,
        "watchdogs": watchdogs,
        "control_propagations": propagations,
        "worker_boundary_diagnostics": diagnostics,
        "child_worker_outcomes": child_outcomes,
        "diagnostic_persistence_failures": diagnostic_failures,
        "run_ownership": ownership,
    }
