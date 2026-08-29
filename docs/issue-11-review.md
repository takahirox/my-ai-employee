# Issue #11 review: fresh minimal worker context

## Decision

Each accepted graph-node attempt continues to receive a fresh `WorkerRequest`; Fleet does not
resume or attach conversation history. A separate immutable `WorkerContextManifest` records the
exact request digest, accepted graph revision, generation and attempt, workspace paths, remaining
budgets, predecessor result and evidence digests, and body-free artifact descriptors. The manifest
is derived deterministically from authoritative persisted records and has no execution authority.

The runtime persists the request and manifest before dispatch, reloads the exact manifest, and
checks every request, node, graph revision, generation, attempt, predecessor result/evidence, path,
budget, and artifact-descriptor binding. Missing, stale, ambiguous, or mismatched mandatory context
fails before the worker is invoked with the single stable code `CONTEXT_INSUFFICIENT`.

## Context boundary

The worker prompt contains only the node goal, accepted-plan digest, declared workspace paths,
Harness and effective-policy digests, remaining budgets, predecessor result bindings, body-free
artifact descriptors, and the strict response contract. Goal text, predecessor results, evidence
bindings, and descriptor metadata are explicitly untrusted data. No prior worker, Planner,
Reviewer, Evaluator, Inspector, or operator conversation is supplied.

Artifact bodies are not embedded in either the manifest or prompt. Existing controlled read paths
remain the only on-demand access mechanism and remain subject to the request's existing artifact
and process budgets. This change adds no retrieval, repair, summarization, ranking, cache,
truncation, secret-scanning, or generic context API.

## Unchanged model boundaries

The fresh, tool-disabled model boundaries remain unchanged:

- `fleet-proposed-graph/2` gives a Planner the accepted Goal, explicit bounds, available
  capabilities, and strict schema in a new ephemeral process.
- `fleet-plan-review/2` gives a Reviewer only the accepted Goal, proposed graph, fixed rubric,
  capabilities, digests, bounds, and strict schema in a new ephemeral process.
- `fleet-semantic-task-assessment/2` gives the semantic classifier only untrusted goal text, the
  fixed categorical rubric, context size, and strict schema, with no tools.
- First-party Harness evaluation remains deterministic mediated code and creates no AI prompt or
  conversation.

Planner, Reviewer, and semantic-classifier outputs remain non-authoritative. Deterministic graph
acceptance, routing, policy, evidence evaluation, composition, review, and promotion boundaries are
unchanged.
