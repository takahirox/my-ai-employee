"""Shared meanings for consequential stage values, not a second orchestration layer.

Models own local validity; StageContract binds runtime references; Engine owns effects.
These definitions are read by all three and projected only into relevant stages.
"""

from __future__ import annotations

from typing import Any

from .source_refs import SOURCE_REFERENCE_RULE

INPUT_PRESERVATION = (
    "For an explicit requirement to preserve input files, declare preserved_paths on the "
    "criterion, not only in prose. Paths are canonical workspace-relative file or directory "
    "names, without globs; '.' selects the entire workspace. A directory includes all its "
    "descendants and detects additions as well as changes/deletions. Each selected path must "
    "exist in the Run's initial snapshot. Fleet compares content and executable flags against "
    "that immutable snapshot and supplies runtime-owned input_comparison to verification "
    "and its review. This proves snapshot equality only, never that no transient write "
    "occurred. Do not create a Worker baseline or seek .fleet-inputs/Git for this evidence. "
    "Keep the Goal's preserved_paths on the Plan's result Task, including recovery. "
    "Leave preserved_paths empty for criteria without this requirement."
)

# Successful boundary events, also consumed at Engine acceptance/publication boundaries.
LIFECYCLE: dict[str, dict[str, Any]] = {
    "task_verification": {"goal_level": False, "postcondition": "accepted"},
    "goal_verification": {"goal_level": True, "postcondition": "completed"},
    "promotion": {"precondition": "completed", "postcondition": "promoted"},
}
# A method chosen by the agent is not a new user requirement. This same contract
# is used in field schemas, proposal review, execution, and recovery.
METHOD_SELECTION = {
    "requirements": "Separate the user's desired outcomes and explicit constraints from "
    "agent-selected execution methods. Preserve every explicitly permitted alternative in "
    "clarified_goal and its criteria. A choice of one method must not erase or prohibit "
    "another authorized method. If the user requires or forbids a method, preserve that "
    "constraint; permission to choose is not permission to weaken the outcome.",
    "assumptions": "Record factual uncertainties and interpretations only. An agent's selected "
    "execution route belongs in the Plan, not an assumption that changes acceptance. Merely "
    "disclosing a choice here does not authorize narrowing the user's alternatives. "
    "Never hide a necessary user decision or waive requirements in assumptions.",
    "planning": "Choose execution methods in Task descriptions and verification plans using "
    "the unchanged Goal's alternatives. Check known authority/environment constraints before "
    "committing to a route. A ceiling is not a grant or proof of reachability; unknown "
    "feasibility remains an assumption. Do not expand authority or perform unnecessary "
    "external actions just to probe feasibility. Goal criteria describe success; Task "
    "criteria may specialize the chosen authorized route without making it mandatory for "
    "all future Plans.",
    "review": "Compare Original Input, clarified_goal, criteria, assumptions and downstream "
    "outcomes. Reject unsupported narrowing as unsupported_expansion: an agent-selected "
    "route must not become a user requirement, including by omission of an authorized "
    "alternative. Also reject removal of a user-required method or use of a forbidden one. "
    "Review plans against the preserved alternatives, not a previous agent preference.",
    "verification": "Assess the route actually represented by the Candidate against the "
    "Goal's authorized alternatives and this Task's contract. For explicitly allowed "
    "post-handoff execution, verify the executable deliverable and execution instructions "
    "now, and retain the conditional downstream outcome for the designated actor. Do not "
    "require results that actor can create only after handoff, or claim they already exist. "
    "A declaration alone is not sufficient evidence that the deliverable meets the request.",
    "recovery": "A failed execution method is not a failed user requirement. When an observed "
    "environment/authority constraint defeats a route, reconsider the Goal's other permitted "
    "methods instead of repeatedly asking for the same infeasible operation. Use existing "
    "Task replacement/replanning and preserve the accepted Goal, history and safety bounds. "
    "Never relax a genuine user restriction or relabel uncertain effects as safe to replay.",
}

