# Trust Kernel architecture

The authority boundary is intentionally narrow:

1. generators or external workers produce a candidate `Graph` or `NodeProposal`;
2. graph validation returns a stable issue set, and `accept_graph` creates an
   immutable, digest-bound `AcceptedGraphRevision`;
3. `DeterministicRuntime` alone advances table-validated states and accepts output
   contracts, artifact metadata, and evidence references;
4. completion is derived from mandatory criteria, gates, artifacts, evidence coverage,
   and blocking findings;
5. SQLite stores canonical structured inputs, events, checkpoints, profiles, routing
   history, and metrics. Replay reads accepted `ResultEnvelope` events and invokes no
   worker handler.

Graph schemas remain versioned at `1`; Project Harness and work-service contracts are
versioned at `2`. Unknown fields are rejected. Replanning
creates revision `n + 1`; accepted values are frozen and never edited in place. Runtime
generation and graph-revision fences reject stale state mutations.

Project and safety rules outrank routing optimization. Adaptive routing uses success
rate, then duration and cost, only after three samples. Until then it records an
explicit deterministic fallback reason. v0.1 contains no learned or opaque optimizer.

v0.2 adds a reviewed-patch vertical slice around the same authority root:

```text
CLI worker (read-only proposal generation)
  -> strict WorkerProposalEnvelope
  -> PolicyResolver / approval
  -> controlled process, download, install, or exact-patch edit service
  -> deterministic Harness verification
  -> patch artifact + AcceptanceLedger
  -> explicit digest-bound promotion
```

Every `fleet work` invocation enters this slice through `GraphExecutionService` and is
persisted as a `GraphRunRecord`. Fixed routing constructs a one-node authoritative Graph;
adaptive routing accepts a Planner-proposed child DAG. Inspector drill-down reads those
Graph-owned node, route, execution, evidence, and review records.

An optional `JobRecord` sits above this authority tree solely as durable grouping identity.
`JobGraphRunRecord` appends independently claimed Graph Run IDs to the Job with a monotonic
sequence. The relationship is established explicitly by `fleet work --job-id`; it is not inferred
from goal text, repository, worker ancestry, timestamps, or graph revision lineage. Consequently:

- a new invocation under a Job creates a new ordered child Graph Run;
- a replan changes the accepted revision and generation of the same Graph Run; and
- omitting `--job-id` preserves the pre-Job standalone-run model.

Job records carry no policy, evidence, verification, promotion, replay, or mutation authority.

The worker never writes authoritative state and free-form prose is never executable.
The Codex worker transport omits proposal and request IDs. After checking process
correlation and cancellation, the adapter allocates these IDs locally before domain
validation and mediation. Legacy model-supplied IDs are replaced as well. Persisted
proposals retain their attributed IDs and digests; replay does not allocate new ones.
General commands use `LocalProcessExecutor`. Git worktree lifecycle, diff construction,
exact patch application, and promotion are deterministic system operations encapsulated
by `GitWorkspaceManager`; they are not arbitrary worker subprocess authority.

## First-party evaluator foundation

Runtime-observed evidence uses `CandidateRevision`, an immutable fleet-work identity bound
to a run ID, generation, base commit, and exact patch or tree digest. It is deliberately not
called `AcceptedGraphRevision`: this foundation does not attach evaluation to the graph
runtime. Evaluation requests, observation manifests, results, and replay ledgers also bind
the evaluator specification and effective policy digests. A pure freshness check rejects a
mismatch before evidence can influence a decision.

Evaluator providers are developer-managed first-party code behind a static registry. Fleet
does not discover Python entry points, dynamically import providers, load marketplace code,
or give a provider state-transition authority. A provider receives a narrow mediated service
surface and returns typed observations, findings, and criterion outcomes. Deterministic core
logic alone maps those facts to `PASS`, `REPAIR`, `ESCALATE`, or `FAIL`.

`process.harness` adapts an exact predeclared
Harness command to the existing `ProcessExecutor`, policy decision, cancellation, and artifact
paths. Process stdout and stderr remain ordinary `ArtifactDescriptor` records; evaluators do
not write observation files into the candidate worktree. Established required commands are
derived into required process evaluators in memory, preserving existing Project Harness intent.

`browser.playwright` is the second available provider. The Playwright dependency is optional
and loaded only for a declared browser evaluation. Its service maps an exact loopback origin to
contained candidate-workspace files, denies all other requests, uses a fresh credential-free
context, executes only typed bounded actions, and persists typed browser observations and
content-addressed capture artifacts. Provider code still cannot accept a candidate.

