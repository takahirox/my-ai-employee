# Productivity evaluation

Fleet's productivity suite is a reproducibility protocol, not a performance claim. It records
strict `fleet-productivity-results/2` bundles and keeps quality, human effort, elapsed time,
cost, reliability, and orchestration separate. A process exit code is not acceptance:
canonical criterion and regression outcome records bind each declared check ID and authority to
a pass/fail disposition and evidence digest. Success, regression freedom, and acceptance are
derived from those records rather than supplied by a caller.

The CLI is deliberately offline. It does not start a worker, alter Fleet authority, fetch a
benchmark, or write an evaluation database. Validation and reporting are read-only; combination
writes only the explicitly named, previously absent output file:

```bash
fleet productivity combine --output results.json direct.json fleet.json
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
Bundles also require canonical task/arm/result ordering, unique trial IDs and logical keys, stable
arm families, and complete check coverage. A terminal outcome and failure classification are
retained for every unsuccessful trial and reported as rates.

## Controlled direct-versus-Fleet protocol

Define the task, baseline commit, acceptance authorities, regression checks, tool surface,
environment image, model snapshot, budgets, and stopping rules before observing outcomes. Every
pair uses the same task baseline, worker, seed and repetition. Retained environment and fairness
manifests bind executable/version, dependency lock, sandbox/network, cache/machine, prompt/context,
model/reasoning, tools, resource budgets, stopping conditions, pricing allocation, and the complete
randomized order. Their stored digests must equal their canonical content digests. Direct-versus-
Fleet validation compares every controlled manifest field. Arm manifests retain planning, review,
repair and parallelism settings. An ablation is valid only when its declared disabled component is
the sole differing arm-manifest field.
Stopping conditions use exactly one canonical identifier per terminal outcome: `accepted` maps to
`accepted`, `checks-failed` to `checks_failed`, `execution-failed` to `execution_failed`,
`timed-out` to `timed_out`, and `cancelled` to `cancelled`. Unknown identifiers and incomplete
internal mappings fail manifest validation. A trial also fails validation when its retained
stopping policy does not list the identifier mapped to its observed terminal outcome.

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
separate acceptance and regression authorities; evaluation infrastructure and contamination
limitations must be reported with any results. Representative native records may use uppercase
`FAIL_TO_PASS` and `PASS_TO_PASS` arrays or JSON-encoded arrays. The typed
`TaskIdentity.swe_bench_provenance` projection retains the standard `problem_statement`,
`hints_text`, `patch`, `test_patch`, `version`, `created_at`, and
`environment_setup_commit` fields. Only SWE-bench tasks may populate that projection; unrelated
adapters leave it null. Other fields remain forbidden, and parsing performs no network access.

For real work, use controlled A/B experiments on consented, eligible tasks. Randomize assignment
within repository and task-class blocks; keep workers, tools, environment, review policy, and
acceptance authority comparable. Predeclare safety exclusions and stopping rules. Preserve human
override and incident handling. Report assignment, attrition, crossovers, learning effects, and
confidence intervals, and avoid productivity surveillance or individual ranking.

## Retention, compatibility, and regression comparison

Retain the exact canonical bundle, benchmark/task manifests, acceptance evidence, environment and
fairness digests, arm configuration, pricing table, tool/model versions, randomization schedule,
and analysis code. Treat bundles as immutable content-addressed records.

The current format is `fleet-productivity-results/2`; schema models use schema version `2`.
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

The exact cheap cadence is every pull request. The exact expensive cadence is Sunday at 03:00 UTC
and every release candidate; it also runs whenever the worker/model, evaluator authority,
benchmark version, or Fleet algorithm changes materially. Never spend benchmark budget merely to
refresh a headline; publish versioned, reproducible runs.

## Version-controlled offline protocols

[`examples/productivity/protocols.json`](../examples/productivity/protocols.json) is the canonical
protocol index. It contains runner templates for direct Codex, Codex through full Fleet, direct
Claude, Claude through full Fleet, every exact one-component ablation, a generic OSS producer,
native SWE-bench import, randomized real A/B collection, and release regression. Angle-bracketed
values are mandatory operator substitutions; replace every placeholder in both the command and
treatment manifests, then recompute the adjacent canonical manifest digests. None of the templates
claims measured performance.

The runner is deliberately separate from the `fleet` command and does not change Fleet execution
or promotion authority. For example, after creating a canonical `TaskIdentity` JSON file and an
offline producer wrapper, collect the direct-Codex protocol with:

```bash
python -m ai_employee.productivity_protocol \
  --manifest examples/productivity/protocols.json \
  --protocol codex-direct \
  --task ./task.json \
  --repository ./disposable-checkout \
  --output-root . \
  --timeout 3600 \
  --network disabled \
  -- ./operator-productivity-producer \
  --task '{task}' --repository '{repository}' --output '{output}' --protocol '{protocol}'
