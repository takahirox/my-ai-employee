# Issue 14 design review: single-run explainability

Issue 14 proposes a broad observability foundation. The first implementation keeps the
existing SQLite records and Inspector as the source of truth and adds one vertical
slice: a running or historical run can be explained as a coherent story without
re-running AI or manually joining raw records.

## Implemented scope

`fleet explain RUN_ID` and `GET /api/runs/RUN_ID/explanation` deterministically project:

- the Goal and final disposition;
- the accepted graph, dependencies, durable task states, and ready/waiting/blocked
  positions;
- Planner and task routing selections with their persisted assessment and selection
  reasons;
- body-free context manifests showing predecessor results, evidence, accepted feedback,
  and artifact references;
- worker attempts, deterministic criterion evidence, evaluator and independent-review
  decisions, and bounded retry/repair/replan decisions;
- graph revision ancestry, triggers, evidence, and added/removed/retained tasks;
- stable failure codes and the resulting failure path;
- ready-to-promote versus completed/promotion state.

The projection is point-in-time and read-only. Missing evidence remains absent instead
of being guessed. Only the current accepted revision is treated as current authority;
older revisions remain visible as graph evolution. Secret and artifact bodies are not
opened. A pre-acceptance plan-review failure now retains its Goal alongside the already
persisted proposed graph, so that failure is also explainable historically.

## Deliberate non-goals

- a dashboard, UI framework, live tracing service, or second telemetry database;
- cross-run analytics, cost optimization, or automatic diagnosis;
- model-generated explanations or access to private chain-of-thought;
- redesigning legacy runtime persistence.

The existing `fleet inspect` raw projection remains available for deeper forensics and
backward compatibility. Future analytics can consume the stable persisted evidence and
the explanation projection without changing runtime authority.
