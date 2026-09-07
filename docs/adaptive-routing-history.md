# Verified adaptive routing history

Adaptive node dispatch now loads performance observations from committed runtime
facts. The CLI's top-level worker selection uses the same loader. Planner selection
keeps its existing fixed/policy behavior; worker outcomes do not train a Planner.
Mandatory Harness/operator constraints and assessment suitability still run before
history ranking. Fewer than three qualifying samples uses the existing deterministic
fallback. There is no automatic exploration or permission to add a model/provider.

## Scope and source authority

Lookup reads at most the 200 most recently registered graph runs in the same local
repository identity and uses at most 200 matching node attempts. Worktrees sharing
a Git common directory share that repository identity. Another checkout, repository,
Harness digest, effective policy, operator configuration, task kind, or executed
strategy configuration does not supply observations. Strategy identity includes the
backend, model, effort, context/retry/review configuration and capabilities; routing
mode and explanatory reasons are excluded. Task classes match complexity, scale,
risk, required capabilities, semantic categories, and bounded context-size bands.
Missing repository registration or operator configuration gives a cold fallback.

Each observation requires a terminal graph with a matching closed execution owner,
an exactly bound route and WorkerRequest/WorkerResult, and the runtime's node
evidence and evaluator decision. Writing successes additionally require the exact
parent candidate PASS and promotion readiness. Read-only successes require the
Goal evaluator PASS. Required independent Task Review must also pass. In-progress,
paused, cancelled and interrupted runs do not supply observations. A worker success
without the required evidence cannot become a successful observation. An explicit
node evaluator FAIL supplies a failure observation; an infrastructure/control
failure with no corresponding evaluator does not. Passing nodes in a graph whose
parent failed are not counted as successful worker outcomes.

Statistics are derived from these authoritative records when routing, rather than
maintaining another mutable counter that can drift after a crash. A child attempt
is counted once by its child-run and accepted-request identities. Retained copies
across graph revisions are excluded. Replay reads facts and never trains a model,
launches a process, or increments a counter. Resume cannot duplicate samples.
Missing or invalid optional history is ignored without failing the current task.
The historical unscoped `strategy_performance` aggregate is not imported.

## What is optimized

Among equally suitable, policy-eligible mature strategies, ranking uses verified
success rate and the runtime-observed node execution/evaluation duration, then the
existing deterministic tie-break. This duration is measured between persisted
running and terminal node transitions, not the worker's self-reported duration and
not the total user-visible time including parent composition/review. No monetary
cost is inferred: all derived cost fields are uniformly zero, so unknown cost does
not favor any strategy. These conditional observations are not causal estimates or
a replacement for controlled benchmark comparisons.

`NodeRouteRecord.performance_history_digests` retains the node/lease source digests
used for each adaptive decision. Empty history preserves legacy route digests;
non-empty provenance is included in the route's content digest. Selection reasons
retain the sample count and success rate. Fixed selection and replay continue to
use their exact persisted strategy decisions.
