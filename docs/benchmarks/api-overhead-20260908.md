> Historical benchmark report from before Issue #167. It does not describe the current runtime.

# API worker overhead: bounded investigation

## Finding

The retained comparison establishes additional model preparation and a different
worker execution path. It does **not** establish redundant API traffic or unnecessary
API retries. No API-call deletion is justified by this sample. Keep review stages,
permissions, verification, retry behavior and time allowances unchanged.

This report resolves the investigation in #140. The transport reductions in #138
and #139 are separate changes; they are not presented as a fix for the whole API gap.

## Reproduction identity and limits

- My AI Employee: `ec0993b945222c2ef8fa7f5eae978a3176b6ebf4`.
- Pocket Agent Bench: `91fbdc73dfbec5a06e6a1c95ea63caa227da5892`, plus recorded existing
  working-tree changes in its CLI/interface/suite. The commit alone is insufficient
  to reproduce those local changes; use the retained source snapshot.
- Job: `sol-employee-comparison`, September 8, 2026; 12 tasks per arm, one attempt per
  task, concurrency one, fresh containers, 180-second aggregate agent allowance.
- Both compared arms used Sol high. No explicit Fast setting. Employee used its
  default assessment, planning, plan review and worker stages. All 12 tasks passed
  in both arms; Employee had no internal worker repairs or protocol errors.
- Native usage is complete. Token inputs include cached input; billing is unknown.
- This is three API tasks with one sample each, not a stable performance estimate.

## Phase and usage accounting

Agent execution includes the configured agent and optional declared program; it
excludes container preparation and the external grader. Per-stage times are model
CLI invocation durations, not pure inference time or individual HTTP latencies.

| Three-task total | Seconds | Input tokens | Cached input | Output tokens |
| --- | ---: | ---: | ---: | ---: |
| Sol single agent, entire agent phase | 98.501 | 118,185 | 99,968 | 1,931 |
| Employee assessment | 25.701 | 43,943 | 21,760 | 281 |
| Employee planning | 110.488 | 54,803 | 11,776 | 3,036 |
| Employee plan review | 17.654 | 48,845 | 21,760 | 157 |
| Employee worker | 223.175 | 196,696 | 160,640 | 5,486 |
| Employee, entire agent phase | 383.991 | 344,287 | 215,936 | 8,960 |

Employee preparation takes 153.844 seconds; worker invocations take 223.175 seconds.
The remaining 6.972 seconds are outside these four model invocation totals. They
include wrapper/runtime work, verification and declared execution; this sample does
not partition that residual fully. The six recorded internal verification results
sum to approximately 0.155 seconds and are only one part of it.

The total difference from Sol is 285.490 seconds. Preparation contributes an observed
153.844-second component. Comparing Employee workers with the entire Sol phase
leaves another 124.674 seconds, but those phases perform different work; this is an
accounting comparison, not an isolated causal estimate. Summing cached and uncached
input reconciles to total input; cache effects and model-server load are uncontrolled.

## API traffic and side effects

Retained service state records the same request sequence for both configurations:

| Task | Requests in each arm | Side-effect evidence |
| --- | --- | --- |
| api-pagination | GET /items; GET /items?page=2 | Two pages; no extra API request |
| api-retry | GET /value three times | Three value attempts, as required by the fixture |
| api-approval | GET /request; GET /approval | Zero executions while approval is absent |

Therefore, the observed gap cannot be attributed to additional recorded API calls.
This does not measure request latency or rule out other kinds of overhead.

## Different work performed by the workers

The common optional `pocket-python-v1` contract permits direct API execution or a
Python program plus an execution manifest. It does not force a reusable script on
either configuration.

- Sol performed all three workflows directly; each `execution.json` records
  `executed: false` for the optional program step.
- Employee produced a script under `src/` plus `output/execute.json` in every API
  task. Each optional program step records `executed: true` and exit code zero.
- The saved Employee scripts contain 36, 18 and 36 lines for pagination, retry and
  approval respectively. These counts are observations, not evidence of excessive
  complexity or a target to minimize.
- Employee's worker outputs contain 2,293, 1,302 and 1,891 tokens respectively,
  versus Sol's entire-agent outputs of 830, 446 and 655 tokens. Preparing executable
  candidates and structured proposals is a plausible contributor; this sample does
  not quantify its independent effect.

Employee's proposal/runtime route and single-agent direct execution are different
operational contracts. Changing action authority or bypassing verification to copy
the single-agent route would require a separate design review. It is not an
identified redundant-call optimization.

## What the available tool observations can and cannot establish

Employee's body-free progress records show 0 completed command items for pagination,
3 for retry and 3 for approval. Sol's native event stream shows 2, 1 and 3 completed
command items respectively. Count only completed items, not both start and completion.
A shell item may contain multiple operations, so these counts are not API counts.
Employee's retained progress for these invocations is not truncated.

The six Employee command items have zero observed start-to-completion intervals.
`model_progress.py` measures when event chunks reach the observer. Start and completion
can arrive in one chunk, so zero does **not** prove instantaneous tool execution.
Do not subtract these observations from invocation time and call the remainder
"reasoning time." Model output generation, server waiting and buffering remain mixed.
Raw command bodies are intentionally absent from Employee's retained progress and
filtered stdout. Consequently this investigation cannot classify those commands as
necessary inspections versus redundant checks. Do not add secret-bearing request
bodies or model reasoning to logs merely to fill that gap.

## Decision and follow-up evidence

No avoidable API operation has been demonstrated, so this investigation introduces
no speculative runtime optimization. Preserve the successful approval and retry
semantics. Continue separately measured transport reductions without claiming they
explain the entire gap.

If this category remains a priority, the next bounded diagnostic should collect
trusted request durations and coarse operation counts without bodies, separately
from model-stream arrival times. Instrumentation must have bounded overhead and
explicit completeness/truncation reporting. Repeat a predeclared workload only when
needed to distinguish variance; preserve every outcome and stop on usage limits.
Review-effectiveness experiments remain deferred.

To audit these findings, join saved trial results with `fleet-diagnostics.json`
by trial, join usage and progress on `request_digest`, and compare `execution.json`
with service state. Count `item.completed` once, retain missing timings as unknown,
and inspect saved script sizes without publishing their contents. The aggregate
figures above do not require publication of credentials, host paths or model traces.
