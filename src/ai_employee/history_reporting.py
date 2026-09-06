"""Quality-first, body-free observations for explicit private corpus trials."""

from __future__ import annotations

from collections import Counter
from typing import cast

from .execution_profile import inspect_profile
from .graph_composition import GraphPatchCompositionRecord
from .graph_evaluation import ParentCandidateEvaluationRecord
from .history_corpus import CorpusTrialBinding, HistoricalStart, HistoricalTask
from .model_usage import inspect_usage
from .promotion_approval import validate_exact_parent_evidence_store
from .serialization import canonical_digest, project_harness_digest
from .storage import SQLiteStore
from .task_orchestration import (
    GraphRunRecord,
    LoopTransitionRecord,
    NodeRouteRecord,
    TaskGraphAcceptance,
)


def trial_report(store: SQLiteStore, task: HistoricalTask, run_id: str) -> dict[str, object]:
    run = store.get("graph_run_v2", run_id, GraphRunRecord)
    if (
        run.goal.model_dump(exclude={"id"}) != task.start.goal.model_dump(exclude={"id"})
        or run.base_commit != task.start.base_commit
        or run.harness_digest != project_harness_digest(task.start.harness)
        or run.effective_policy_digest != canonical_digest((task.start.policy.content_digest,))
        or run.execution_policy != task.execution_policy
    ):
        raise ValueError("trial does not bind the original task, baseline, checks and policy")
    environment_digest = None
    try:
        binding = store.get("corpus_trial_binding_v2", "corpus-trial-" + run_id, CorpusTrialBinding)
    except KeyError:
        pass
    else:
        profile = inspect_profile(store, run_id)
        if (
            binding.fixture_digest != task.content_digest
            or binding.logical_task_digest != task.logical_task_digest
            or binding.environment_digest != canonical_digest(task.environment)
            or profile is None
            or cast(dict[str, object], profile["choice"])["content_digest"]
            != binding.execution_profile_digest
        ):
            raise ValueError("trial fixture/environment/profile binding is stale")
        environment_digest = binding.environment_digest
    acceptances = tuple(
        item
        for item in store.list_records(
            "task_graph_acceptance_v2", TaskGraphAcceptance, run_id=run_id
        )
        if item.accepted_revision.content_digest == run.accepted_graph_revision_digest
    )
    accepted: bool | None = False if run.status in {"failed", "cancelled"} else None
    quality_reason = "No complete independent parent acceptance is available."
    if len(acceptances) == 1 and run.parent_evaluation_id is not None:
        try:
            evaluation = store.get(
                "parent_candidate_evaluation_v2",
                run.parent_evaluation_id,
                ParentCandidateEvaluationRecord,
            )
            if evaluation.content_digest != run.parent_evaluation_digest:
                raise ValueError("evaluation binding is stale")
            validate_exact_parent_evidence_store(
                store, run, acceptances[0].accepted_revision, evaluation, task.start.harness
            )
            accepted = True
            quality_reason = "Exact independent parent acceptance and declared checks verified."
        except (KeyError, ValueError):
            accepted = False if run.status in {"failed", "cancelled"} else None
            quality_reason = "Parent acceptance evidence is incomplete, stale or failed."
    graph = acceptances[0].accepted_revision.graph if len(acceptances) == 1 else None
    patch_bytes = None
    touched_files = None
    if run.composition_id is not None:
        try:
            composition = store.get(
                "graph_patch_composition_v2", run.composition_id, GraphPatchCompositionRecord
            )
            if (
                composition.content_digest == run.composition_digest
                and composition.candidate_patch is not None
            ):
                patch_bytes = composition.candidate_patch.size_bytes
                touched_files = len(
                    {path for item in composition.ordered_inputs for path in item.paths}
                )
        except KeyError:
            pass
    loops = store.list_records("loop_transition_v2", LoopTransitionRecord, run_id=run_id)
    recovery = Counter(item.action.value for item in loops)
    routes = store.list_records("node_route_v2", NodeRouteRecord, run_id=run_id)
    try:
        runtime_source_digest = store.get(
            "historical_start_v2", "start-" + run_id, HistoricalStart
        ).runtime_source_digest
    except KeyError:
        runtime_source_digest = None
    workers = tuple(
        sorted(
            {
                (
                    item.selected_strategy.backend,
                    item.selected_strategy.model,
                    item.selected_strategy.effort,
                )
                for item in routes
            }
        )
    )
    profile_projection = inspect_profile(store, run_id)
    return {
        "run_id": run_id,
        "runtime_source_digest": runtime_source_digest,
        "logical_task_digest": task.logical_task_digest,
        "task_class": task.task_class,
        "terminal_status": run.status,
        "failure_code": run.failure_code,
        "quality": {
            "independently_accepted": accepted,
            "declared_regressions_passed": accepted if accepted else None,
            "reason": quality_reason,
        },
        "complexity_scope": {
            "nodes": None if graph is None else len(graph.nodes),
            "edges": None if graph is None else len(graph.edges),
            "patch_bytes": patch_bytes,
            "touched_files": touched_files,
            "changed_loc": None,
            "unnecessary_work": None,
            "scope_drift": None,
            "speculative_abstractions": None,
        },
        "recovery": {
            key.lower(): recovery[key] for key in ("RETRY", "REPAIR", "REPLAN", "ESCALATE")
        },
        "profile": profile_projection,
        "usage": inspect_usage(store, (run_id,)),
        "worker_bindings": workers,
        "controlled_inputs": {
            "base_commit": run.base_commit,
            "harness_digest": run.harness_digest,
            "operator_config_digest": run.operator_config_digest,
            "policy": run.execution_policy.model_dump(mode="json"),
            "environment": environment_digest,
        },
        "human_active_seconds": None,
        "elapsed_through_last_invocation_seconds": (
            None
            if profile_projection is None
            else profile_projection["elapsed_through_last_invocation_seconds"]
        ),
        "human_interventions": None,
        "later_rework": None,
        "limitations": (
            "Unknown measurements are null. Declared checks are not proof of all possible "
            "regressions. Profile timings exclude inter-invocation waiting. This report does "
            "not infer human effort or engineering quality from patch size."
        ),
    }


def comparison_report(
    store: SQLiteStore, task: HistoricalTask, run_ids: tuple[str, ...]
) -> dict[str, object]:
    if not run_ids or len(run_ids) != len(set(run_ids)):
        raise ValueError("select one or more unique trial IDs; failed trials must not be dropped")
    reports = tuple(trial_report(store, task, run_id) for run_id in run_ids)
    comparable = len(reports) >= 2 and all(
        item["controlled_inputs"] == reports[0]["controlled_inputs"]
        and item["worker_bindings"] == reports[0]["worker_bindings"]
        and bool(item["worker_bindings"])
        and cast(dict[str, object], item["controlled_inputs"])["environment"] is not None
        for item in reports
    )
    return {
        "schema_version": "1",
        "kind": "private_history_corpus_report",
        "task_class": task.task_class,
        "logical_task_digest": task.logical_task_digest,
        "matching_recorded_controls": comparable,
        "trials": reports,
        "interpretation": (
            "Quality first; no automatic winner. Matching recorded controls do not prove "
            "unrecorded environment/model version equivalence. Use the productivity protocol "
            "for counterbalancing, repetitions and human measurements."
        ),
    }
