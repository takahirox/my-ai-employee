"""Deterministic, read-only explanations of one persisted Fleet run."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .storage import SQLiteStore


def explain_any_run(store: SQLiteStore, run_id: str) -> dict[str, Any]:
    """Explain a running or historical run without invoking workers or reading bodies."""

    # Keep the raw Inspector projection as the only storage reconstruction path.  This
    # layer deliberately summarizes those persisted facts instead of creating another
    # telemetry store or a second source of runtime authority.
    from .inspector import inspect_any_run

    inspected = inspect_any_run(store, run_id)
    kind = inspected.get("kind")
    if kind == "graph_run":
        explanation = _explain_graph_run(inspected)
        _attach_child_work_runs(store, explanation)
        return explanation
    if kind == "work_run":
        return _explain_work_run(inspected)
    return _explain_legacy_run(inspected)


def _attach_child_work_runs(store: SQLiteStore, explanation: dict[str, Any]) -> None:
    """Attach bounded child-run causes referenced by authoritative node attempts."""

    from .inspector import inspect_work_run

    parent_failures = explanation.get("failure_path")
    if not isinstance(parent_failures, list):
        return
    for story in _mappings(explanation.get("task_stories")):
        child_ids = tuple(
            dict.fromkeys(
                str(attempt["work_run_id"])
                for attempt in _mappings(story.get("execution_attempts"))
                if attempt.get("work_run_id")
            )
        )
        child_summaries: list[dict[str, Any]] = []
        for child_id in child_ids:
            try:
                child = _explain_work_run(inspect_work_run(store, child_id))
            except KeyError:
                child_summaries.append(
                    {
                        "run_id": child_id,
                        "status": "unknown",
                        "diagnostic": "referenced child WorkRun is unavailable",
                    }
                )
                continue
            child_tasks = _mappings(child.get("task_stories"))
            summary = {
                "run_id": child_id,
                "status": _mapping(child.get("current_state")).get("status"),
                "policy_decisions": []
                if not child_tasks
                else child_tasks[0].get("policy_decisions", []),
                "failure_path": child.get("failure_path", []),
                "final_outcome": child.get("final_outcome"),
            }
            child_summaries.append(summary)
            for cause in _mappings(child.get("failure_path")):
                parent_failures.append(
                    {
                        "stage": "child_work_run",
                        "task_id": story.get("task_id"),
                        "child_run_id": child_id,
                        "cause": cause,
                    }
                )
        story["child_work_runs"] = child_summaries


def _explain_graph_run(view: dict[str, Any]) -> dict[str, Any]:
    run = _mapping(view.get("run"))
    acceptance = _mapping(view.get("graph_acceptance"))
    accepted_revision = _mapping(acceptance.get("accepted_revision"))
    proposal = _mapping(view.get("proposed_graph"))
    graph = _mapping(accepted_revision.get("graph")) or _mapping(proposal.get("graph"))
    goal = _mapping(run.get("goal")) or _mapping(view.get("goal"))
    graph_nodes = _mappings(graph.get("nodes"))
    graph_edges = _mappings(graph.get("edges"))
    revision_digest = _optional_text(accepted_revision.get("content_digest"))
    graph_reference_digest = revision_digest or _optional_text(proposal.get("content_digest"))

    dependencies: dict[str, list[str]] = {str(node["id"]): [] for node in graph_nodes}
    dependents: dict[str, list[str]] = {str(node["id"]): [] for node in graph_nodes}
    for edge in graph_edges:
        source = str(edge["source_id"])
        target = str(edge["target_id"])
        dependencies.setdefault(target, []).append(source)
        dependents.setdefault(source, []).append(target)

    history = _mappings(view.get("node_history"))
    current_history = [
        item for item in history if item.get("accepted_graph_revision_digest") == revision_digest
    ]
    latest_nodes = _latest_by_node(current_history)
    durable_status = {
        str(node["id"]): str(latest_nodes.get(str(node["id"]), {}).get("status", "pending"))
        for node in graph_nodes
    }

    task_positions: dict[str, str] = {}
    for node in graph_nodes:
        node_id = str(node["id"])
        status = durable_status[node_id]
        predecessor_statuses = [
            durable_status.get(item, "pending") for item in dependencies[node_id]
        ]
        if not accepted_revision:
            position = "not_accepted"
        elif status == "passed":
            position = "completed"
        elif status in {"routed", "running"}:
            position = "active"
        elif status in {"failed", "blocked", "cancelled"}:
            position = status
        elif any(item in {"failed", "blocked", "cancelled"} for item in predecessor_statuses):
            position = "blocked"
        elif any(item != "passed" for item in predecessor_statuses):
            position = "waiting"
        else:
            position = "ready"
        task_positions[node_id] = position

    graph_tasks = [
        {
            "id": str(node["id"]),
            "name": node.get("name"),
            "objective": node.get("objective"),
            "kind": node.get("kind"),
            "dependencies": dependencies[str(node["id"])],
            "dependents": dependents[str(node["id"])],
            "execution_state": durable_status[str(node["id"])],
            "position": task_positions[str(node["id"])],
            "authority": "accepted" if accepted_revision else "proposed_only",
        }
        for node in graph_nodes
    ]
    position_counts = Counter(task_positions.values())

    routes = _mappings(view.get("routes"))
    contexts = _mappings(view.get("worker_context_manifests"))
    evidence = _mappings(view.get("node_evidence"))
    evaluators = _mappings(view.get("node_evaluator_decisions"))
    review_results = _mappings(_mapping(view.get("task_reviews")).get("results"))
    review_decisions = _mappings(_mapping(view.get("task_reviews")).get("decisions"))
    loop_transitions = _mappings(view.get("loop_transitions"))

    task_stories = [
        _graph_task_story(
            node,
            dependencies=dependencies[str(node["id"])],
            status=durable_status[str(node["id"])],
            position=task_positions[str(node["id"])],
            latest=latest_nodes.get(str(node["id"])),
            history=current_history,
            routes=routes,
            contexts=contexts,
            evidence=evidence,
            evaluators=evaluators,
            review_results=review_results,
            review_decisions=review_decisions,
            loop_transitions=loop_transitions,
            revision_digest=revision_digest,
            authoritative_generation=int(view.get("generation", 0)),
        )
        for node in graph_nodes
    ]

    promotion_approval = _promotion_approval_story(view)
    current_state = {
        "status": view.get("state"),
        "generation": view.get("generation"),
        "graph_revision": accepted_revision.get("revision_number"),
        "task_counts": dict(sorted(position_counts.items())),
        "active_task_ids": _ids_at(task_positions, "active"),
        "ready_task_ids": _ids_at(task_positions, "ready"),
        "waiting_task_ids": _ids_at(task_positions, "waiting"),
        "blocked_task_ids": _ids_at(task_positions, "blocked"),
        "failed_task_ids": _ids_at(task_positions, "failed"),
        "promotion_approval_state": None
        if promotion_approval is None
        else promotion_approval.get("decision"),
        "next_action": _graph_next_action(str(view.get("state", "unknown")), promotion_approval),
    }

    return {
        "schema_version": "2",
        "kind": "run_explanation",
        "source_kind": "graph_run",
        "run_id": view.get("run_id"),
        "observation": _observation_contract(),
        "goal": {
            "id": run.get("goal_id") or goal.get("id"),
            "statement": goal.get("statement"),
            "unavailable_reason": goal.get("unavailable_reason"),
            "completion_criteria": [
                {"id": item.get("id"), "description": item.get("description")}
                for item in _mappings(goal.get("completion_criteria"))
            ],
        },
        "current_state": current_state,
        "graph": {
            "id": graph.get("id"),
            "accepted": bool(accepted_revision),
            "revision": accepted_revision.get("revision_number"),
            "digest": graph_reference_digest,
            "entry_task_ids": graph.get("entry_node_ids", []),
            "terminal_task_ids": graph.get("terminal_node_ids", []),
            "tasks": graph_tasks,
            "evolution": _graph_evolution(view),
        },
        "plan_decisions": {
            "planner_routing": _planner_routing_story(view.get("planner_routing")),
            "plan_review": _plan_review_story(view.get("plan_review")),
        },
        "task_stories": task_stories,
        "failure_path": _graph_failure_path(view, task_stories),
        "final_outcome": _graph_final_outcome(view, promotion_approval),
    }


def _graph_task_story(
    node: dict[str, Any],
    *,
    dependencies: list[str],
    status: str,
    position: str,
    latest: dict[str, Any] | None,
    history: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    evaluators: list[dict[str, Any]],
    review_results: list[dict[str, Any]],
    review_decisions: list[dict[str, Any]],
    loop_transitions: list[dict[str, Any]],
    revision_digest: str | None,
    authoritative_generation: int,
) -> dict[str, Any]:
    node_id = str(node["id"])
    node_attempts = sorted(
        (item for item in history if item.get("node_id") == node_id),
        key=_attempt_key,
    )
    route = _latest_matching(routes, node_id, revision_digest)
    context = _latest_matching(contexts, node_id, revision_digest)
    node_evidence = _latest_matching(evidence, node_id, revision_digest)
    evaluator = _latest_matching(evaluators, node_id, revision_digest)
    review = _latest_matching(review_decisions, node_id, revision_digest)
    review_result = None
    if review is not None:
        result_digest = review.get("result_digest")
        review_result = next(
            (item for item in review_results if item.get("content_digest") == result_digest), None
        )
    loops = [
        _loop_summary(item)
        for item in loop_transitions
        if item.get("node_id") == node_id
        and item.get("accepted_graph_revision_digest") == revision_digest
    ]
    criteria = _mappings(node.get("completion_criteria"))

    return {
        "task_id": node_id,
        "objective": node.get("objective") or node.get("name"),
        "completion_criteria": [
            {"id": item.get("id"), "description": item.get("description")} for item in criteria
        ],
        "dependencies": dependencies,
        "state": status,
        "position": position,
        "why_this_state": _why_task_state(status, position, latest, evaluator, review, loops),
        "routing": _route_story(route),
        "information_flow": _context_story(node, context),
        "execution_attempts": [
            {
                "generation": item.get("generation"),
                "attempt": item.get("attempt"),
                "sequence": item.get("sequence"),
                "status": item.get("status"),
                "authoritative_for_current_state": latest is not None
                and item.get("content_digest") == latest.get("content_digest"),
                "generation_matches_run": item.get("generation") == authoritative_generation,
                "route_digest": item.get("route_digest"),
                "worker_request_digest": item.get("worker_request_digest"),
                "worker_result_digest": item.get("worker_result_digest"),
                "evidence_digest": item.get("evidence_digest"),
                "evaluator_digest": item.get("evaluator_digest"),
                "evaluator_decision": item.get("evaluator_decision"),
                "work_run_id": item.get("work_run_id"),
                "verification_result_digests": item.get("verification_result_digests", []),
                "failure_code": item.get("failure_code"),
            }
            for item in node_attempts
        ],
        "evidence": None
        if node_evidence is None
        else {
            "digest": node_evidence.get("content_digest"),
            "criteria": node_evidence.get("criteria", []),
        },
        "evaluation": None
        if evaluator is None
        else {
            "decision": evaluator.get("decision"),
            "evidence_digest": evaluator.get("evidence_digest"),
            "worker_result_digest": evaluator.get("worker_result_digest"),
        },
        "review": None
        if review is None
        else {
            "action": review.get("action"),
            "reason_code": review.get("reason_code"),
            "accepted_finding_digests": review.get("accepted_finding_digests", []),
            "findings": [] if review_result is None else review_result.get("findings", []),
            "limitations": [] if review_result is None else review_result.get("limitations", []),
        },
        "loop_decisions": loops,
        "artifacts": [
            {
                "id": item.get("id"),
                "logical_kind": item.get("logical_kind"),
                "artifact_digest": item.get("artifact_digest"),
                "redaction_state": item.get("redaction_state"),
            }
            for item in _mappings((latest or {}).get("artifact_descriptors"))
        ],
    }


def _route_story(route: dict[str, Any] | None) -> dict[str, Any] | None:
    if route is None:
        return None
    assessment = _mapping(route.get("assessment"))
    selected = _mapping(route.get("selected_strategy"))
    return {
        "selected_strategy": _strategy_summary(selected),
        "eligible_strategy_ids": route.get("eligible_strategy_ids", []),
        "assessment": {
            "complexity": assessment.get("complexity"),
            "scale": assessment.get("scale"),
            "risk": assessment.get("risk"),
            "required_capabilities": assessment.get("required_capabilities", []),
            "reasons": assessment.get("reasons", []),
        },
        "selection_reasons": selected.get("routing_reasons", []),
    }


def _context_story(node: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {
            "manifest_recorded": False,
            "goal_or_objective": node.get("objective") or node.get("name"),
            "required_capabilities": node.get("required_capabilities", []),
            "artifact_bodies_included": False,
        }
    return {
        "manifest_recorded": True,
        "manifest_digest": context.get("content_digest"),
        "goal_or_objective": node.get("objective") or node.get("name"),
        "required_capabilities": context.get("required_capabilities", []),
        "predecessor_task_ids": context.get("predecessor_node_ids", []),
        "predecessor_result_digests": context.get("predecessor_result_digests", []),
        "predecessor_evidence_digests": context.get("predecessor_evidence_digests", []),
        "accepted_feedback_digests": context.get("accepted_feedback_digests", []),
        "artifact_descriptors": context.get("artifact_descriptors", []),
        "conversation_history_included": context.get("conversation_history_included", False),
        "artifact_bodies_included": context.get("artifact_bodies_included", False),
    }


def _why_task_state(
    status: str,
    position: str,
    latest: dict[str, Any] | None,
    evaluator: dict[str, Any] | None,
    review: dict[str, Any] | None,
    loops: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    if latest is not None and latest.get("failure_code"):
        reasons.append(f"runtime recorded failure {latest['failure_code']}")
    if evaluator is not None:
        reasons.append(f"deterministic evaluation decided {evaluator.get('decision')}")
    if review is not None:
        reasons.append(f"task review decided {review.get('action')}: {review.get('reason_code')}")
    if loops:
        last = loops[-1]
        reasons.append(f"closed loop decided {last['action']}: {last['reason_code']}")
    if not reasons:
        reasons.append(
            {
                "completed": "the latest durable task state is passed",
                "active": "the latest durable task state is active",
                "waiting": "one or more predecessor tasks have not passed",
                "blocked": "the task or one of its predecessors cannot proceed",
                "ready": "all predecessor tasks passed; execution has not started",
                "not_accepted": "the proposed task graph was never accepted for execution",
                "failed": "the latest durable task state is failed",
                "cancelled": "the task was cancelled",
            }.get(position, f"the latest durable task state is {status}")
        )
    return reasons


def _graph_evolution(view: dict[str, Any]) -> list[dict[str, Any]]:
    revisions = sorted(
        _mappings(view.get("graph_revisions")),
        key=lambda item: int(_mapping(item.get("accepted_revision")).get("revision_number", 0)),
    )
    previous_nodes: set[str] = set()
    previous_summaries: dict[str, dict[str, Any]] = {}
    retention_records = _mappings(view.get("retained_node_bindings"))
    node_history = _mappings(view.get("node_history"))
    historical_nodes_by_digest = {
        item.get("content_digest"): item.get("node_id")
        for item in _mappings(view.get("node_history"))
        if item.get("content_digest") and item.get("node_id")
    }
    evolution: list[dict[str, Any]] = []
    if not revisions:
        proposal = _mapping(view.get("proposed_graph"))
        proposed_graph = _mapping(proposal.get("graph"))
        if proposed_graph:
            proposed_summaries = _revision_task_summaries(
                proposed_graph, None, node_history, accepted=False
            )
            return [
                {
                    "revision": None,
                    "digest": proposal.get("content_digest"),
                    "previous_revision_digest": None,
                    "trigger": "proposal was not accepted",
                    "evidence_digests": [],
                    "triggered_by_task_ids": [],
                    "added_task_ids": sorted(
                        str(item["id"]) for item in _mappings(proposed_graph.get("nodes"))
                    ),
                    "tasks": proposed_summaries,
                    "added_task_summaries": proposed_summaries,
                    "removed_task_summaries": [],
                    "removed_task_ids": [],
                    "retained_task_ids": [],
                    "redone_task_ids": [],
                }
            ]
    for item in revisions:
        accepted = _mapping(item.get("accepted_revision"))
        graph = _mapping(accepted.get("graph"))
        current_nodes = {str(node["id"]) for node in _mappings(graph.get("nodes"))}
        current_summaries = {
            str(item["id"]): item
            for item in _revision_task_summaries(
                graph, _optional_text(accepted.get("content_digest")), node_history
            )
        }
        exact_retained = {
            str(binding["node_id"])
            for binding in retention_records
            if binding.get("accepted_graph_revision_digest") == accepted.get("content_digest")
        }
        replan_evidence = item.get("replan_evidence", [])
        evolution.append(
            {
                "revision": accepted.get("revision_number"),
                "digest": accepted.get("content_digest"),
                "previous_revision_digest": item.get("previous_revision_digest"),
                "trigger": item.get("replan_trigger") or "initial plan accepted",
                "evidence_digests": replan_evidence,
                "triggered_by_task_ids": sorted(
                    {
                        str(historical_nodes_by_digest[digest])
                        for digest in replan_evidence
                        if digest in historical_nodes_by_digest
                    }
                ),
                "added_task_ids": sorted(current_nodes - previous_nodes),
                "removed_task_ids": sorted(previous_nodes - current_nodes),
                "tasks": [current_summaries[node_id] for node_id in sorted(current_summaries)],
                "added_task_summaries": [
                    current_summaries[node_id] for node_id in sorted(current_nodes - previous_nodes)
                ],
                "removed_task_summaries": [
                    previous_summaries[node_id]
                    for node_id in sorted(previous_nodes - current_nodes)
                ],
                "retained_task_ids": sorted(exact_retained),
                "redone_task_ids": sorted((current_nodes & previous_nodes) - exact_retained),
            }
        )
        previous_nodes = current_nodes
        previous_summaries = current_summaries
    return evolution


def _revision_task_summaries(
    graph: dict[str, Any],
    revision_digest: str | None,
    node_history: list[dict[str, Any]],
    *,
    accepted: bool = True,
) -> list[dict[str, Any]]:
    nodes = _mappings(graph.get("nodes"))
    dependencies: dict[str, list[str]] = {str(item["id"]): [] for item in nodes}
    for edge in _mappings(graph.get("edges")):
        dependencies.setdefault(str(edge["target_id"]), []).append(str(edge["source_id"]))
    revision_records = [
        item
        for item in node_history
        if item.get("accepted_graph_revision_digest") == revision_digest
    ]
    latest = _latest_by_node(revision_records)
    return [
        {
            "id": str(node["id"]),
            "name": node.get("name"),
            "objective": node.get("objective"),
            "dependencies": dependencies[str(node["id"])],
            "historical_state": latest.get(str(node["id"]), {}).get(
                "status", "pending" if accepted else "not_accepted"
            ),
            "authority": "historical_accepted" if accepted else "proposed_only",
        }
        for node in nodes
    ]


def _graph_failure_path(
    view: dict[str, Any], task_stories: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    path: list[dict[str, Any]] = []
    for attempt in _mappings(_mapping(view.get("plan_review")).get("attempts")):
        if attempt.get("action") != "accept":
            path.append(
                {
                    "stage": "plan_review",
                    "round": attempt.get("review_round"),
                    "outcome": attempt.get("outcome"),
                    "action": attempt.get("action"),
                    "reason_code": attempt.get("failure_code"),
                    "finding_ids": [item.get("id") for item in _mappings(attempt.get("findings"))],
                }
            )
    for revision in _mappings(_mapping(view.get("plan_review")).get("revision_attempts")):
        if revision.get("status") == "failed":
            path.append(
                {
                    "stage": "plan_revision",
                    "outcome": "failed",
                    "reason_code": revision.get("failure_code"),
                    "source_proposed_graph_digest": revision.get("source_proposed_graph_digest"),
                }
            )
    for story in task_stories:
        for attempt in story["execution_attempts"]:
            if attempt["failure_code"] or attempt["status"] in {
                "failed",
                "blocked",
                "cancelled",
            }:
                path.append(
                    {
                        "stage": "task_execution",
                        "task_id": story["task_id"],
                        "generation": attempt["generation"],
                        "attempt": attempt["attempt"],
                        "outcome": attempt["status"],
                        "reason_code": attempt["failure_code"],
                    }
                )
        for decision in story["loop_decisions"]:
            if decision["action"] != "PASS":
                path.append({"stage": "closed_loop", "task_id": story["task_id"], **decision})
    current_revision_digest = _mapping(
        _mapping(view.get("graph_acceptance")).get("accepted_revision")
    ).get("content_digest")
    replan_by_evidence: dict[str, dict[str, Any]] = {}
    for acceptance in _mappings(view.get("graph_revisions")):
        accepted = _mapping(acceptance.get("accepted_revision"))
        for digest in acceptance.get("replan_evidence", []):
            replan_by_evidence[str(digest)] = {
                "revision": accepted.get("revision_number"),
                "revision_digest": accepted.get("content_digest"),
                "trigger": acceptance.get("replan_trigger"),
            }
    for attempt in sorted(_mappings(view.get("node_history")), key=_attempt_key):
        if attempt.get("accepted_graph_revision_digest") == current_revision_digest or (
            not attempt.get("failure_code")
            and attempt.get("status") not in {"failed", "blocked", "cancelled"}
        ):
            continue
        content_digest = attempt.get("content_digest")
        path.append(
            {
                "stage": "historical_task_execution",
                "historical": True,
                "task_id": attempt.get("node_id"),
                "accepted_graph_revision_digest": attempt.get("accepted_graph_revision_digest"),
                "generation": attempt.get("generation"),
                "attempt": attempt.get("attempt"),
                "outcome": attempt.get("status"),
                "reason_code": attempt.get("failure_code"),
                "record_digest": content_digest,
                "triggered_replan": replan_by_evidence.get(str(content_digest)),
            }
        )
    for item in _mappings(view.get("loop_transitions")):
        if item.get("node_id") is None and item.get("action") != "PASS":
            path.append({"stage": "graph_control", **_loop_summary(item)})
    for item in _mappings(view.get("stale_results")):
        path.append(
            {
                "stage": "stale_result_ignored",
                "task_id": item.get("node_id"),
                "result_generation": item.get("result_generation"),
                "authoritative_generation": item.get("authoritative_generation"),
                "worker_result_digest": item.get("worker_result_digest"),
            }
        )
    promotion_approval = _promotion_approval_story(view)
    if promotion_approval is not None and promotion_approval.get("decision") in {
        "denied",
        "expired",
    }:
        path.append(
            {
                "stage": "promotion_approval",
                "outcome": promotion_approval.get("decision"),
                "approval_id": promotion_approval.get("approval_id"),
                "request_digest": promotion_approval.get("request_digest"),
                "reason_code": "PROMOTION_APPROVAL_DENIED_OR_EXPIRED",
            }
        )
    run = _mapping(view.get("run"))
    if run.get("failure_code"):
        path.append(
            {
                "stage": "run",
                "outcome": view.get("state"),
                "reason_code": run.get("failure_code"),
            }
        )
    return path


def _graph_final_outcome(
    view: dict[str, Any], promotion_approval: dict[str, Any] | None
) -> dict[str, Any]:
    status = str(view.get("state", "unknown"))
    revision_digest = _mapping(_mapping(view.get("graph_acceptance")).get("accepted_revision")).get(
        "content_digest"
    )
    goal_evaluations = [
        item
        for item in _mappings(view.get("parent_goal_evaluations"))
        if item.get("accepted_graph_revision_digest") == revision_digest
    ]
    semantic_decisions = [
        item
        for item in _mappings(_mapping(view.get("parent_semantic_review")).get("decisions"))
        if item.get("accepted_graph_revision_digest") == revision_digest
    ]
    goal_evaluations.sort(key=_attempt_key)
    semantic_decisions.sort(key=_attempt_key)
    plan_attempts = _mappings(_mapping(view.get("plan_review")).get("attempts"))
    run_failure_code = view.get("failure_code") or _mapping(view.get("run")).get("failure_code")
    if run_failure_code is None and plan_attempts:
        run_failure_code = plan_attempts[-1].get("failure_code") or (
            "PLAN_REVIEW_BLOCKED" if plan_attempts[-1].get("action") == "reject" else None
        )
    return {
        "status": status,
        "disposition": _graph_disposition(status, promotion_approval),
        "goal_evaluation": None
        if not goal_evaluations
        else {
            "decision": goal_evaluations[-1].get("decision"),
            "evidence_digests": goal_evaluations[-1].get("evidence_digests", []),
        },
        "parent_semantic_review": None
        if not semantic_decisions
        else {
            "action": semantic_decisions[-1].get("action"),
            "reason_code": semantic_decisions[-1].get("reason_code"),
            "accepted_finding_digests": semantic_decisions[-1].get("accepted_finding_digests", []),
        },
        "promotion_recorded": bool(_mappings(view.get("promotions"))),
        "promotion_approval": promotion_approval,
        "failure_code": run_failure_code,
        "next_action": _graph_next_action(status, promotion_approval),
    }


def _promotion_approval_story(view: dict[str, Any]) -> dict[str, Any] | None:
    run = _mapping(view.get("run"))
    approval_id = run.get("promotion_approval_id")
    request_digest = run.get("promotion_approval_request_digest")
    if approval_id is None and request_digest is None:
        return None
    if not isinstance(approval_id, str) or not isinstance(request_digest, str):
        return {"binding": "invalid", "decision": "unknown"}
    policy_digest = run.get("effective_policy_digest")
    approvals = [item for item in _mappings(view.get("approvals")) if item.get("id") == approval_id]
    requests = [
        item
        for item in _mappings(view.get("approval_requests"))
        if item.get("request_digest") == request_digest
        and item.get("policy_digest") == policy_digest
        and "promotion" in item.get("approval_classes", [])
    ]
    approval = approvals[-1] if approvals else None
    if (
        approval is None
        or len(requests) != 1
        or approval.get("request_digest") != request_digest
        or approval.get("policy_digest") != policy_digest
        or request_digest not in approval.get("scope", [])
    ):
        return {
            "binding": "invalid",
            "decision": "unknown",
            "approval_id": approval_id,
            "request_digest": request_digest,
        }
    request = requests[0]
    authorization_kind = approval.get("authorization_kind", "manual")
    authority = None
    if authorization_kind == "policy_auto":
        authority_digest = approval.get("authorization_digest")
        authorities = [
            item
            for item in _mappings(view.get("promotion_policy_decisions"))
            if item.get("content_digest") == authority_digest
        ]
        authority = authorities[0] if len(authorities) == 1 else None
        if (
            authority is None
            or authority.get("decision") != "policy_auto_approved"
            or authority.get("candidate_digest") != request_digest
            or authority.get("effective_policy_digest") != policy_digest
            or authority.get("accepted_graph_revision_digest")
            != run.get("accepted_graph_revision_digest")
            or authority.get("harness_digest") != run.get("harness_digest")
            or authority.get("operator_config_digest") != run.get("operator_config_digest")
            or authority.get("parent_evaluation_digest") != run.get("parent_evaluation_digest")
        ):
            return {
                "binding": "invalid",
                "decision": "unknown",
                "approval_id": approval_id,
                "request_digest": request_digest,
                "authorization_kind": authorization_kind,
            }
    return {
        "binding": "bound",
        "decision": approval.get("decision"),
        "approval_id": approval_id,
        "approval_request_id": request.get("id"),
        "request_digest": request_digest,
        "policy_digest": policy_digest,
        "authorization_kind": authorization_kind,
        "rule_id": approval.get("rule_id"),
        "reason_code": approval.get("reason_code"),
        "authorization_digest": approval.get("authorization_digest"),
        "authority": authority,
        "expires_at": approval.get("expires_at"),
        "decided_at": approval.get("decided_at"),
    }


def _graph_next_action(status: str, approval: dict[str, Any] | None) -> str | None:
    if status != "ready_to_promote":
        return _next_action(status)
    decision = None if approval is None else approval.get("decision")
    if decision == "pending":
        return "approve or deny the exact pending promotion request"
    if decision == "approved":
        return "explicitly promote the approved exact candidate patch"
    if decision in {"denied", "expired"}:
        return "obtain a fresh digest-bound promotion approval before promotion"
    return "inspect the missing or stale promotion approval binding"


def _graph_disposition(status: str, approval: dict[str, Any] | None) -> str:
    if status != "ready_to_promote":
        return _disposition(status)
    decision = None if approval is None else approval.get("decision")
    if decision == "pending":
        return "accepted_awaiting_approval"
    if decision == "approved":
        return "accepted_awaiting_promotion"
    if decision in {"denied", "expired"}:
        return "promotion_blocked_or_incomplete"
    return "indeterminate"


def _planner_routing_story(value: object) -> dict[str, Any] | None:
    routing = _mapping(value)
    if not routing:
        return None
    selected = _mapping(routing.get("selected_strategy"))
    assessment = _mapping(routing.get("assessment"))
    return {
        "selected_strategy": _strategy_summary(selected),
        "eligible_strategy_ids": routing.get("eligible_strategy_ids", []),
        "assessment_reasons": assessment.get("reasons", []),
        "selection_reasons": selected.get("routing_reasons", []),
    }


def _plan_review_story(value: object) -> dict[str, Any]:
    review = _mapping(value)
    return {
        "status": review.get("status", "not_configured"),
        "attempts": [
            {
                "round": item.get("review_round"),
                "outcome": item.get("outcome"),
                "action": item.get("action"),
                "failure_code": item.get("failure_code"),
                "findings": item.get("findings", []),
            }
            for item in _mappings(review.get("attempts"))
        ],
    }


def _explain_work_run(view: dict[str, Any]) -> dict[str, Any]:
    run = _mapping(view.get("run"))
    status = str(view.get("state", "unknown"))
    events = _mappings(view.get("events"))
    routing = _mapping(view.get("routing"))
    selected = _mapping(routing.get("selected_strategy"))
    decisions = _mappings(_mapping(view.get("policy")).get("decisions"))
    approvals = _mappings(view.get("approvals"))
    verification = _mappings(view.get("verification"))
    acceptances = _mappings(view.get("acceptance"))
    failure_path = [
        {
            "stage": "event",
            "sequence": item.get("sequence"),
            "kind": item.get("kind"),
        }
        for item in events
        if "fail" in str(item.get("kind", ""))
        or "deny" in str(item.get("kind", ""))
        or "cancel" in str(item.get("kind", ""))
    ]
    failure_path.extend(
        {
            "stage": "policy",
            "outcome": item.get("outcome"),
            "reason_code": item.get("reason_code"),
            "request_digest": item.get("request_digest"),
        }
        for item in decisions
        if item.get("outcome") != "allow"
    )
    failure_path.extend(
        {
            "stage": "approval",
            "outcome": item.get("decision"),
            "request_digest": item.get("request_digest"),
        }
        for item in approvals
        if item.get("decision") in {"denied", "expired"}
    )
    failure_path.extend(
        {
            "stage": "verification",
            "outcome": item.get("status"),
            "reason_code": _mapping(item.get("failure")).get("code"),
        }
        for item in verification
        if item.get("status") != "succeeded"
    )
    if run.get("failure_code"):
        failure_path.append({"stage": "run", "reason_code": run.get("failure_code")})
    work_position = _work_position(status)
    node_id = run.get("node_id")
    return {
        "schema_version": "2",
        "kind": "run_explanation",
        "source_kind": "work_run",
        "run_id": view.get("run_id"),
        "observation": _observation_contract(),
        "goal": {
            "statement": run.get("goal"),
            "completion_criteria": run.get("completion_criteria", []),
        },
        "current_state": {
            "status": status,
            "generation": view.get("generation"),
            "task_counts": {} if node_id is None else {work_position: 1},
            "active_task_ids": [node_id] if node_id and work_position == "active" else [],
            "ready_task_ids": [node_id] if node_id and work_position == "ready" else [],
            "waiting_task_ids": [node_id] if node_id and work_position == "waiting" else [],
            "blocked_task_ids": [node_id] if node_id and work_position == "blocked" else [],
            "failed_task_ids": [node_id] if node_id and work_position == "failed" else [],
            "next_action": _next_action(status),
        },
        "graph": {
            "digest": run.get("accepted_graph_digest"),
            "tasks": []
            if run.get("node_id") is None
            else [{"id": run.get("node_id"), "execution_state": status}],
            "evolution": [],
        },
        "plan_decisions": {
            "routing": {
                "selected_strategy": _strategy_summary(selected),
                "assessment_reasons": _mapping(routing.get("assessment")).get("reasons", []),
                "selection_reasons": selected.get("routing_reasons", []),
            }
        },
        "task_stories": [
            {
                "task_id": run.get("node_id"),
                "state": status,
                "information_flow": {
                    "goal": run.get("goal"),
                    "worker_request_digest": run.get("worker_request_digest"),
                    "artifact_bodies_included": False,
                },
                "policy_decisions": [
                    {
                        "outcome": item.get("outcome"),
                        "reason_code": item.get("reason_code"),
                        "request_digest": item.get("request_digest"),
                    }
                    for item in decisions
                ],
                "approvals": [
                    {
                        "decision": item.get("decision"),
                        "request_digest": item.get("request_digest"),
                    }
                    for item in approvals
                ],
                "verification": [
                    {
                        "status": item.get("status"),
                        "failure": _stable_failure_summary(item.get("failure")),
                    }
                    for item in verification
                ],
            }
        ],
        "timeline": [
            {
                "sequence": item.get("sequence"),
                "created_at": item.get("created_at"),
                "actor": item.get("actor"),
                "kind": item.get("kind"),
                "request_digest": item.get("request_digest"),
                "result_digest": item.get("result_digest"),
            }
            for item in events
        ],
        "failure_path": failure_path,
        "final_outcome": {
            "status": status,
            "disposition": _disposition(status),
            "acceptance_criteria": [] if not acceptances else acceptances[-1].get("criteria", []),
            "promotion_recorded": bool(_mappings(view.get("promotions"))),
            "failure_code": run.get("failure_code"),
            "next_action": _next_action(status),
        },
    }


def _explain_legacy_run(view: dict[str, Any]) -> dict[str, Any]:
    graph = _mapping(view.get("graph"))
    goal = _mapping(view.get("goal"))
    nodes = _mappings(graph.get("nodes"))
    status = str(view.get("state", "unknown"))
    counts = Counter(str(item.get("state", "pending")) for item in nodes)
    failures = [
        {
            "stage": "transition",
            "entity_id": item.get("entity_id"),
            "outcome": item.get("to_state"),
            "reason": _mapping(item.get("provenance")).get("cause"),
        }
        for item in _mappings(view.get("node_transitions"))
        if item.get("to_state") in {"failed", "blocked", "cancelled"}
    ]
    return {
        "schema_version": "2",
        "kind": "run_explanation",
        "source_kind": "legacy_run",
        "run_id": view.get("run_id"),
        "observation": _observation_contract(),
        "goal": {
            "id": goal.get("id"),
            "statement": goal.get("statement"),
            "completion_criteria": goal.get("completion_criteria", []),
        },
        "current_state": {
            "status": status,
            "generation": view.get("generation"),
            "task_counts": dict(sorted(counts.items())),
            "active_task_ids": [item.get("id") for item in nodes if item.get("state") == "running"],
            "ready_task_ids": [],
            "waiting_task_ids": [
                item.get("id") for item in nodes if item.get("state") == "pending"
            ],
            "blocked_task_ids": [
                item.get("id") for item in nodes if item.get("state") == "blocked"
            ],
            "failed_task_ids": [item.get("id") for item in nodes if item.get("state") == "failed"],
            "next_action": _next_action(status),
        },
        "graph": {
            "revision": graph.get("revision"),
            "digest": graph.get("digest"),
            "tasks": nodes,
            "evolution": [],
        },
        "task_stories": [
            {
                "task_id": item.get("id"),
                "state": item.get("state"),
                "information_flow": {"artifact_bodies_included": False},
            }
            for item in nodes
        ],
        "failure_path": failures,
        "final_outcome": {
            "status": status,
            "disposition": _disposition(status),
            "next_action": _next_action(status),
        },
    }


def _latest_by_node(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in records:
        node_id = str(item.get("node_id"))
        if node_id not in latest or _attempt_key(item) > _attempt_key(latest[node_id]):
            latest[node_id] = item
    return latest


def _latest_matching(
    records: list[dict[str, Any]], node_id: str, revision_digest: str | None
) -> dict[str, Any] | None:
    matches = [
        item
        for item in records
        if item.get("node_id") == node_id
        and item.get("accepted_graph_revision_digest") == revision_digest
    ]
    return None if not matches else max(matches, key=_attempt_key)


def _attempt_key(item: dict[str, Any]) -> tuple[int, int, int, str, str]:
    return (
        int(item.get("generation", 0)),
        int(item.get("attempt", 0)),
        int(item.get("sequence", 0)),
        str(item.get("created_at", "")),
        str(item.get("id", "")),
    )


def _loop_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": item.get("generation"),
        "attempt": item.get("attempt"),
        "action": item.get("action"),
        "reason_code": item.get("reason_code"),
        "evidence_digests": item.get("evidence_digests", []),
        "next_graph_revision_digest": item.get("next_graph_revision_digest"),
        "bound_consumed": item.get("consumed"),
        "bound_limit": item.get("limit"),
    }


def _strategy_summary(strategy: dict[str, Any]) -> dict[str, Any] | None:
    if not strategy:
        return None
    return {
        "id": strategy.get("id"),
        "backend": strategy.get("backend"),
        "model": strategy.get("model"),
        "effort": strategy.get("effort"),
    }


def _ids_at(positions: dict[str, str], expected: str) -> list[str]:
    return [node_id for node_id, value in positions.items() if value == expected]


def _disposition(status: str) -> str:
    if status in {"completed", "succeeded"}:
        return "accepted"
    if status == "ready_to_promote":
        return "accepted_awaiting_promotion"
    if status in {"failed", "cancelled"}:
        return "rejected_or_incomplete"
    return "incomplete"


def _work_position(status: str) -> str:
    if status in {"running", "verifying", "reviewing", "promoting"}:
        return "active"
    if status in {"planning", "waiting_approval", "paused"}:
        return "waiting"
    if status == "planned":
        return "ready"
    if status in {"ready_to_promote", "completed"}:
        return "completed"
    if status == "failed":
        return "failed"
    return "blocked"


def _next_action(status: str) -> str | None:
    return {
        "paused": "resume the persisted run",
        "waiting_approval": "approve or deny the exact pending request",
        "ready_to_promote": "inspect the accepted patch and explicitly promote it",
        "failed": "inspect the failure path and stable reason codes",
        "planned": "start or resume execution",
        "planning": "wait for planning to complete",
        "running": "allow bounded execution to continue",
        "verifying": "allow deterministic verification to complete",
        "reviewing": "allow the configured review gate to complete",
    }.get(status)


def _observation_contract() -> dict[str, Any]:
    return {
        "source": "persisted_facts",
        "read_only": True,
        "ai_invocations": 0,
        "artifact_bodies_read": False,
    }


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _stable_failure_summary(value: object) -> dict[str, Any] | None:
    failure = _mapping(value)
    if not failure:
        return None
    return {
        "code": failure.get("code"),
        "retryable": failure.get("retryable", False),
    }
