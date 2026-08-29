# Issue 10 review: bounded closed-loop orchestration

Issue 10 correctly separates transient retry, local repair, and graph replan, but the full
proposal also describes future AI reviewer and semantic evaluator producers. The first milestone
does not need a general workflow engine or new probabilistic authority. Fleet already owns the
required authority primitives: accepted graph revisions, generation and attempt fences,
evaluator records, aggregate resource reservations, revision ancestry, fresh worker context, and
worker-free replay.

This implementation therefore adds one small deterministic loop vocabulary (`PASS`, `RETRY`,
`REPAIR`, `REPLAN`, `ESCALATE`, `FAIL`) and persists each selected transition as an immutable fact.
Existing exception retry remains the only retry source. A local repair is allowed only after a
current node evaluator returns `FAIL` and its worker result, evidence, evaluator, graph revision,
generation, attempt, and request bindings are all authoritative. The next attempt receives only
the accepted evidence and evaluator digests in a new `WorkerRequest` and
`WorkerContextManifest`; conversation history and artifact bodies remain excluded.

Retry and repair use independent limits (`max_retries` plus the node retry cap, and
`max_repairs`). Both still consume the existing aggregate attempt, worker-turn, process, wall-time,
and artifact budgets. Exhausting an enabled loop limit selects `ESCALATE` and fails closed; a
disabled/non-applicable loop selects `FAIL`. No unattended human action is performed.

Graph replan continues to accept only an already-produced strict `ProposedGraph`. Existing
revision ancestry, authoritative evidence, budget monotonicity, deterministic graph validation,
generation fencing, and compatible-node retention remain unchanged. Acceptance additionally
records a `REPLAN` transition. This is the integration point for a future planner policy; Fleet
does not implement Issue 7's AI reviewer or Issue 8's semantic evaluator here.

Inspector and replay expose the stored transition history and counters without invoking workers,
evaluators, planners, composition, or promotion. The milestone is demonstrated with transient
retry, accepted-evidence repair, revisioned replan, stale feedback rejection, and bound exhaustion
tests.
