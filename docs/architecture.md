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

The worker never writes authoritative state and free-form prose is never executable.
General commands use `LocalProcessExecutor`. Git worktree lifecycle, diff construction,
exact patch application, and promotion are deterministic system operations encapsulated
by `GitWorkspaceManager`; they are not arbitrary worker subprocess authority.
