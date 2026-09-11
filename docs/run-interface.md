# Public Run interface

The supported external automation boundary is the `fleet` CLI. Callers do not need
Engine internals, direct SQLite access or an external system's protocol in the
product package. CLI commands reuse the same runtime used by the Inspector.

## Create, execute and observe

Use an explicit private state directory and operator-approved configuration:

```sh
fleet --state /absolute/state submit 'Create the requested result' \
  --config /absolute/policy.json --root /absolute/input
fleet --state /absolute/state resume RUN_ID
fleet --state /absolute/state status RUN_ID
fleet --state /absolute/state logs RUN_ID
fleet --state /absolute/state result RUN_ID
fleet --state /absolute/state promote RUN_ID --destination /absolute/new-result
```

`submit` snapshots the goal, policy and input and returns the Run ID without running
models. `resume` executes synchronously; the caller owns that CLI process. There is
no daemon or guarantee of progress after the caller kills it. The persisted Run ID
can still be inspected from another process. `work` combines submit and execute;
`revise` creates a linked replacement Run and executes it. Both print the new ID,
flushed before execution, then a final projection or error.

`status`, `inspect` and `logs` return the same Run projection: status, events,
stage_diagnostics, stage_invocations, budget, goal, plan and cleanup. Events are ordered and carry timestamps. Budget contains measured usage and unknown usage;
missing token/cost measurements must not be converted to zero. A stored diagnostic
may be only a classification/digest; the interface does not reconstruct discarded
model text. `history` returns an object with a `runs` array.

`result` returns `candidate` and `candidate_digest`, after validating final Goal
verification, lineage, stored file hashes and publication authority. It does not
publish files. `promote` publishes those verified bytes to a new directory and
records the Candidate digest and destination in a `promoted` event. It refuses
existing destinations, unverified results and revoked/stopped/uncertain Runs.
Callers decide their own output layout and copy-back rules; Fleet never overwrites
the supplied source workspace. Concurrent explicit lifecycle changes may invalidate
a result between inspection and promotion; promotion revalidates it.

## JSON and exit codes

Except `serve`, help and argument-parser errors, stdout is newline-delimited JSON
objects with `contract_version: "fleet-run-1"`. Each line is a complete object;
`work` and `revise` can produce multiple lines. Inspect objects keep their fields at
the top level; command errors contain `error` and, when known, `run_id`.

Exit 0 means the requested operation succeeded. For `work`, `resume` and `revise`,
it specifically requires completion. Exit 2 means an operation failed or execution
did not complete; inspect the durable status/events to distinguish waits, failures,
budget exhaustion and usage limits. A successful `status` command can report a
failed Run. Parser errors use argparse's stderr text and exit 2. Unexpected process
termination may produce no final JSON; retain the early Run ID and reconcile.

The contract is versioned for caller validation. Ignore unknown additive fields,
but reject unsupported contract versions. Configuration uses the documented
`RunConfig` schema; `fleet init` generates a template. Models, checks, authority,
limits and the explicit delegated authentication path are snapshotted at submit.
Runtime APIs inside the Python package remain implementation details, not a second
promised integration interface.

## Durable interaction and resource ownership

```sh
fleet --state /absolute/state answer RUN_ID 'Explicit clarification answer'
fleet --state /absolute/state authority RUN_ID ATTEMPT_ID --decision approve
fleet --state /absolute/state authority RUN_ID ATTEMPT_ID --decision reject
fleet --state /absolute/state cancel RUN_ID
fleet --state /absolute/state cleanup RUN_ID
```

Answer/approval commands record input; resume explicitly after a resolvable wait.
Authority application remains distinct from approval. `cancel` persists a stop;
it is not confirmation that every owned resource is released. Cancellation and
usage-limit stops cannot be used to reset a Run's budget or resume it automatically.

`cleanup` stops unfinished work, acquires the ordinary controller lock, and
reconciles only Fleet-owned runtime resources using their durable ledgers. It
never calls a model, reads model credentials to authenticate, removes Run history,
or deletes caller workspaces. A busy controller yields `RUN_ALREADY_OWNED` with
cleanup pending; its cancellation observer shuts down work. After that controller
exits, repeat cleanup. Do not report release as confirmed merely because the CLI
process exited or a cancellation request was accepted.

The projection's cleanup state is `not_requested`, `pending`, `unconfirmed` or
`confirmed`. Confirmation describes the last cleanup; new reserved activity
invalidates it. Failed cleanup retains its evidence and can be repeated. Existing
completed/failed/stopped/uncertain outcomes are preserved by cleanup, including
publication of still-authorized completed results. Reconciliation cannot resolve
uncertain external effects or release their logical authority leases merely by
removing a container. Explicit cancellation/revocation can still invalidate an
otherwise completed result.

Fleet owns its Workers/containers/process namespaces. Evaluators own their own
execution environments, temporary input/output directories and protocol records.
Collect diagnostics and confirm Fleet cleanup before removing caller-owned state;
retain recovery information when release is unconfirmed. A caller's timeout,
protocol or export failure must be recorded separately from the Run's outcome and
linked by its Run ID.

## Connecting external systems

An evaluator reads this interface and its own Agent interface and supplies the
minimal translation when needed. Protocol parsing, input layouts, public-check
construction, result export and evaluator response formatting stay outside Fleet.
Checks must be explicitly supplied as ordinary operator-owned RunConfig checks;
no named integration injects them inside the runtime. Keep evaluation instructions
and permitted checks separate from private graders/solutions.

No permanent integration package or plugin framework is required. Retain an
adapter in evaluator-owned tooling only when repeated use warrants maintenance.
Evaluate connections after product-specific glue is removed, record the tested
versions/conditions, and distinguish protocol success from task success. Model-free
contract tests do not establish live-model quality or isolation.