COMPLETION = {
    "scope": "Preserve the user's final desired outcome in clarified_goal. criteria are only "
    "Fleet-owned conditions independently verifiable before completion and promotion. "
    "Task verification precedes Task acceptance; Goal verification precedes completion; "
    "promotion requires completed, verified bytes. Neither verification runs after handoff. "
    + METHOD_SELECTION["requirements"],
    "downstream": "Only when Original Input explicitly allows execution after handoff, retain "
    "that outcome, its external owner and original authorization in downstream_outcomes. "
    "Link each to artifact criteria that verify the executable deliverable and execution "
    "instructions before handoff. Preserve all downstream requirements in those deliverables. "
    "Results first created by the external owner after promotion are not immediate Task/Goal "
    "evidence and Fleet must not claim they occurred. Never move a requirement for Fleet itself "
    "to execute or complete an external operation into downstream_outcomes. If infeasible, "
    "report the blocker through clarification; do not silently substitute artifact delivery. "
    "When handoff is one of multiple explicitly allowed routes, retain its conditional "
    "downstream outcome even before planning chooses a route. Express immediate Goal "
    "criteria as the authorized alternatives (direct result OR verified handoff), not "
    "requirements to perform both. An agent preference for direct execution does not "
    "remove the user's handoff authorization.",
    "review": "Check original authorization, responsible actor, timing and feasible evidence "
    "for the whole Goal and Plan. Reject circular conditions requiring post-handoff results "
    "before promotion, lost downstream requirements, or weakened direct-execution requests. "
    "During verification inspect actual deliverables against linked downstream requirements, "
    "without requiring their external execution or attesting unperformed effects. "
    + METHOD_SELECTION["review"],
}
# A stop is a report, never admission to execution. Other validation still applies.
EXECUTION_CHECKS: dict[str, Any] = {
    "required_by_disposition": {"proceed": True, "wait": True, "repair": True, "stop": False},
    "rule": "Mandatory-check coverage and protected external-evidence availability are "
    "execution-admission conditions. A clarification with disposition=stop may report their "
    "absence without inventing checks. Source references, declared check IDs, criterion "
    "mappings and all other structural rules still apply. Review the stop's grounds; an "
    "approved stop cannot admit a Goal or launch work. Other dispositions retain these checks.",
}


def lifecycle_context(stage: str, prompt: dict[str, Any]) -> dict[str, Any]:
    """Inventory the actual invocation, including review and recovery evidence.

    Presence means supplied input, never proof of inspection or external completion.
    No guessed global pre/post-work phase and no second workflow scheduler.
    """
    source = prompt.get("original", prompt) if stage.endswith("_review") else prompt
    inputs = {
        key
        for key, value in source.items()
        if value is not None and key not in {"instruction", "feedback", "review_feedback"}
    }
    inputs.update(
        key
        for key in ("input_tree", "input_snapshot", "proposal", "source_evidence")
        if key in prompt
    )
    if isinstance(source.get("context"), dict):
        inputs.update("context." + key for key in source["context"])
    target = stage.removesuffix("_review")
    return {
        "boundaries": LIFECYCLE,
        "available_inputs": sorted(inputs),
        "snapshot_tree": prompt.get("input_tree"),
        "assessment": (
            "actual_candidate_evidence"
            if target in {"worker", "verification"}
            else "proposal_feasibility"
        )
        if stage.endswith("_review")
        else None,
        "verification_scope": source.get("verification_scope"),
        "review_target": stage.removesuffix("_review") if stage.endswith("_review") else None,
        "evidence_rule": "Postcondition events require accepted verification and applicable "
        "reviews/checks; failed proposals use existing bounded repair/recovery. "
        "Available inputs are bound context, not inspected facts. Use the "
        "actual supplied Candidate, worker result, accepted upstream receipts and snapshot; "
        "do not assume a repair/recovery invocation has no previous work. A proposal review "
        "assesses feasibility; a Candidate review assesses available actual evidence. "
        "Results requiring this Run's future promotion are not available at any model stage.",
    }


