# Issue #12 review: bounded semantic plan review

## Decision

The smallest closeable first milestone is one optional, pre-acceptance semantic gate for strict
ProposedGraph inputs. A fresh, tool-disabled reviewer emits bounded typed findings. Deterministic
code validates those findings and chooses exactly one of three actions: continue to graph
acceptance, request one Planner revision, or stop. The reviewer never returns an acceptance bit,
a graph, a capability, or an execution instruction.

The gate is enabled for the current adaptive CLI path, which is the only path that creates a
ProposedGraph. It is an optional dependency for direct TaskOrchestrator and GraphExecutionService
callers so existing callers remain source-compatible. When configured, it is fail-closed and
cannot be skipped because a reviewer, revision Planner, or parser fails. Production code and tests
start only after this review is accepted.

## Current interfaces and constraints

| Current interface | Consequence for this milestone |
| --- | --- |
| task_planning.ProposedGraph and CliProposedGraphPlanner | Planner output is already strict, digest-bound to the exact Goal, run, strategy, effective policy, and Harness, capability-checked, and explicitly non-authoritative. The CLI adapter already runs Codex read-only and ephemeral, Claude with an empty tool set and no session persistence, or Ollama without Fleet tools through a ProcessRequest and PolicyDecision. Reuse this pattern for review and revision. |
| graph.validate_task_graph() and accept_task_graph() | The Trust Kernel already checks the bounded dependency-DAG subset, capabilities, policy limits, budgets, entries, terminals, objectives, and criteria. Semantic review must be surrounded by these checks, never replace or weaken them. |
| TaskOrchestrator.run() | A ProposedGraph currently receives provenance checks, is accepted, persisted, and then scheduled. A plain Graph bypasses planner provenance. The review gate belongs after ProposedGraph provenance plus preliminary deterministic validation and before the one authoritative accept_task_graph() result is persisted or any NodeExecutionRecord can run. |
| Existing revisioned replan | Runtime replan is only authorized after an accepted failed or paused run, requires authoritative prior-revision evidence, increments AcceptedGraphRevision and run generation, and consumes max_replans. A pre-execution review correction is not that operation and must not reuse or relax its authority. |
| routing.select_strategy() and operator configuration | Fixed, policy, and adaptive selection first enforce configured IDs, backends, locality, capabilities, risk, and fit. The current adaptive CLI resolves one assessment strategy and uses it for graph planning. No reviewer role exists in operator configuration. |
| Policy-mediated process execution | AI subprocesses are represented by bounded ProcessRequest records and exact PolicyDecision bindings. Policy denial, approval requirements, timeout, non-zero status, missing output, and strict parse failure already have fail-closed meanings. |
| SQLiteStore | The generic versioned records table can store new strict v2 record kinds without a table migration. Existing digested records cannot safely gain defaulted fields because doing so would change the digest recomputed for old JSON. |
| TaskGraphAcceptance, GraphReplay, and Inspector | Acceptance already binds the winning proposed_graph_digest. Replay is read-only and reports zero worker, verification, composition, and promotion invocations. Inspector projects persisted JSON facts without opening artifact bodies. New review evidence should join these records by digest rather than change an existing digested shape. |

## Strict reviewer contract

Add review-specific strict v2 models beside the planner boundary. PlanReviewFinding contains only:

- id: an Identifier;
- finding_type: one of missing_goal_coverage, unnecessary_task, scope_expansion,
  premature_generalization, over_fragmentation, under_decomposition, unnecessary_refactor,
  verification_gap, or unclear_goal_traceability;
- impact: blocking or advisory;
- affected_node_ids: zero to sixteen unique existing node IDs, lexicographically sorted; an empty
  tuple is allowed only for a graph-wide finding;
- goal_relation: one non-blank string of at most 1,000 characters explaining the finding against
  the supplied Goal or completion criteria; and