The graph-first work path composes node patches, captures one immutable parent candidate, then
runs all required evaluators in that composition workspace. It persists the candidate revision,
evaluator specifications and requests, process results or browser observations, observation
manifests, evaluation results, evidence ledgers, a criterion-level `AcceptanceLedger`, and one
parent decision before promotion can be requested. A second
live diff capture rejects a candidate that changed during evaluation. Inspector exposes the
stored records as metadata-only projections, and replay does not invoke workers, evaluators,
workspace capture, composition, or promotion.

The IDs `judge.visual` and `threejs.instrumentation` are reserved for future first-party
implementations and are rejected as unavailable today. A `REPAIR` decision is replayable typed
data, but parent evaluation currently converts any non-`PASS` result into a failed graph run.
The next transition reuses the bounded repair machinery: an immutable transition cites the
parent evidence, advances generation/attempt, passes accepted feedback references to a worker,
recomposes, and reevaluates. Repair count and remaining resource budgets gate admission;
generation fences reject stale output, and exhaustion escalates or fails. Probabilistic and
indeterminate results must continue to escalate rather than pass or repair automatically.

## Productivity evaluation boundary

Productivity evaluation models are deterministic records outside the execution authority path.
The `fleet productivity` CLI validates canonical result bundles, aggregates existing trials, and
optionally performs an explicitly named paired direct-versus-Fleet comparison. Its offline
`combine` command writes only one explicitly named new canonical bundle from validated one-arm
sources; validation and reporting remain read-only. It neither invokes workers nor opens Fleet's
operational database,
changes policy, downloads benchmarks, or promotes work. Metric families remain separate so cost,
latency, or orchestration cannot silently override authoritative quality or human effort. See the
[productivity evaluation protocol](evaluation.md) for experiment design and retention rules.

### Owned parent verification and reviewer control

Writing graphs remain in the nonterminal `verifying` state while composing and
checking the parent candidate. The same execution owner remains live through that
stage; only its fenced final write can publish `ready_to_promote` and close the
lease. Parent repair preparation acquires a fresh fenced maintenance attempt
before changing durable repair state. Replay performs no stage work.

CLI task, parent, and plan reviewers poll the currently bound runtime control via
the existing ProcessExecutor cancellation interface. Polling renews the owner's
lease and observes cancellation; a late response is checked again before it can
be accepted. Adapters must poll Cancellation during blocking work, as required by
the process service contract. A pause drains an already-running node and its task
review, preserving its verified result for resume, but stops parent composition or
verification before promotion readiness. Cancellation never accepts a late PASS.

### Shared active wall-time budget

`fleet work` binds one active wall-time budget before model preparation. Planning,
classification, node attempts, task review, composition, parent verification and
bounded parent repair share its remaining time. Nested stages may tighten the
accepted limit, but cannot replenish it. See [run-time-budgets.md](run-time-budgets.md)
for persistence, pause/resume, legacy migration and crash accounting.

Accepted node process reservations are enforced at mediated dispatch, including
commands, installation and required verification. Durable admissions prevent
sequential calls or resumed execution from resetting the allowance; see
[node process budgets](node-process-budgets.md).

Adaptive worker routing reads repository-scoped, verified node outcomes from
committed execution facts. It preserves mandatory eligibility checks and binds
history provenance into each node route; see
[adaptive routing history](adaptive-routing-history.md).

### Writing-node artifact reservations

Accepted writing nodes charge unique artifact content bytes against their explicit
`artifact_bytes` reservation. Process output, patch captures, and verification output
share that allowance. Repeated descriptors for identical content consume bytes once;
accepted predecessor inputs remain charged to their producer. Metadata and temporary
streaming buffers are not content bytes. Streaming is still bounded by the per-object
store limit and the node's total reservation.

The atomic artifact store persists a node/request/content-bound admission before it
publishes a blob. Concurrent producers cannot spend the same remaining capacity.
Interrupted publications retain their reservation, and child resume reloads it.
Retained descriptors from custom stores and older runs are reconciled before accepting
completion. Exhaustion produces `NODE_ARTIFACT_BUDGET_EXCEEDED` and cannot publish a
passing node patch. Graph reservations continue to bound the sum allocated to nodes.

Normal wall-time completion and crash recovery can overlap for the same invocation.
Their receipts are settled as one interval using the conservative maximum, so a late
normal finalizer neither causes a duplicate-receipt failure nor loses observed time.
Successful graph terminalization checks accepted cancellation in the same SQLite
transaction that publishes the terminal graph and closes its owner lease.
