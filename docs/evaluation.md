# Productivity evaluation

Fleet's productivity suite is a reproducibility protocol, not a performance claim. It records
strict `fleet-productivity-results/1` bundles and keeps quality, human effort, elapsed time,
cost, reliability, and orchestration separate. A process exit code is not acceptance:
authoritative success and regression evidence determine whether a trial is accepted.

The CLI is deliberately read-only and offline. It does not start a worker, alter Fleet
authority, fetch a benchmark, or write an evaluation database:

```bash
fleet productivity validate results.json
fleet productivity report results.json
fleet productivity report results.json --direct-arm codex-direct --fleet-arm codex-fleet
fleet productivity report results.json --direct-arm codex-direct --fleet-arm codex-fleet \
  --format markdown
```

Input must be UTF-8 canonical JSON with a valid content digest and supported bundle version.
Unknown fields, tampering, missing arms, unpaired trials, capability mismatches, and unsupported
formats fail closed. JSON reports are canonical. Markdown reports explicitly separate human
active time (operator attention) from wall-clock time (elapsed latency) and list task classes
where paired Fleet trials have lower acceptance or equal acceptance with more human time.

## Controlled direct-versus-Fleet protocol

Define the task, baseline commit, acceptance authorities, regression checks, tool surface,
environment image, model snapshot, budgets, and stopping rules before observing outcomes. Every
pair uses the same task baseline, worker, provider, model, model version, tools, environment,
fairness configuration, seed, and repetition. Only the arm configuration changes.

For Codex:

1. In the direct arm, invoke the pinned Codex CLI once with the declared repository, prompt,
   model, reasoning effort, sandbox, tools, and budget. The human may perform only interventions
   allowed by the written protocol.
2. In the Fleet arm, configure Fleet's Codex adapter with that same executable, model snapshot,
   reasoning effort, tool capabilities, and total resource limits. Fleet may plan, review,
   repair, and orchestrate only as declared by the arm.
3. Apply the same authoritative acceptance and regression commands outside either worker's
   control. Record all Codex and orchestration tokens and costs; do not treat subscription cost
   as zero without saying so.

For Claude Code, use the same protocol with a pinned Claude Code executable, model and effort in
both arms. The direct arm uses Claude Code's declared permission/tool surface; the Fleet arm uses
Fleet's Claude adapter with an equivalent surface. Do not compare direct Claude to Fleet Codex,
or change model aliases between arms, and call the result a same-worker comparison.

Run multiple repetitions. Predeclare the repetition count from a power or precision target when
possible, and retain failures. Randomize or counterbalance arm order within task blocks so cache,
learning, service load, and time-of-day effects do not systematically favor one arm. Seeds and
repetition numbers are part of trial identity. Report paired distributions and sample variance;
do not select the best run.

Use clean worktrees from the same commit and equivalent dependencies, credentials, network
policy, CPU/memory limits, timeouts, context, and warmed or cold-cache policy. Pin benchmark and
tool versions. Include a no-agent or human baseline when the question requires it, but do not
substitute that baseline for the same-worker direct arm.

## Measurement and authority

- Quality is authoritative acceptance plus regression freedom. Retain criterion-level evidence
  digests and the authority that produced each result.
- Human active time is stopwatch time spent reading, prompting, approving, diagnosing, editing,
  or otherwise attending to the trial. Pause it while waiting. Record intervention count and a
  categorized intervention log using the same rules for both arms.
- Wall time starts at task release and ends at authoritative acceptance or the declared terminal
  condition. Time to accepted is absent for unsuccessful trials. Never add human active time to
  wall time or present one as the other.
- Record input/output and context tokens, API cost, compute duration/cost, and the pricing or
  subscription allocation rule. Missing cost data stays missing rather than becoming zero.
- Record retries, repairs, replans, escalations and recoveries separately. A recovery is not an
  accepted result unless authoritative acceptance and regression checks also pass.
- Record decomposed nodes, dependency edges, maximum parallelism, critical path, and unnecessary
  work. These explain orchestration behavior; they are not a substitute for outcomes.

## Fleet ablations

Run the complete Fleet arm against one-component-disabled arms using the same paired controls.
The minimum matrix is:

