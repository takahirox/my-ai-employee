# Worker deadlines and bounded recovery

An accepted node wall-time reservation is a hard limit. The supervisor also records
an operator profile's recommended duration and progress observations; neither a
recommendation nor silence alone authorizes early cancellation. The effective
attempt deadline remains the minimum of the accepted node, adapter, execution
policy, and remaining run allowances, with a small finalization reserve (default
2 seconds, capped at 5% of the actual remainder).

Wall time is a shared, durable deadline covering planning, assessments, workers,
verification, and repair. New plans do not reserve two full wall durations for an
initial attempt and a possible repair. Their node ceilings may use the full run
limit; each admission is clipped to time actually left. A repair can use only that
remainder. Explicit smaller accepted node ceilings remain binding. Worker turns,
processes, artifacts, attempts, and retry counters retain their cumulative reservations.
Pause/resume and recovery retain previously consumed active wall time.

## A timeout can recover within existing authority

The adapter's process timeout can finish before the scheduler's watchdog fires.
Both paths now use the same recovery decision. Previously only the watchdog path
could use an accepted timeout retry, making the outcome depend on which timer won.

A retry requires all of the following:

- The returned result is bound to the exact child invocation.
- Cleanup is confirmed. Adapter timeouts require an explicit process-cleanup
  result; unknown or failed cleanup blocks another invocation. Legacy in-process
  scheduler runners retain their completion acknowledgement on return within the
  cleanup grace period.
- The accepted node and graph permit another retry and another attempt's resources
  remain. The original strategy, model, and backend remain unchanged.
- The actual remaining run time can meet the selected profile's minimum. A stale
  reservation snapshot does not authorize a retry after the overall deadline.

Cancellation takes precedence. A concurrent budget-exhaustion or other failure
is not converted into a timeout retry. Cleanup failure also prevents a replan
recommendation. Any authorized replan still requires normal graph acceptance;
this mechanism does not create a new plan or extend the run deadline.

## Inspecting an interrupted attempt

`fleet inspect RUN_ID` exposes `timeout_recoveries`. New records include a
`context` with the timer source, exact request/result digests, cleanup status,
remaining run seconds, and minimum retry seconds. The decision and context are
content-digest bound. Existing records without this context retain their historical
digests and can still be loaded.

Returned worker results, including any stdout/stderr artifact references, are kept
for diagnosis. Late proposals, patches, and completion claims do not become accepted
evidence. A retry starts a new bounded attempt; saved output is not a resumable
checkpoint and external side effects are not assumed to be reversible.

Configured local Codex invocations also persist `model_progress_v2` while stdout
arrives. Inspector exposes `model_progress`, containing event kinds, observation
times, elapsed/item durations, and numeric exit status. Commands, arguments, tool
output, reasoning text, provider item IDs, and provider timestamps are excluded.
At most 128 records per invocation retain a prefix and the latest observation.
These are diagnostics, never completion or routing evidence. A hard host kill can
still leave an unmatched start; absence of an event does not prove inactivity.

## Scope and tradeoffs

This change makes already-authorized recovery consistent. It does not enable
retries for plans that have zero retry allowance, increase any default budgets,
borrow repair reservations beyond the existing resource checks, change model
routing, or purchase/reset provider allowance. In particular, the default bounded
Planner's zero-retry policy is unchanged.

Progress-based deadline extensions and general checkpoint/resume need stronger
progress and side-effect contracts. They are not inferred from a model's elapsed
reasoning time. Model-free tests cover both timer orderings, successful recovery,
exhausted time/counters, failed or unknown cleanup, cancellation, result binding,
replay, and legacy digest compatibility; these tests do not establish live-model
success rates.

The worker request and its persisted context advertise the effective attempt allowance,
including already-consumed planning time and stricter adapter/policy ceilings. The original
node reservation remains recorded separately; it is not advertised as available work time.

Bounded planner output omits initial execution metadata (schema versions, state,
generation, transitions, failure, and unsupported edge control flow). Domain defaults
restore those values before normal graph acceptance. Goals, capabilities, completion
criteria, verification IDs, resource budgets, independent assessments and reviews retain
the same validation and authority. Unbounded graph serialization is unchanged.
