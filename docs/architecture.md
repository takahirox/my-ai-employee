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

The worker never writes authoritative state and free-form prose is never executable.
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
