# Configuration and recovery

`fleet init` creates a JSON Run configuration. `schema_version` is `autonomous-1`.
There is one runtime; this identifier does not select a compatibility path.

Each LLM-backed stage has the same policy fields: backend/model/effort,
`review` (`never`, `always`, `conditional`), review conditions, reviewer backend/
model/effort, revision count and optional supervision interval. Stages are
`clarification`, `planning`, `selection`, `worker`, `verification`, and `recovery`.
A fixed worker is the default. `worker_options` allows selection among configured
choices; `worker_escalations` supplies ordered alternatives after ordinary failure.
A Usage Limit never triggers selection, escalation or another provider.

`checks` contains immutable operator command definitions; `mandatory_checks` binds
those IDs to Goal acceptance. Worker-authored tests can contribute evidence, but
cannot replace these commands. The default template enables clarification review.
Unresolved questions produce `WAITING_FOR_CLARIFICATION`; answers are separate
events and do not rewrite Original Input.

`limits` includes Run wall time, aggregate active time, per-invocation time,
invocation count, per-task attempts, added tasks, replans and concurrency. Optional
token/cost limits use transactional reservations across all stages. Missing usage
is unknown, not zero. See [security.md](security.md) for the difference between
admission reservations and provider-enforced spend caps.

`approval_counts_wall` explicitly controls whether durable approval waiting counts
toward the wall deadline. Waiting never holds an active worker invocation open.
A request records a proposed authority version; approval records intent; native
application must succeed before APPLIED is recorded. Resume never treats APPROVED
as APPLIED. Rejection, cancellation and unknown external effects are durable states.

Ordinary worker or verification failure retains a workspace where safe, then uses
configured escalation or a forward graph extension within the same remaining Run
budget. Existing tasks retain exact definitions. New repairs use new IDs and may
identify a superseded task. Accepted upstream work is reused with exact lineage;
completed history is checked again before publication.

`fleet revise` is an explicit human replacement, not an automatic recovery step.
It creates a linked new Run, preserves both original inputs, invalidates publication
of the predecessor, and requires fresh clarification and verification. Stopped or
uncertain Runs cannot use this route to restart automatically.

`--state` selects a journal/object/workspace directory; its default is
`~/.fleet/autonomous`. Old history databases are rejected. `fleet inspect` and
`fleet logs` read the event journal, and `fleet serve` opens the read-only Inspector.
Record which optional native/container/live checks actually ran when reporting
validation; ordinary unit tests do not establish live-model quality.

`revisions` bounds output attempts beyond the initial call, including proposal
content revisions. `transport_retries` defaults to zero and separately permits
bounded timeout/connection retries after adapter cleanup. Both counters survive
controller restart and consume the shared limits. Arbitrary environment/invariant
failures, denied authority, quota and uncertain external effects are not response
repair. Inspector exposes these distinctions alongside contract and target digests.
Only the supported backend/version set is executable; a syntactically representable
`claude` setting is rejected before model execution, including in a later stage.