| Arm | Planning | Evaluation/review | Repair/replan | Parallelism |
| --- | --- | --- | --- | --- |
| Complete Fleet | on | on | on | declared bound |
| No planning | off | on | on | declared bound |
| No review | on | off | on | declared bound |
| No repair | on | on | off | declared bound |
| Serial | on | on | on | 1 |

Change one component at a time. Give every ablation an explicit arm ID and exactly one disabled
component. Report full-minus-ablation paired deltas; interaction effects require separately
predeclared factorial arms and cannot be inferred from single-component ablations.

## OSS tasks, SWE-bench, and real-world experiments

The generic OSS adapter normalizes a frozen task manifest containing repository, baseline,
version, task class, and authoritative criteria. Publish selection and exclusion rules, retain
every selected task, and report by task class. Comparisons with another orchestration system
must use the same worker capabilities and acceptance authority or be labeled non-comparable.

The SWE-bench adapter is an offline import profile. Obtain and retain the dataset separately,
pin its version and repository commits, and feed local case JSON to the normalizer. Fleet neither
downloads SWE-bench nor claims a score. The imported fail-to-pass and pass-to-pass tests become
explicit acceptance authorities; evaluation infrastructure and contamination limitations must
be reported with any results.

For real work, use controlled A/B experiments on consented, eligible tasks. Randomize assignment
within repository and task-class blocks; keep workers, tools, environment, review policy, and
acceptance authority comparable. Predeclare safety exclusions and stopping rules. Preserve human
override and incident handling. Report assignment, attrition, crossovers, learning effects, and
confidence intervals, and avoid productivity surveillance or individual ranking.

## Retention, compatibility, and regression comparison

Retain the exact canonical bundle, benchmark/task manifests, acceptance evidence, environment and
fairness digests, arm configuration, pricing table, tool/model versions, randomization schedule,
and analysis code. Treat bundles as immutable content-addressed records.

The current format is `fleet-productivity-results/1`; schema models use schema version `2`.
Reject unsupported versions. Historical comparison requires an explicit compatibility record
mapping every old task and arm to the new version and affirming equivalent baselines, criteria,
and capabilities. If those assertions cannot be made, publish separate results rather than a
regression delta. Accepted-to-unaccepted changes and increases in human time, wall time, API cost,
or compute cost are retained as regressions, not averaged away.

## The ten Issue #68 questions

The suite answers the success questions as ten distinct analyses rather than one synthetic score:

1. **Does Fleet complete more work correctly?** Compare paired authoritative acceptance and
   regression-free rates.
2. **Is output quality preserved?** Inspect criterion and regression evidence, not exit codes or
   self-reports.
3. **Does Fleet reduce human effort?** Compare human active seconds and intervention counts.
4. **What happens to latency?** Compare wall time, time to accepted, and critical-path time.
5. **What does it cost?** Compare tokens, API cost, compute usage, and disclosed allocation rules.
6. **Is it more reliable?** Compare retries, terminal failures, escalations, and recoveries while
   retaining unsuccessful trials.
7. **Does orchestration help?** Relate decomposition, dependency edges and parallelism to paired
   outcomes and unnecessary work.
8. **Where does Fleet hurt?** Report task classes with lower acceptance, or equal acceptance and
   greater human active time, in the paired comparison.
9. **Which Fleet components matter?** Use the planning, review, repair and serial ablations.
10. **Do results generalize and persist?** Replicate across workers, frozen OSS/SWE-bench profiles
    and controlled real-world A/B tests, then use explicit compatibility mappings for regressions.

## Limitations and cadence

Small samples have high variance. Task selection, benchmark contamination, model drift, service
load, evaluator coverage, pricing allocation, imperfect active-time capture, and interactions
between Fleet components limit causal and external validity. Markdown summaries aid review but do
not replace retained bundles and evidence. The suite makes no claim until a protocol is run.

Run cheap deterministic fixture and bundle-compatibility regressions on every change. Run a small
frozen smoke panel on material orchestration or adapter changes. Expensive external benchmarks
should run on a documented release cadence (for example, before a release or quarterly), and when
the worker/model, evaluator authority, benchmark version, or Fleet algorithm changes materially.
Never spend benchmark budget merely to refresh a headline; publish versioned, reproducible runs.
