# Active wall-time budgets

The Harness wall-time limit bounds one logical `fleet work` run, including model
preparation, accepted node execution, reviews, composition and parent verification.
An accepted graph or execution policy can tighten that limit. Internal timing
estimates do not independently grant more time or turn into additional stop rules.
The existing per-attempt watchdog and authorized retry limits remain in force.

Each stage uses the remaining active time. Model preparation/review requests are
bounded before policy resolution; local and isolated process execution, browser
actions/captures and restricted download transport timeouts also use the remaining
deadline without changing an already bound request digest. Late browser startup,
capture, download transport and final EOF results are checked before acceptance;
interruption closes their browser session or response body.
The scheduler uses the same remaining time for dispatch and timeout recovery.
Cancellation keeps precedence when cancellation and budget exhaustion coincide in
an owned run. Otherwise the stable terminal code is `RUN_WALL_BUDGET_EXCEEDED`.
A late PASS cannot publish completion, a planned result, or promotion readiness.

The CLI uses a monotonic clock. Embedded schedulers use a monotonic wall clock by
default, with explicit clock injection supported for deterministic tests. Adapters
must poll the cancellation token during blocking execution. As with per-attempt
timeouts, an arbitrary noncooperative in-process plugin cannot be forcibly killed;
its late completion does not become accepted evidence. The local process service
cleans surviving descendants in its owned process group before returning even when
the foreground leader exited normally and all capture pipes have closed. A mediated
process does not grant an unmanaged background-job lifetime. Internal Git operations
for worktree creation, patch validation/capture and candidate reconstruction also
poll the shared stage control and wall budget through one supervised Git boundary.
Their hooks and filters are cleaned with that operation. These deterministic runtime
operations do not consume model-mediated process reservations. Already returned, exactly
bound worker results are retained for diagnostics when the overall deadline wins.

## Persistence and resume

`run_wall_start_v2` binds a unique invocation, logical run ID, UTC recovery origin
and accepted time limit. `run_wall_finish_v2` binds that start to measured active
seconds. These are additive v2 records; existing graph and worker records retain
their canonical serialization. Start identity and the recovery clock origin are
part of the content digest, rather than relying on descriptive `created_at` alone.

Nested stages share the current interval, so their durations are not double
counted. On resume, completed intervals are charged once. Waiting between a
completed pause and a later invocation consumes no active allowance. A new
invocation cannot reset the budget or raise an earlier accepted limit.

After a crash, an unclosed interval has no monotonic completion receipt. Recovery
conservatively charges elapsed UTC time through recovery and freezes that charge
when the old owner is no longer live. This may exhaust the allowance after a long
unobserved interruption; it does not silently assume that the missing interval
was free. Subsequent pauses do not keep charging the recovered interval.

For existing runs without these records, the first invocation imports known usage
from digest-bound execution-profile timings and execution-owner/closure records.
It takes the larger observation instead of summing nested measurements, records
the source digests, and imports only once. The legacy start/finish pair is inserted
in one SQLite transaction: concurrent importers use the winning complete pair,
and an interrupted insert cannot publish half an import. Pre-existing incomplete
legacy imports are rejected rather than treating known prior work as free. Unclosed legacy intervals are charged
through the recovery observation. Profile records with stale bindings and
orphan completion receipts and conflicting normal completions are rejected.
A normal completion overlapping crash recovery is charged once using the maximum
observed duration; a late normal finalizer cannot discard previously observed time.

## Verification

Model-free regressions cover cumulative stage time, pause/resume without an
allowance reset, legacy usage import, interrupted interval recovery, digest-bound
clock/identity fields, the ProcessExecutor's actual timeout, pre-acceptance CLI
failure persistence, expired parent verification, and a late 80-second Task Review
under a 60-second graph limit. Existing timeout-recovery tests separately exercise
per-attempt timeouts with remaining global allowance.

## Authority at publication

Graph admission and terminal success read the live run budget inside their SQLite
write transaction. Copied remaining-time fields are diagnostic snapshots. Scheduler
recovery and parent repair preparation use the same persisted active-time authority;
node creation timestamps cannot supply a fallback allowance. See the
[ownership audit](authority-audit.md) for the acceptance boundary and compatibility rules.