- smallest_correction: one non-blank string of at most 1,000 characters describing the least
  change that resolves it.

PlanReviewPayload contains only schema_version 2 and zero to sixteen findings. Finding IDs are
unique and sorted. All properties are required, additional properties are forbidden, and unknown
enums, unknown node IDs, duplicate or unsorted IDs, blank text, excess findings, and excess text
reject the whole payload. There is no essay, numeric score, confidence, free-form verdict,
replacement graph, proposed capability, policy change, or execution field.

The prompt protocol is fleet-plan-review/2. A fresh context receives exactly the accepted Goal,
the exact ProposedGraph graph, the available capabilities, the effective policy digest, the
Harness digest, applicable graph/policy budget ceilings, and the fixed rubric above. It receives
neither repository access nor files, tools, planner conversation, worker results, routing history,
Inspector data, or secrets. It is told that justified breadth explicitly required by the Goal is
not a defect and that minimality cannot remove correctness, safety, compatibility, required error
handling, or verification.

The model-controlled payload is wrapped in a trusted PlanReviewAttempt record containing the run,
round (0 or 1), goal digest, proposed-graph digest, reviewer strategy, effective-policy digest,
Harness digest, outcome, findings, deterministic action, and optional stable failure code. The
wrapper validates all bindings and the decision invariant; the model cannot populate trusted
bindings or the action.

## Deterministic gate and one revision

For a configured gate, TaskOrchestrator performs this sequence before creating a runnable graph:

1. Apply the existing ProposedGraph provenance checks for run, Goal and Goal digest, exact
   configured Planner strategy, non-local restriction, effective policy, and Harness.
2. Call validate_task_graph() with the current ExecutionPolicy and available capabilities. Any
   issue raises the existing GraphValidationError without invoking the reviewer.
3. Persist the valid candidate ProposedGraph, invoke one reviewer through a policy-mediated,
   tool-disabled adapter, strictly parse it, validate every binding and finding, and persist round
   0 PlanReviewAttempt.
4. Derive the action solely from the validated record: no blocking findings means accept;
   blocking findings in round 0 means request_revision; an adapter or contract failure means
   reject. Advisory findings are persisted but never cause revision by themselves.
5. For request_revision, invoke the same already-resolved Planner strategy once in a fresh
   tool-disabled process using fleet-proposed-graph-revision/2. Supply only the exact Goal,
   original proposal, accepted typed findings, current constraints, and the strict ProposedGraph
   response schema. Instruct it to make only corrections needed by the findings. Do not pass
   reviewer conversation or grant tools.
6. Apply the same strict parse, capability canonicalization, provenance checks, and
   validate_task_graph() call to the revision. Persist a PlanRevisionAttempt that digest-links the
   original proposal, triggering review, and revised proposal. There is no fallback to the
   original proposal if this fails.
7. Review the revised proposal once as round 1. No blocking findings means accept; any blocking
   finding means reject. A second revision request is unrepresentable.
8. Immediately recompute accept_task_graph() for the selected proposal. Only that return value may
   populate TaskGraphAcceptance. Persist the acceptance and its review binding before node records
   are made runnable or the scheduler starts.

The deterministic action function is therefore:

| Attempt state | Round 0 | Round 1 |
| --- | --- | --- |
| Completed, no blocking findings | accept | accept |
| Completed, one or more blocking findings | request_revision | reject |
| Invocation, policy, binding, or contract failure | reject | reject |

Finding truth remains a probabilistic signal, but treatment of a validated signal is fixed. A
reviewer cannot make an invalid graph valid, mutate either proposal, select the revised graph,
accept a graph, expand budgets, add capabilities, relax policy, approve an action, execute a node,
or promote a patch. The final graph still traverses the existing routing, reservation, worker,
policy, approval, verification, composition, parent evaluation, and promotion boundaries.

