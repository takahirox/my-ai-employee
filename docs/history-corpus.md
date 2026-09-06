# Private History-derived task corpus

`fleet corpus` reproduces the original Goal and repository starting state, not an
old Planner graph or Worker answer. It complements public benchmarks without adding
Fleet-specific concepts to their core task or adapter interfaces.

Inspection and reporting open SQLite in read-only/query-only mode. They never start
a model, create a workspace, change schema or read artifact bodies. Selection output
contains classifications, logical-task digests, task classes and historical outcomes,
not Goal text, repository paths, credentials or patches.

## Select and inspect

```sh
fleet corpus inspect --history-db /private/history/fleet.db
fleet corpus inspect --history-db /private/history/fleet.db --run RUN_ID
```

Every selected candidate has an explicit reason. Supported classifications include
`REPRODUCIBLE`, `BASE_COMMIT_UNAVAILABLE`, `REPOSITORY_UNAVAILABLE`,
`HARNESS_UNAVAILABLE`, `CONFIG_UNAVAILABLE`, `CONFIG_DIGEST_MISMATCH`,
`EXTERNAL_FIXTURE_MISSING`, `UNCOMMITTED_START_STATE`, `DUPLICATE_TASK` and
`INSUFFICIENT_PROVENANCE`. Registered non-Graph history is explicitly unsupported,
not silently counted as reproducible. Repeated Goal/repository/base combinations
are identified as logical duplicates. Selection order is explicit with repeated
`--run`; otherwise IDs are sorted. Inspect candidate status/replans and assign an
operator-reviewed `--task-class` when selecting a representative corpus.

New invocations retain private `historical_start_v2` records before model calls:
Goal, source commit/clean-state digest, full typed Harness and operator config,
policy, and a source-code fingerprint of the running Fleet package. Existing
authentication remains outside these records: Fleet does not open auth files.
Operator config may include private paths to delegated authentication, not its bytes.
Old runs can be recovered only from exact recorded child workspace snapshots,
base-commit v2 Harness and still-available digest-matching operator/policy content.
Missing configuration is never invented from its digest. Provisional/inferred or
irrecoverable Harness state is unsupported by the initial corpus workflow.

## Attest the environment and export explicitly

Repository and config records do not establish that all external task inputs or
tool versions are available. Supply a private environment declaration. Set `complete`
to true only after auditing the specific task, its external fixtures, dependency
locks, tool/model versions, sandbox/network and credential delegation requirements.
Use the same audited declaration for paired trials. This is **operator attestation**,
not a claim that Fleet recovered an unknown historical environment automatically.

```json
{
  "schema_version": "2",
  "identity": "audited, pinned task environment identifier",
  "external_files": [],
  "complete": false,
  "uncommitted_state_required": false,
  "declaration": "operator_attested_complete_task_environment"
}
```

Each required external regular file belongs in `external_files` as
`{"path":"/absolute/private/fixture","sha256":"<64 lowercase hex characters>"}`.
Never list or copy credentials as corpus fixture content. Empty external files is
valid only when there are genuinely no required external file fixtures. Missing or
changed files and an incomplete declaration reject reproduction.

```sh
fleet corpus inspect --history-db /private/history/fleet.db --run RUN_ID \
  --environment-file environment.json --task-class small-fix
fleet corpus export --history-db /private/history/fleet.db --run RUN_ID \
  --environment-file environment.json --task-class small-fix --output task.json
fleet corpus restore --fixture task.json --directory /private/corpus/task-baseline
```

Export requires one explicitly selected reproducible run and an absent output file;
the new file is mode 0600. Keep its containing directory private and outside public
repositories. Restoring creates an absent, mode-0700 local clone with no hardlinks,
disabled hooks/global Git config, no remote fetch and no model invocation. Original
checkouts and indexes are untouched. Failure retains the exact partial destination
for inspection, never resets/deletes it. Source history and fixture publication are
never automatic. Git state alone cannot restore unavailable submodules/LFS content,
external databases, services or uncommitted files; attest and retain those inputs or
reject the task.

## Run independent trials and compare

Each invocation below starts a fresh Goal/graph. It requires explicit model execution
consent and a separately supplied, exact digest-matching operator config; it never
silently adopts credentials, changes models or retries a batch.

```sh
fleet corpus run --fixture task.json --directory /private/corpus/light \
  --operator-config operator.json --profile lightweight --strategy baseline \
  --run-id trial-light --execute
fleet corpus run --fixture task.json --directory /private/corpus/adaptive \
  --operator-config operator.json --profile adaptive --run-id trial-adaptive --execute
fleet corpus report --fixture task.json --history-db /private/history/fleet.db \
  --run trial-light --run trial-adaptive
```

Use an operator strategy set containing one identical authorized worker/model/effort
baseline for the first profile comparison. Unsupported combinations (including
adaptive routing on a fixed-only isolated backend) fail clearly; the corpus does not
change backend authority to make them run. Preserve all failures, quota outcomes and
incomplete trials. Do not redeem Usage Limit resets, purchase allowance or silently
substitute a model. Rerun with each explicitly selected Fleet version on a fresh clone;
source fingerprints distinguish recorded runtime code, but do not pin provider aliases.

The same fixture supports a minimal-sufficient **guidance** ablation:

```sh
fleet corpus run --fixture task.json --directory /private/corpus/ablation \
  --operator-config operator.json --profile adaptive --minimal-sufficient off \
  --run-id trial-guidance-off --execute
```

Compare with an otherwise identical adaptive ON arm. OFF removes supplemental
minimal-sufficient preferences from Planner/worker instructions and shared simplicity
review guidance. It does not disable accepted scope, repair bounds, checks, review
authority, comment guidance or safety. The choice is persisted and restored on resume;
worker threads receive the same setting. Goal text and authoritative serialization
are not rewritten. It is not an ablation of mandatory scope/acceptance controls.

## Quality-first observations and limits

Reports verify exact independent parent evidence before claiming acceptance, then
show DAG size, patch bytes/touched paths, recovery transitions, recorded model usage,
pre-worker time, active CLI wall time and UTC elapsed time through the latest completed
invocation (including pause/resume waiting). Incomplete timing coverage leaves totals
null with a known completed subtotal. Failed trials retain terminal classification.
Human active time/interventions, changed LOC, unnecessary work, scope drift and later
rework are null unless actually measured by a separate protocol; smaller patches do
not automatically score better. Declared checks do not prove every possible regression.

Comparison marks whether recorded baseline, Harness/checks, policy/budgets, worker
bindings, operator config and explicitly bound environment match. Unrecorded environment
or provider-version equivalence is not inferred. Missing trial/environment bindings
cannot produce a positive controlled-comparison claim. The report makes no automatic
winner declaration. Group separate task reports by `task_class` and preserve selection,
repetition and arm-order manifests.

Follow [the productivity protocol](evaluation.md) for counterbalancing, repetitions,
human stopwatches, independent scope/rework assessments and public-benchmark comparison.
These observations use the same separate metric families; they are not fabricated
`fleet-productivity-results/2` bundles when mandatory human/authority measurements are
unavailable.

Regression tests use disposable Git fixtures and deterministic worker transports for
a typo correction, a medium implementation with compatibility checks, and an API/consumer
change. Lightweight, adaptive and guidance-OFF trials restore the same start and pass
the same independent checks. This establishes wiring/reproducibility, not a live-model
speed, cost, human-effort or quality improvement.
