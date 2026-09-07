# Accepted node process budgets

A node's `resource_budget.processes` reserves cumulative controlled process
launches. The scheduler reserves that allowance atomically across the graph;
WorkCoordinator now consumes it before each mediated command, project-local
installation, or verification dispatch. A process semaphore still limits
concurrency, but does not substitute for this cumulative accounting.

Mandatory verification slots are protected from optional worker commands. Native
worker process reservations, when configured, are also subtracted from the
mediated allowance; the isolated worker enforces its own native reservation.
The model worker invocation remains charged to `worker_turns`. This accounting
counts controlled launch boundaries, not arbitrary descendants of a command.
Supported project-local installation performs one controlled process dispatch.
Custom services must preserve that contract or separately enforce their children.

`node_process_admission_v2` records bind the accepted WorkerRequest, child run,
unique admission, service request, and accepted limits. A SQLite write transaction
checks the cumulative charge and persists the next admission before side effects.
Different request IDs, concurrent callers, and a reopened database cannot reset
that charge. Admissions are not refunded when a launch fails or execution is
interrupted. Completed actions are skipped on resume; any actual re-execution
requires another admission. Replay performs no dispatch and spends no admission.

Known legacy action-start receipts and verification results are imported once in
the same transaction. The original worker request must carry an explicit integer
process reservation for accepted graph nodes; absent or stale authority does not
grant an unlimited fallback. Historical compatibility WorkCoordinator runs that
have no accepted node WorkerRequest keep their existing policy behavior.

Exhaustion fails before launching the denied service with
`NODE_PROCESS_BUDGET_EXCEEDED` and a `node_process_budget_rejected` diagnostic.
Policy denial and pending approvals are resolved before an admission is spent.
