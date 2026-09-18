# Public Run interface

The supported external automation boundary is the `fleet` CLI. Callers do not need
Engine internals, direct SQLite access or an external system's protocol in the
product package. CLI commands reuse the same runtime used by the Inspector.

Stage invocations expose their recorded contract binding, including shared
`semantics` and invocation-specific `constraints`. Their authoritative owners and
runtime effects are indexed in the [semantic contract audit](semantic-contract-audit.md).

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
missing token/cost measurements must not be converted to zero. `history` returns an
object with a `runs` array. Diagnostic retention is described below; old histories
remain readable, but text already discarded by older versions cannot be recovered.

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

Current Goal requirements, downstream outcomes and unresolved clarification needs contain
`original_refs`,
an ordered list of IDs from `original_source.fragments`. `source_evidence` in Run
projections contains their exact runtime-derived `fragments` for display, including
pending human questions. Separate fragments are separate quotations, not an implied
contiguous excerpt. The original remains unchanged, including line endings and
Unicode. A source ID proves location, not interpretation. Old quotation-based
histories remain inspectable; `stage-contract-6` rejects older histories' execution rather
than migrating references or resetting invocation budgets.


`goal.specification.criteria` are Fleet's verified completion conditions.
`goal.specification.downstream_outcomes` preserves explicitly authorized post-handoff
requirements: description, external owner, `after: "handoff"`, original references and
linked artifact criterion IDs. `status: "completed"` means Fleet's scope was verified;
it does not assert those downstream outcomes occurred. Promotion publishes verified
bytes; the receiving actor owns later execution. Inspectors display downstream
requirements separately, and source evidence includes their exact original fragments.
These fields survive journal replay; callers must not infer external completion from
an executable deliverable. Existing histories remain readable but cannot resume under
the new contract semantics.


### Snapshot and workspace capacity

New `fleet init` configurations explicitly write `snapshot_max_bytes: 536870912`
(512 MiB) and `isolation.workspace_mb: 1024` (1 GiB). Set them in the JSON file or
use `fleet init --snapshot-max-bytes <bytes> --workspace-mb <MiB>`. Snapshot bytes
must be a positive integer; the existing workspace range is 16–4096 MiB.

Configurations without `snapshot_max_bytes` retain 64,000,000 bytes. Isolation
profiles without `workspace_mb` retain 256 MiB. Existing saved Run identities are
preserved, and resume/result/promotion use the saved allowance, not a subsequently
edited configuration file. Change the operator configuration before starting a
new Run; do not edit a persisted Run to increase its allowance.