OUTCOMES: dict[str, dict[str, Any]] = {
    "artifact": {
        "external_evidence": False,
        "rule": "Success is a property of the immutable Candidate. Empty optional checks are "
        "valid: independent verification inspects actual files after work. Do not require "
        "an operator check merely to make artifact work verifiable.",
    },
    "external_effect": {
        "external_evidence": True,
        "rule": "Success requires an external effect, not a local script or Worker claim. "
        "For execution admission, an explicitly linked operator external_effect check is "
        "required (see clarification execution_checks for stop reports); planning and "
        "repair must preserve the accepted Goal's external outcomes and check coverage.",
    },
}


def external_evidence(kind: str | None) -> bool:
    return OUTCOMES.get(kind or "", {}).get("external_evidence") is True


EVIDENCE = {
    "claims": "Model summaries, reasons, assumptions and feedback are proposals or diagnostic "
    "claims. They cannot change Original Input, policy, accepted definitions or runtime "
    "identity. Review them against the bound original; never treat them as authority.",
    "checks": "IDs of immutable operator-owned RunConfig.checks, not tests a Worker promises "
    "to add. Empty is allowed for artifact criteria unless a mandatory check is required. "
    "Only the operator can configure a missing protected check; never invent its ID.",
    "mandatory_checks": "Operator-owned checks required for Goal acceptance. They cannot be "
    "replaced by Worker-authored tests or AI review. " + EXECUTION_CHECKS["rule"],
    "required_evidence": "Evidence available before promotion that the Worker must produce or "
    "locate and the independent verifier must assess after work. These strings are requirements, "
    "not "
    "receipts, command definitions or proof that the work already happened.",
    "verification_plan": "Proposed method for independent verification after work; not an "
    "executed check or observed result. It may include inspecting artifacts or Worker tests.",
    "worker_evidence": "Untrusted Worker claims and pointers to inspect. Empty does not by "
    "itself fail artifact work; real evidence must be inspected by independent verification. "
    "Claims never grant authority or attest external completion.",
    "proposal_review": "Use the bound lifecycle context to assess the proposal. Before work, "
    "assess preservation of requirements and a feasible "
    "evidence route. Do not demand future artifacts, tests or passing receipts already exist. "
    "Apply the shared clarification execution_checks conditions to missing mandatory/external "
    "checks; empty optional artifact checks are valid. "
    "After work, assess actual Candidate evidence; a plan or claim is not proof.",
    "verification": "Inspect each exact criterion against the Candidate and required evidence. "
    "Protected checks prove only their named coverage and all executed checks must pass. "
    "External outcomes require protected external receipts; never repeat a side effect to "
    "verify it. Only accepted upstream receipts may contribute at Goal verification.",
}
WORKER_STATES: dict[str, dict[str, Any]] = {
    "completed": {
        "action": "verify",
        "external_action": "verify",
        "request": False,
        "rule": "Work is ready for Candidate freeze, review and independent verification. "
        "This is not acceptance or publication and does not prove the outcome.",
    },
    "failed": {
        "action": "retry",
        "external_action": "uncertain",
        "request": False,
        "rule": "Ordinary local failure may retry/escalate within existing budgets. "
        "With external-write authority it is uncertain and must not repeat the effect.",
    },
    "authority_requested": {
        "action": "request",
        "external_action": "request",
        "request": True,
        "rule": "Provide a complete authority_request within the Run ceiling. This proposes "
        "a grant and pauses; only explicit approval followed by native application grants it.",
    },
    "uncertain": {
        "action": "uncertain",
        "external_action": "uncertain",
        "request": False,
        "rule": "External effects may have occurred; record durable uncertainty and wait for "
        "reconciliation. This is not a generic signal for local difficulty or missing facts.",
    },
    "usage_limit": {
        "action": "stop",
        "external_action": "stop",
        "request": False,
        "rule": "Report provider allowance exhaustion. Stop the Run; no retry, escalation, "
        "provider switch, purchase or quota reset. A claim cannot authorize more usage.",
    },
}
FINDING_CATEGORIES: dict[str, dict[str, Any]] = {
    "satisfied": {"passed": True, "rule": "The assessed criterion is supported by evidence."},
    "missing_evidence": {"passed": False, "rule": "Required evidence at this stage is absent."},
    "omitted_requirement": {"passed": False, "rule": "An original requirement was omitted."},
    "weakened_requirement": {"passed": False, "rule": "An original requirement was weakened."},
    "unsupported_expansion": {"passed": False, "rule": "Unrequested scope was introduced."},
    "incorrect_result": {"passed": False, "rule": "Observed result violates the criterion."},
    "unspecified": {"passed": None, "rule": "No diagnostic category; passed still decides."},
}
FINDINGS = {
    "references": "Return exactly one Finding per expected criterion ID, with no duplicates "
    "or other IDs. Proposal review uses the single ID review; multiple concerns belong in "
    "that Finding's evidence. Candidate verification uses the supplied criterion namespace.",
    "category": "All categories are available in review and verification; interpret evidence "
    "at the stage being assessed. Category diagnoses the failure, not a new recovery action. "
    "Rejection follows existing proposal revision, local retry/replan or external uncertainty.",
    "evidence": "Explain inspected facts supporting the judgment. A Finding is an independent "
    "assessment, not permission or an operator receipt. Acceptance also requires runtime "
    "identity, exact coverage, protected checks and applicable external evidence.",
}
GRAPH = {
    "dependencies": "Each dependency requires an accepted upstream Candidate, not merely "
    "ordering. All dependencies must resolve within this acyclic Plan. Runtime binds exact "
    "upstream Candidate digests and provides immutable inputs to the downstream Worker.",
    "kind": "work creates a result; integration combines upstream results; repair corrects "
    "previous work. These are intent labels with the same scheduling/verification rules, "
    "not privileged execution paths. Integration may occur at any convergence point.",
    "supersedes": "Optional historical Task ID identifying work replaced by this new Task. "
    "It does not rewrite history or automatically rewire dependents. Use new IDs for "
    "replacement/reintegration paths and return the complete adopted Plan.",
    "result_task": "The one Task whose accepted Candidate becomes the final result. Every "
    "Task must be its ancestor or itself; Goal verification is still required before completion.",
    "recovery": "Preserve exact definitions of historical IDs and the accepted Goal. Reuse "
    "accepted upstream work only with exact lineage; do not depend on permanently failed "
    "Tasks. Introduce new IDs for changed work and downstream reintegration. At least one "
    "new Task is required by recovery, within remaining graph-growth/replan limits.",
    "candidate": "Runtime-owned immutable identity: tree, Task digest, attempt, upstream "
    "Candidate digests and applied authority version. Worker output cannot supply or change "
    "these. Acceptance/publication recheck identity, lineage and independent verification. "
    "Only explicit human goal revision may replace Original Input in a linked new Run.",
}
SELECTION = (
    "Select a zero-based index from the supplied operator-configured options; never "
    "invent a model, provider or policy. Selection cannot bypass usage limits or authority."
)