This review correction is pre-acceptance proposal history, not AcceptedGraphRevision history. It
does not increment GraphRunRecord.generation or replan_count, consume graph max_replans, retain
nodes, use execution evidence, or alter the existing post-execution replan method. It is limited
to one additional Planner call and one additional reviewer call, both within the original process
and wall-time policy ceilings.

## Adapter and strategy resolution

Add a narrow CliPlanReviewer next to CliProposedGraphPlanner and reuse its executor, output reader,
policy decider, prompt artifact writer, backend allowlist, bounded stdout/stderr, cancellation
behavior, and backend-specific tool-disabled argv. Do not introduce a generic agent framework or a
new process service. Persist the exact ProcessRequest, PolicyDecision, ExecutionResult/output
artifact, and PlanReviewAttempt needed to diagnose the gate without storing conversation state.

For the adaptive CLI, use the already resolved assessment_strategy as both the graph Planner and
the reviewer strategy, but invoke a new ephemeral process with a fresh prompt and no session. This
is independence of context and role; a distinct model is not promised. The strategy must still be
configured and Harness/policy eligible. There is no reviewer-driven routing and no fallback to a
different strategy, backend, model, effort, local model, or free-form output. A separate reviewer
configuration field is deferred.

Direct TaskOrchestrator and GraphExecutionService construction may provide the paired reviewer and
revision callbacks or omit both. Supplying only one is a constructor error. When present, their
ExecutionStrategy values must exactly match configured eligible strategies and all review steps
above are mandatory for ProposedGraph inputs.

## Persistence, replay, and Inspector

Use new record kinds rather than changing ProposedGraph or TaskGraphAcceptance:

- plan_review_attempt_v2: one immutable record for each attempted round, including failures and the
  deterministically derived action;
- plan_revision_attempt_v2: zero or one immutable record binding the source proposal, triggering
  review digest, status, failure code, and revised proposal digest when completed; and
- plan_review_acceptance_binding_v2: exactly one record for a reviewed acceptance, binding the
  TaskGraphAcceptance digest, selected ProposedGraph digest, final accepting review digest, and
  optional revision-attempt digest.

Persist each valid proposal before its review, and persist a failed attempt whenever trusted
bindings are available. Existing prompt/output ArtifactDescriptor and process/policy records give
low-level evidence; the review records contain only bounded parsed findings and digests. A crash
may leave a non-authoritative partial attempt, but execution starts only after acceptance plus its
complete binding have been stored. Missing, duplicate, mismatched, cyclic, out-of-order, or
wrong-run bindings fail closed when a reviewed graph is resumed or executed.

GraphReplay gains default-empty review_attempts, revision_attempts, and review_acceptance_binding
fields. Replay loads and digest-validates records, orders attempts by round, checks the chain
against revision_history, and invokes no adapter or service. Inspector adds one plan_review object
under graph-run JSON with status not_configured, accepted, revised, blocked, or failed plus the
bounded records. It does not render a new UI, read artifact bodies, recompute semantic findings, or
show hidden prompts.

Compatibility is explicit:

- Existing databases need no SQL migration. Old records retain their original JSON and content
  digests because no field is added to an existing DigestedRecordV2 model.
- Old graph runs and direct callers with no review records replay and inspect as
  plan_review.status=not_configured; absence is not retroactively treated as corruption and never
  invokes a reviewer.
- Legacy CLI and fixed routing continue to build the existing one-node plain Graph and do not run a
  plan reviewer because no probabilistic ProposedGraph exists.
- Policy routing and hand-authored plain Graph callers retain current deterministic acceptance.
- Existing direct ProposedGraph callers that omit the optional gate retain current behavior. If
  they opt in, both reviewer and one-revision Planner are required and failures are closed.
- Adaptive plan-only performs the full gate and persists an accepted graph but runs no Worker.
  Resume uses the persisted accepted graph and validates an existing review binding; it never
  reviews or replans again.