The snapshot limit counts regular-file contents once per selected path (including
identical files) and the bytes of each symlink's stored target. It applies to input,
materialization/validation, container transfer/recovery, and published candidates.
Transfers containing read-only upstream snapshots count their content too. Git
input selection and reserved metadata exclusions are unchanged. Temporary and
new files in the workspace remain candidate content; `.gitignore` does not filter
them. The separate 10,000-entry limit remains unchanged (see #224).

The archive byte ceiling is derived as the content allowance plus 16,000,000 bytes
for tar headers, padding, path metadata, and other archive overhead. This preserves
the former metadata headroom without the old fixed 80 MB ceiling. It is enforced
on both archive creation and receipt and is not a separate operator setting.

Workspace storage is independent: `/work` is a bounded tmpfs. Raising the snapshot
allowance does not enlarge it. Input content exceeding the workspace capacity
fails with `WORKSPACE_SIZE_LIMIT`; filesystem metadata, dependencies, temporary
files, and build outputs need additional headroom even when that check passes.
The default 1 GiB is not a guarantee for a complete development environment.
Prepared dependencies may reside in the approved image; this feature does not
install, relocate, filter, or automatically clean dependencies. In particular,
approximately 451 MB of Node dependencies plus 3.17 GB of Ruby dependencies do not
fit in a 1 GiB workspace with the reported 202 MB input.

Byte-overflow diagnostics retain the `INPUT_SIZE_LIMIT`, `CANDIDATE_SIZE_LIMIT`,
or `CANDIDATE_TRANSPORT_SIZE_LIMIT` code and include `limit_bytes`,
`observed_bytes`, and `count=partial|complete`. A partial count describes only the
traversal/read prefix observed at rejection, not the complete input size. Reads
stop at the remaining allowance plus one byte. No files are silently truncated,
no limit is automatically increased, and no input paths or file contents appear
in these size diagnostics. Larger allowances permit greater memory/disk use;
packing and validating snapshots can hold multiple copies of content in memory,
and tmpfs use also counts against container memory. Configure
`isolation.memory_mb` and workspace headroom for the workload. Docker uploads
use a private temporary file, removed on completion or failure, so host temporary
storage also needs room for one bounded archive per concurrent upload.


`fleet init` leaves `limits.wall_seconds`, `limits.active_seconds`,
`limits.invocation_seconds` and each check's `timeout` unset (`null`). Omission has
the same meaning: no hard time limit for that scope. Set positive seconds explicitly
when required; the smallest applicable remaining limit bounds a call. For example,
`"wall_seconds": 7200` limits the Run to two hours. `approval_counts_wall` affects
only a configured wall limit. `active_seconds` sums concurrent invocation durations,
not elapsed Run time. `supervision_seconds` is an observation interval, not a timeout.
Cancellation, usage/attempt limits and mandatory verification still apply.

Budget includes `elapsed_seconds` (time since Run creation) and
`active_seconds_charged` with live open-call timing even without time limits.
After an interrupted controller, reconciliation closes open calls with a conservative
time charge through cleanup, recorded as `reservation_recovered` / `upper_bound`;
it does not report unknown provider usage as zero or clear uncertain external effects.
Existing submitted Runs retain their snapshotted explicit limits; changing a template
does not change a submitted Run's policy.

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


## Diagnostic retention

Investigation uses the existing private history database, public `inspect` / `logs`
projection and authenticated Inspector. `stage_diagnostics` includes diagnostic-only
records alongside existing validation classifications. The Inspector renders their
text, redaction counts and truncation state; it does not interpret text as HTML.

For new invocations the runtime retains completed structured model responses before
contract validation or acceptance: clarification proposals, plans, review summaries
and finding evidence, Worker results, selection and verification responses. This
includes rejected proposals and each repair attempt. Schema-invalid responses retain
recognized top-level schema fields and error paths/types; unknown top-level fields
and non-object payloads are omitted with explicit counts/flags. Successful native
streams are not persisted as transcripts; enabled command capture stores bounded
snapshots separately. A failed invocation or protected check
additionally records `execution_failure`, linked to its Run, stage and reservation.
Native stdout/stderr tails are redacted before being limited to 32,768 bytes each.
Parsed native item bodies (including command and reasoning text) are omitted from
stdout/stderr failure tails; command snapshots use the separately controlled store below.
Incomplete JSON event lines are also omitted. Other non-JSON transport messages and
provider errors may remain as bounded excerpts. Authentication files, host launch argv/stdin and
unrelated host files are not inspected or collected.
A process killed before capture cannot guarantee a complete response; this is not
a transcript of every internal model/tool action.

Execution snapshots include whether transport streaming started, observed stdout/stderr byte
counts, the last parsed event's type/tool status, the last read time in seconds since
stream reading began, and transport/native exit codes when known. An incomplete non-JSON
transport line may appear without a parsed event; incomplete JSON is marked omitted. Native exit codes are reported
only after guarded final accounting succeeds. An event showing tool completion
followed by no final response identifies an observed waiting interval, not its
provider-internal cause. Missing observations are unknown; no extra network-log
collection is performed after a deadline. Already recorded network observations
remain separate and their absence does not prove no communication occurred.
`started` reports transport startup, not proof that the model/provider started.

The native layer carries sanitized snapshots across exception boundaries without
depending on Journal. Engine persists them through the existing private diagnostic
path, including up to eight entries in cleanup exception chains, stopping at the
invocation's caller exception boundary and marking truncation when needed. Only
exception types are retained, not arbitrary exception
messages or tracebacks. Process stop/reap precedes failure snapshot processing.
Diagnostic capture/storage failures do not replace the active control exception,
skip settlement, or turn successful work into failure. These records do not change
timeout, cancellation, Usage Limit, retry, acceptance or cleanup decisions.

Readiness failures include the Task ID/digest, proposed Plan digest, authority
request/ceiling, unsupported boundary fields, security mode, criteria and eligible
external checks. Protected check results include exit code and stdout/stderr;
check exceptions retain their existing `check_failed` classification. Any partial
native output appears separately in `execution_failure`, not as a successful check receipt.
Check receipt digests and deterministic acceptance rules remain unchanged. Correlate
model responses with existing stage invocations by reservation and contract identity;
review targets and response digests connect proposals with their reviews.

Each event's `record.text` is sanitized JSON, capped at 1,000,000 UTF-8 bytes.
`format: json` describes the original serialization; truncation can leave an invalid
JSON prefix. `original_bytes`, `stored_bytes`, `redactions`, `truncated` and the original
SHA-256 digest make transformations visible. Once accumulated diagnostic event bodies
reach 16,000,000 bytes for a Run, subsequent payload text is omitted. Remaining
capacity can truncate the event that crosses the threshold. Small omission metadata
and up to 8,192 bytes of sanitized linking context per event are still retained, so
this is a payload retention threshold, not a total database file-size quota. Existing
runtime invocation/attempt limits still bound event production. These append-only diagnostic events have no automatic
expiry; caller-owned state remains available after `cleanup`. Separate command bodies
have the expiration/purge policy described under [Command diagnostics](#command-diagnostics).

Common credential fields and text patterns (authorization/cookies, password/API and
access/refresh tokens, private-key blocks, Bearer/Basic credentials and recognized
provider tokens) are replaced before truncation. This is best-effort credential
redaction, not general personal-data anonymization: ordinary task text, filenames,
review evidence and check output can remain sensitive. The database is mode 0600;
keep its state directory and exported CLI JSON private. Inspector authentication
is required for data access. Do not publish diagnostic exports without reviewing
retained content. Diagnostic records are never accepted Goal/Plan/Candidate inputs,
permission grants, retry instructions or authority evidence, even after a Run stops.


## Durable token usage

`fleet status` exposes `budget.usage_details` with `invocations`, `stages`, and
`total`. Each invocation is keyed by its reservation and stage, and records the
selected backend, model, and effort before model execution (including failed
attempts). Older records without that identity expose nulls; they are not relabeled
using the current configuration. Successful historical stage results can supply
identity when present.

Usage fields are `tokens`, `input_tokens`, `output_tokens`, `cached_input_tokens`,
`cache_creation_input_tokens`, `reasoning_output_tokens`, and `cost`. In the current
Codex path, input includes cache reads/writes and output includes reasoning:
`tokens = input_tokens + output_tokens`. Do not add cache or reasoning subsets
again. Input minus cached input gives input not served from the read cache; any
reported cache writes remain a separate pricing distinction. Missing, invalid,
or unreported measurements remain null, never an assumed zero. The existing
Claude total-count helper keeps its separate additive cache convention; Claude
native execution is currently unavailable and this change does not enable it.

The native adapter treats Codex `turn.completed` usage as an invocation snapshot:
the latest snapshot replaces the earlier one. Observation and final return are
copies of the same measurement, not additional usage. Distinct retry reservations
are counted separately. Cleanup does not remove usage history. If a controller
crashes before settlement, its last durable observation remains visible with
`settled=false` and, after reconciliation, `recovered=true`; it does not establish
complete final usage or release the conservative reservation charge.

Every aggregate field has `value`, `observed_sum`, and `complete`. `value` is null
unless all constituent invocations are settled and report that field.
`observed_sum` sums available snapshots, but is not a complete bill. Empty groups
sum to zero. These measured aggregates do not replace `admission_charges` or change
budget reservation, cancellation, or recovery policy. Legacy totals remain readable,
with unavailable breakdown fields null. No database rewrite is required.

Usage is reported by the backend, not measured independently by Fleet. These
counts allow comparisons such as different models and reasoning efforts, but do
not prove actual subscription charges. A cost estimate must retain its currency,
price source/version or effective date, model, and applicable pricing conditions
(including cache and long-context/service-tier rules). This feature neither fetches
price tables nor converts token counts to money. `cost` remains unknown unless
supplied by the adapter. Provider fields absent from its usage report cannot be
reconstructed from the total.

## Command diagnostics

New `fleet init` configurations enable bounded, redacted command capture. Use
`fleet init ... --no-command-capture` to disable it, or edit the configuration
**before submitting a new Run**:

```json
"command_capture": {
  "enabled": true,
  "command_bytes": 65536,
  "run_bytes": 4000000,
  "max_commands": 1000,
  "retention_days": 7
}
```

Configurations without this field remain readable with their original digest and
leave capture disabled. Adding settings does not reconstruct past commands. Settings
are snapshotted with the Run; do not edit persisted Run configuration to enable capture.

Inspect or export diagnostic JSON without launching a model or applying authority:

```sh
fleet --state /path/to/state commands RUN_ID
fleet --state /path/to/state commands RUN_ID --stage worker --failed
umask 077
fleet --state /path/to/state commands RUN_ID --reservation ATTEMPT_ID > commands.json
fleet --state /path/to/state purge-commands RUN_ID
```

Records identify the Run, Stage, invocation reservation, Task/worker attempt when
applicable, and provider command ID. Task and attempt are unknown for stages without
that context. Provider IDs are scoped to each reservation; identical IDs in a later
invocation are distinct. Duplicate snapshots do not repeat output or move timestamps.
Only the latest snapshot is retained, and completed records ignore later updates.
An invocation that ends without a command-completed event is explicitly incomplete;
Fleet does not invent its exit code or claim that the command succeeded.

`command`, `cwd`, `stdout`, `stderr`, and `combined_output` carry field-level
availability, redaction counts, truncation reasons, and observed byte counts. Empty
reported output is distinct from unavailable output. Codex exec normally supplies
`aggregated_output`; it appears as `combined_output`, not an invented split into
stdout/stderr. A provider-reported command cwd is distinct from the known `/work`
session workspace; Fleet does not infer it by parsing shell commands. Capture consumes
complete command snapshot events (`item.started`, `item.updated`, `item.completed`),
not arbitrary output deltas or model messages/reasoning. If a provider emits no partial
snapshot before interruption, those unreported output bytes are unavailable.

Start/end timestamps and `observed_duration_seconds` measure **controller event
reception**, which may be buffered; they are not precise process execution times or
model inference latency. `provider_duration_seconds`, when supplied, is converted
from the provider's `item.duration_ms` and is separately labeled. Missing start events
leave duration unknown. Wall-clock timestamps and monotonic reception intervals have
different meanings and are not substituted for one another.

### Limits, retention, and confidentiality

The command byte limit covers each stored JSON snapshot, including metadata; the Run
byte limit covers the sum of those snapshots, not SQLite page overhead, tombstones,
or other existing diagnostic records. Each text field gets up to one tenth of the
command limit so one output stream cannot consume all space needed by the other
fields and correlation metadata. The command ID is capped at 256 bytes. If the Run
budget leaves only metadata space, text is omitted with a truncation reason. Missing
IDs and byte/count exhaustion increment separate omission counters. Limits are
transactional across concurrent workers and persist across resume/reopen. Counts also
include expired/deleted command tombstones so late snapshots cannot bypass the cap.

Command snapshots live in separate, non-authoritative SQLite tables in the private
history database, rather than the append-only decision journal. Expiration clears
bodies on command capture/inspection; there is no background timer. Retention starts
at a command's first stored observation and updates do not extend it. Explicit purge
clears bodies for the Run and prevents later snapshots from repopulating it. Tombstones
retain only the reservation, Stage, opaque key, and removal metadata. These operations
do not rewrite the decision hash chain, erase accounting, or change execution status.
SQLite secure deletion is enabled, but exports, backups, filesystem snapshots, and
already-existing diagnostics are outside this purge's scope; manage their retention
separately. Stop the process or use an external maintenance schedule if expiry must
occur while Fleet is idle.

Capture redacts recognized credential keys/tokens, authorization values, private keys,
URL credentials, and common secret command arguments **before** truncation and storage.
This is best-effort pattern matching, not a guarantee that arbitrary business data,
encoded secrets, or unknown credential formats are safe. Disable capture for sensitive
work that cannot tolerate this limitation, and review exports before sharing. No extra
environment, auth-file, network-body, or process-introspection collection is performed.
Native failure snapshots omit item bodies so the command store's retention and disabled
setting are not bypassed by a second raw command copy. Existing model-result/check
reports remain separate diagnostics and may quote their own findings.

The `commands` result includes filtered `capture_failures` metadata when persistence
was unavailable, rather than presenting missing records as complete evidence.
Diagnostic capture/storage failure emits bounded metadata when possible and must not
replace quota, cancellation, cleanup, or settlement behavior. `commands` is diagnostic
only: its content never grants authority, drives retries, or proves acceptance or
external success. Old histories have no reconstructable command bodies.

## Unverified partial workspaces

`fleet partials RUN_ID` lists failed worker capture outcomes. When the supervisor
confirms worker stop/reap and the container remains available with controller
ownership and budget, Fleet attempts one bounded extraction before disposal.
Ordinary nonzero exit and invalid responses can therefore leave a saved snapshot.
Timeout, cancellation, Usage Limit, transport failure or controller loss do not
promise recovery; `unavailable` records explain missing prerequisites. A failed
transfer or unsafe/oversized snapshot is `capture_failed`, never a saved baseline.

Use an artifact's reservation ID to export it explicitly:

```sh
fleet export-partial RUN_ID --reservation RESERVATION_ID \
  --destination /absolute/new-unverified-directory
```

The response includes `verified: false`, Run/task/attempt/reservation identity,
failure category and the immutable tree digest. The destination must be new;
existing paths are refused. `inspect` also includes `partial_artifacts`. These
records survive environment cleanup and history reopening. They are separate from
Candidate lineage: `result`/`promote` still require verification, and execution does
not automatically reuse partial trees. Saved files do not resolve uncertain external
effects or cleanup failures.

Failure extraction uses a maximum 30-second Docker-control deadline, capped by the
remaining invocation deadline and cancellation/ownership checks. Local archive and
storage work uses the existing snapshot byte and 10,000-entry limits; it performs
no model calls or native worker restart. File contents use the ordinary snapshot
safety rules, including safe relative symlinks and excluded control files; contents
are unverified workspace bytes, not redacted command logs. No additional credentials
or host files are collected. A controller crash or inaccessible storage can prevent
retention. Original failures and unconditional cleanup take precedence over capture.