# Stable violation codes and meanings are also used for bounded repair feedback.
RULES: dict[str, dict[str, str]] = {
    "INVALID_PRESERVATION_PATH": {
        "path": "criteria.preserved_paths",
        "rule": INPUT_PRESERVATION,
    },
    "INPUT_PRESERVATION_WEAKENED": {
        "path": "criteria.preserved_paths",
        "rule": INPUT_PRESERVATION,
    },
    "DUPLICATE_AUTHORITY_RESOURCE": {
        "path": "authority.network_hosts/credentials",
        "rule": "List each host and named "
        "credential once; repeated names do not grant additional authority.",
    },
    "AUTHORITY_REQUIRES_HOST_NAMES_NOT_URLS_OR_PORTS": {
        "path": "authority.network_hosts",
        "rule": "Use lowercase host names or the "
        "supported * / *.domain wildcard forms, not URLs, ports, paths or consecutive dots.",
    },
    "WORKER_AUTHORITY_REQUEST_MISMATCH": {
        "path": "authority_request",
        "rule": "authority_request is required exactly when "
        "status is authority_requested, and must be null for every other status.",
    },
    "FINDING_CATEGORY_MISMATCH": {
        "path": "findings.category/passed",
        "rule": "satisfied requires passed=true; failure "
        "categories require passed=false; unspecified permits either value.",
    },
    "INVALID_FINDING_REFERENCES": {"path": "findings", "rule": FINDINGS["references"]},
    "UNDECLARED_VERIFICATION_CHECK": {"path": "criteria.checks", "rule": EVIDENCE["checks"]},
    "MANDATORY_CHECK_OMITTED": {"path": "criteria.checks", "rule": EVIDENCE["mandatory_checks"]},
    "MISSING_EXTERNAL_EVIDENCE_CHECK": {
        "path": "criteria.checks",
        "rule": OUTCOMES["external_effect"]["rule"],
    },
    "HISTORICAL_TASK_REWRITTEN": {"path": "tasks", "rule": GRAPH["recovery"]},
    "FOREIGN_REPAIR_TARGET": {"path": "tasks.supersedes", "rule": GRAPH["supersedes"]},
    "GRAPH_GROWTH_LIMIT": {"path": "tasks", "rule": GRAPH["recovery"]},
    "FAILED_DEPENDENCY": {"path": "tasks.dependencies", "rule": GRAPH["recovery"]},
    "WORKER_SELECTION_OUT_OF_RANGE": {"path": "index", "rule": SELECTION},
    "INVALID_ORIGINAL_REFERENCES": {"rule": SOURCE_REFERENCE_RULE},
    "INVALID_DOWNSTREAM_CRITERIA": {
        "path": "downstream_outcomes.criteria",
        "rule": "Each downstream outcome must link unique existing artifact criterion IDs "
        "for Fleet's verified handoff, never external-effect criteria. " + COMPLETION["downstream"],
    },
    "FOREIGN_REQUIREMENT_CRITERION": {
        "path": "requirements.criteria",
        "rule": "Map original fragments only to criterion IDs defined in this clarification.",
    },
    "UNMAPPED_CRITERION": {
        "path": "requirements.criteria",
        "rule": "Every criterion must "
        "map to an original request fragment; do not silently expand or omit the goal.",
    },
    "DUPLICATE_CRITERION": {"path": "criteria", "rule": "Criterion IDs must be unique."},
    "DUPLICATE_TASK_CRITERION": {
        "path": "tasks.criteria",
        "rule": "Criterion IDs within each Task must be unique.",
    },
    "DUPLICATE_DEPENDENCY": {"path": "tasks.dependencies", "rule": "List each dependency once."},
    "INVALID_GRAPH_IDENTITY": {
        "path": "tasks/result_task",
        "rule": "Task IDs must be unique and result_task must identify a Task in this Plan.",
    },
    "CYCLIC_OR_MISSING_DEPENDENCY": {"path": "tasks.dependencies", "rule": GRAPH["dependencies"]},
    "UNCONNECTED_RESULT": {"path": "result_task", "rule": GRAPH["result_task"]},
}


def projection(stage: str) -> dict[str, Any]:
    """No model call or classifier: select meanings by the existing stage identity."""
    result: dict[str, Any] = {
        "evidence": EVIDENCE,
        "outcomes": OUTCOMES,
        "completion": COMPLETION,
        "method_selection": METHOD_SELECTION,
    }
    if stage not in {"clarification", "clarification_review"}:
        result["graph"] = GRAPH
    if stage in {"worker", "worker_review", "task_verification", "goal_verification"}:
        result["worker_states"] = WORKER_STATES
    if stage.endswith("_review") or stage.endswith("_verification"):
        result.update(findings=FINDINGS, finding_categories=FINDING_CATEGORIES)
    if stage in {"selection", "selection_review"}:
        result["selection"] = SELECTION
    return result