- Current runtime replan, TaskGraphAcceptance revision ancestry, routing decisions, approvals,
  promotion authority, CLI flags/output keys, and strict old parsers keep their meanings.

## Failure and fallback behavior

A structurally invalid initial proposal fails through GraphValidationError before review. Reviewer
policy denial or approval-required outcome, spawn failure, timeout, cancellation, non-success
status, absent output, malformed JSON, schema violation, excessive findings/text, or stale binding
persists a failed attempt when possible and terminates with stable code PLAN_REVIEW_FAILED. No
Planner revision or Worker runs.

Round-0 blocking findings cause the sole revision. Revision invocation failure, invalid strict
output, unsupported capability, stale provenance, changed Goal/policy/Harness, or invalid DAG
terminates with GRAPH_PLANNER_FAILED and never falls back to the original. Reviewer failure on the
revision terminates with PLAN_REVIEW_FAILED. Remaining round-1 blockers terminate with
PLAN_REVIEW_BLOCKED. This milestone chooses fail-closed because no existing policy authorizes an
operator to waive semantic plan findings. Cancellation remains cancellation. None of these cases
falls back to unreviewed acceptance, another model, a plain Graph, or execution.

## Focused offline acceptance tests

Production implementation is acceptable when deterministic tests prove:

1. The reviewer schema requires every field, forbids extras, enforces enums/count/text/ordering,
   rejects unknown node IDs, and contains no graph, verdict, capability, tool, or score field.
2. Codex and Claude reviewer argv are tool-disabled and ephemeral as currently documented, the
   exact ProcessRequest is policy-decided, and denied/approval-required/timeout/malformed outputs
   fail without fallback. Scripted executors keep the test offline.
3. A structurally valid but obviously over-engineered proposal yields a blocking round-0 finding,
   receives exactly one bounded Planner revision, passes round 1, and persists/accepts only the
   revised proposal before the runner invocation count can become non-zero.
4. A justified broader plan required by the Goal returns no blocking findings, is accepted
   unchanged, and makes zero revision calls. Advisory-only findings also do not revise.
5. An invalid initial DAG never invokes the reviewer; an invalid or stale revision and round-1
   blockers never accept either proposal and make zero Worker, verification, composition, parent
   evaluation, approval, and promotion calls.
6. SQLite round-trip, GraphReplay, CLI replay, and Inspector expose the exact proposal-review-
   revision-acceptance digest chain and remain byte-stable across repeated read-only replay with all
   invocation counters zero. Tampered or missing new bindings fail closed for execution/resume.
7. plan-only completes the gate without Workers; resume uses the persisted binding without new AI
   calls; the existing post-execution replan tests keep their revision numbers, evidence fences,
   replan_count, and aggregate budgets.
8. Legacy and fixed CLI, policy-routed and hand-authored Graph inputs, old databases with no review
   records, and direct TaskOrchestrator ProposedGraph callers without the optional gate retain
   current acceptance, replay, and Inspector behavior.
9. Reviewer output cannot change policy, approvals, routing eligibility, capabilities, budgets,
   graph contents, accepted revision number, or promotion authority; final accept_task_graph()
   rejection always wins.

Place focused contract/adapter cases with task-planning tests and gate/persistence cases with
TaskOrchestrator, graph execution, revisioned replan, CLI, and Inspector tests. Use scripted model
outputs and temporary SQLite databases only. Then run those focused tests and the existing
repository-required ruff format --check ., ruff check ., mypy src, pytest -q, and git diff --check.

## Explicit deferrals

This milestone explicitly defers Issues #5, #6, #7, #8, #10, #11, and #14. It also defers new
dependencies, a reviewer-specific operator configuration or strategy role, generalized repair or
replanning, multiple revision loops, task-result review, parent-patch semantic review, independent
node reassessment, context or repository retrieval, learned scoring, confidence calibration,
free-form reviewer authority, promotion changes, UI, analytics, cross-run aggregation, and
speculative abstractions or extension points.
