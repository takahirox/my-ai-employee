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

Graph and profile schemas are versioned at `1`. Unknown fields are rejected. Replanning
creates revision `n + 1`; accepted values are frozen and never edited in place. Runtime
generation and graph-revision fences reject stale state mutations.

Project and safety rules outrank routing optimization. Adaptive routing uses success
rate, then duration and cost, only after three samples. Until then it records an
explicit deterministic fallback reason. v0.1 contains no learned or opaque optimizer.

Process-like nodes accept structured command results but the runtime does not expose a
general subprocess API. Network and unrestricted process authorities are disabled by
the built-in policy floor.
