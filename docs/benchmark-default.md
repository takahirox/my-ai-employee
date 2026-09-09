# Default configuration benchmark connection

`fleet-bench-default REQUEST.json` runs ordinary adaptive `fleet work` defaults
inside an explicitly prepared disposable Pocket Agent Bench task container. It
expects `/app`, `/opt/pocket` public checks, `/opt/pocket/guarded-codex`, and the
explicitly delegated container-local login. It is not a host sandbox or a replacement
for the separate isolated `fleet-bench` host controller.

The request's remaining seconds include setup and patch export. The connection
deducts setup and a bounded return reserve before assigning Fleet's wall budget.
Product stdout/stderr stream directly to the trial's log files, preserving terminal
evidence during interruption. The accepted patch is applied only to the disposable
task for independent grading. Operator history must never be mounted into this fixture.

For new, nonempty LF text files, the Codex edit transport accepts `files` instead
of `unified_diff`: an array of `{path, content}` objects matching the exact ordered
`paths` declaration. Runtime code produces the new-file diff. Mixed representations,
duplicates, traversal, NUL/CR content, and oversized inputs are rejected. Existing
files still require an ordinary edit diff; new-file proposals cannot overwrite
them. Every compiled change retains the normal policy, path, evidence, and promotion
checks. Full benchmark success must be measured separately from model-free tests.

The connection emits an empty `pocket.usage` status event with a `complete` flag
from persisted model invocation records. An interrupted invocation leaves usage
incomplete even when the controller returns normally for grading. Numeric subtotals
continue to come exclusively from native usage records; the status event adds none.

## Failure diagnosis after a trial

The default connection allocates an explicit run ID before starting `fleet work`.
A read-only collector snapshots that run and children bound by persisted worker
requests and node execution records into `/logs/agent/fleet-diagnostic-bundle.json`
every two seconds and once when the process returns or raises. Each update replaces
the file atomically. The benchmark host must retain this log directory outside the
disposable container; this connection cannot recover a removed log volume.

The bundle includes safe structural projections of boundary and model-process
diagnostics, node attempts, repair transitions, watchdog/cancellation facts, parent
review requests/results/failures, candidate descriptors and verification evidence.
It includes creation times and database row order within the snapshot, plus request,
response, candidate and evidence digests. Row order is an observation order, not an
immutable event ID; use references and attempt/generation fields to establish
causality. Runtime identifiers are consistently SHA-256 pseudonymized (`ref-…`) so
references can be matched without exporting arbitrary identifier text.

Parent review failures retain a separate `parent_review_failure_v2` record without
changing the existing aggregate failure code or decision. JSON syntax, strict schema,
criterion coverage, node coverage and other response-contract failures have distinct
codes. Other caught review failures retain their stage and normalized exception type.
The record links the semantic request and candidate, and the model response digest
when parsing or binding was reached. Counts describe expected and received coverage;
no raw exception string is recorded. These diagnostic records have no execution or
replay authority and read-only collection never invokes a model.

Arbitrary strings, prompts, response prose, candidate source bodies, raw exceptions,
artifact bodies and unlisted fields/kinds are omitted. Safe schema/coverage
classifications and references remain available after the database is deleted;
this is not a full source reproduction archive. Per snapshot, collection is bounded
to 256 related runs, 10,000 records, 1 MB per record, 16 MB scanned payload and an
8 MB output ceiling. Projected arrays retain at most 256 items and nesting is capped
at 12. The bundle declares these omissions and limits, and marks record/scan/run
truncation. A collection failure retains the last successful bundle and writes
`fleet-diagnostic-export-error.json` when the output medium is available. A later
successful snapshot does not erase that error marker.

`state: running` means the exporter has not recorded a final snapshot; it does not
prove the process is still alive. `controller_finished` means the controller's
subprocess returned, including unsuccessful outcomes; consult the persisted run
status and failure codes. `interrupted` means an exception exited the collection
scope. A hard kill can preserve only the last snapshot: uncommitted records and
records written since that snapshot may be missing. Unknown or omitted termination
causes must not be inferred as timeouts or cancellations.

The existing `fleet-diagnostics.json` summary now uses the same related-run,
body-free projections, retaining usage reporting and exposing omission metadata.
The separate existing `fleet-result.json` and `fleet.stderr` streams are controller
output, not this sanitized diagnostic format. Docker and live-model benchmark
success must still be verified separately from the deterministic collector tests.