```

The arm command is an argv array, never a shell string. Each of `{task}`, `{repository}`,
`{output}`, and `{protocol}` must occur once as a complete argument. The runner resolves the task,
checkout, output staging directory, protocol ID, and executable before launching the producer with
the checkout as its explicit working directory. Symlinks in the producer path are resolved, and the
resulting executable must exactly equal every treatment environment executable before the process
is started. The task file must be the canonical JSON encoding
of one `TaskIdentity`, followed by one newline. The selected manifest and caller must both declare
the same network policy, and every result arm must retain that policy in its environment manifest.

The producer must create exactly three regular files in `{output}`:

- `acceptance.json`, a canonical `fleet-productivity-check-artifact/1` record with family
  `acceptance`;
- `regression.json`, the same record format with family `regression`;
- `result-bundle.json`, a canonical `fleet-productivity-results/2` `ResultBundle`.

Each check artifact lists canonical `(trial_id, check_id, authority, disposition)` records. Every
matching `CheckOutcome.evidence_digest` must be the SHA-256 digest of the complete, exact bytes of
its family artifact. The bundle run ID must equal the selected protocol ID and its task must equal
the supplied task. Every arm's ID, kind, adapter, worker, complete environment and fairness
manifests, canonical manifest digests, arm configuration, and disabled components must match its
protocol treatment exactly. Each treatment digest is validated against its predeclared manifest
while loading the protocol, and each result-arm digest is validated against its retained manifest
while loading the bundle; a producer cannot make invented controls acceptable merely by digesting
them consistently.

Only after the command exits zero and every binding validates does the runner add canonical
`command.json` metadata and atomically rename the staging directory to the declared destination.
It records original and resolved argv, protocol and task configuration, explicit cwd, timeout,
network policy, UTC start/end, exit code, stdout/stderr digests, and exact artifact digests. Existing
destinations, path escapes, missing or extra files, stale task/check data, malformed JSON, and
protocol or evidence mismatches fail without publishing the staged artifacts.

### Combine separately collected arms

Each executable protocol above deliberately produces a one-arm bundle. Combine the separately
collected direct Codex and full Fleet arms, then run the existing paired report, entirely offline:

```bash
fleet productivity combine \
  --output ./codex-direct-vs-fleet.json \
  artifacts/codex-direct/result-bundle.json \
  artifacts/codex-fleet/result-bundle.json
fleet productivity validate ./codex-direct-vs-fleet.json
fleet productivity report ./codex-direct-vs-fleet.json \
  --direct-arm codex-direct --fleet-arm codex-fleet --format markdown
```

Combine full Fleet with exactly one ablation the same way; this example isolates review:

```bash
fleet productivity combine \
  --output ./codex-fleet-vs-no-review.json \
  artifacts/codex-fleet/result-bundle.json \
  artifacts/ablation-no-review/result-bundle.json
fleet productivity validate ./codex-fleet-vs-no-review.json
fleet productivity report ./codex-fleet-vs-no-review.json --format markdown
```

Combination accepts at least two canonical, independently collected one-arm bundles and writes
exactly one new canonical `ResultBundle`; it never overwrites a path. Every retained
`TrialResult`, task/arm/environment/fairness/configuration manifest, and check-evidence digest is
copied unchanged. Inputs must have distinct trial and arm identities, the exact same benchmark
version and task identities, identical task/seed/repetition scope, and the worker, environment,
fairness, seed, and repetition controls required by the paired and ablation comparisons. A
noncanonical input or any mismatch fails before the output is opened.

The combined `run_id` (and matching bundle `id`) is `productivity-combined-` followed by the
full SHA-256 canonical digest of the lexicographically sorted source `bundle_digest` values.
Consequently input order cannot change the output identity, while the identity commits to every
exact source bundle. The combined `created_at` is the latest source `created_at`, so no wall-clock
timestamp or false collection time is invented. Retain the immutable source bundles alongside the
combined bundle to resolve and audit that provenance commitment.

Run the cheap per-PR gate against the exact candidate:

```bash
git diff --check
uv run --offline pytest -q tests/test_productivity_evaluation.py tests/test_productivity_cli.py
uv run --offline ruff check src/ai_employee/productivity_evaluation.py src/ai_employee/productivity_cli.py tests/test_productivity_evaluation.py tests/test_productivity_cli.py
uv run --offline mypy
```

Run the scheduled/release gate against the same exact candidate:

```bash
git diff --check
uv run --offline pytest -q
uv run --offline ruff check .
uv run --offline mypy
```

Inspect a successfully collected bundle without granting execution authority:

```bash
fleet productivity validate artifacts/<protocol-id>/result-bundle.json
fleet productivity report artifacts/<protocol-id>/result-bundle.json --format markdown
```
