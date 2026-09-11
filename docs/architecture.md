# Architecture

Fleet owns the Goal, Plan, scheduling, environment grants, budgets, immutable
Candidates, verification and history. The native worker owns its internal work.
There is no action proposal vocabulary and no replay of implementation edits.

`models.py` defines frozen, strict contracts. Original Input remains unchanged.
Clarification records success criteria, exact requirement fragments, assumptions
and unresolved questions. Configured review compares that specification against the
original; unresolved clarification cannot reach planning. Mandatory check IDs refer
only to operator-owned commands in the snapshotted Run configuration.

`engine.py` schedules one Planner's DAG. Ready independent tasks run concurrently;
integration is an ordinary task and can occur at multiple convergence points.
Multiple upstream snapshots are available under `.fleet-inputs/<index>`, read-only
inside the native sandbox. A successful task freezes its actual workspace in
`candidates.py`. Every Candidate binds its task definition, attempt, upstream
Candidate digests and authority version.

Verification uses fresh copies of those exact bytes. Operator checks each get a
separate copy; verifier review also starts from the original Candidate rather than
a verifier-modified workspace. Task acceptance does not imply Goal acceptance.
Publication checks the adopted graph and exact lineage again, then materializes the
verified snapshot to a fresh destination. It does not execute worker edits again.

Recovery preserves a task's workspace where safe, supports configured worker/model/
effort escalation, and can extend the graph with new task identities. Historical
Task definitions cannot be rewritten. Repaired upstream results require new
downstream work; old downstream Candidates keep their original bindings. The Run
budget and graph-growth limits apply to every extension.

`history.py` owns an append-only, digest-checked SQLite event journal and transactional
reservations. A controller lock prevents simultaneous controllers for one Run.
Resource leases coordinate overlapping coarse external write grants across Runs;
unknown outcomes do not release those leases by inference. Approval REQUESTED,
APPROVED and APPLIED events are distinct. Owned container ledgers support cleanup
before continuation and revocation.

`container.py` reuses the existing Docker namespace and native process guard.
Workers execute in the native Codex sandbox inside that namespace. The guard stops
and reaps task descendants before workspace capture, including detached processes.
The model gateway carries model traffic and permitted HTTPS destinations while
recording bounded metadata. It does not interpret arbitrary encrypted operations.

`cli.py` and `inspector.py` consume these same runtime facts. There
is no legacy runtime, compatibility profile router or database migration subsystem.

## Stage contracts and execution readiness

`stage_contracts.py` binds each invocation to `stage-contract-2`, its exact input
snapshot/context, Run policy, registered checks and evaluation target. Pydantic
owns structural and proposal-local constraints; the binding projects existing
references into native provider schemas, prompts, validation and repair feedback.
A new DAG can define its own local IDs. Review findings must cover exactly the
review/verification criterion namespace. Recovery cannot redefine historical tasks.

The authority projection also carries field meanings, unsupported product controls,
Run ceiling/security rules and registered external evidence routes. `capabilities.py`
owns these admission rules; native schema descriptions, planner/recovery/reviewer
context, Stage validation, readiness and repair read the same facts. Requirements
remain expressible even when infeasible: schema descriptions explain unsupported
values instead of forcing a needed control to false. Invalid proposals enter the
existing output revision budget before acceptance; runtime and environment failures
retain their stop/wait behavior. External Goal evidence routes cannot be replaced
by artifact-only Plan criteria. This structural check does not prove equivalence
of arbitrary natural-language requirements; configured semantic review and final
protected evidence checks remain necessary layers.

Authority repair feedback retains bounded Task/field/rule context in the existing
rejection event; full rejected proposals stay in bounded diagnostic storage. Resume
uses the same contract identity and durable reservation counter. The version bump
rejects prior stage-contract-1 journals rather than giving their invocations fresh
identities and accidentally resetting counters; no migration is provided.

Malformed output and invalid references enter bounded response repair. Reviewer
output faults remain in the reviewer; exhausting that repair is review unavailable,
not a finding that the proposal is incorrect. Content rejection goes back to the
proposal stage. Original inputs are available for factual investigation before
asking a human. Non-worker invocations receive fresh copies of the exact input
snapshot, so review/repair cannot inherit mutations to the evaluation files.

Reservations persist the contract binding, logical call identity, counter and start
intent before process launch. Output attempts and optional transport retries have
separate durable counters and share the Run budget. Reopening the controller does
not refund an uncertain launch or reset its limits. No automatic environment or
provider fallback is enabled for capability failures. Existing configured worker
selection/escalation remains subject to product and Run policy checks; quota never
selects another model/provider. External-write output/transport uncertainty blocks
all ordinary continuation and retains resource leases. This adapter cannot recover
trusted external receipts from malformed output, so it stops for reconciliation
rather than attempting a response-only repair or repeating the external work.

Authority-changing journal appends check cancellation/revocation and contract
version in the same SQLite write transaction. Approval application is serialized
with the controller. Accepted Candidates keep exact task/authority/upstream
bindings; accepted work is reused after a crash before downstream scheduling.
Duplicate identical acceptance/usage delivery is idempotent; conflicting usage is
rejected. Late observations and usage may be recorded after stop without accepting
the late result. Journals created without the current stage contract are not
silently migrated or reinterpreted.

`product_capabilities.py` publishes supported backend/version facts to schema,
CLI and preflight. All configured stages, reviewers and worker alternatives are
checked before the first model call. Each invocation performs model-free image,
CLI version, check-executable and native-boundary preflight using its selected
configuration and authority; probes and model calls share the reservation/deadline.
The invocation rechecks its actual new environment. Probe results are diagnostic
records, never cached authority or a substitute for runtime enforcement. Delegated
authentication presence is checked, but successful provider authentication/model
access cannot be guaranteed without a model request.

Planning records each criterion's check/review method, evidence/plan digest,
dependencies, policy and authority binding. Natural-language/research criteria can
use independent artifact review. Dependencies are future prerequisites, not already
observed evidence. The scheduler requires exact accepted upstream Candidates;
pending authority requests require APPLIED, not APPROVED. A remote-effect Task
needs an operator-owned verification check before starting; generic model prose
cannot attest remote success. Preflight never performs the external operation.

CLI and Inspector project the same journal, including repair failures, fixed review
categories, preflight/readiness records, invocation counters and unknown usage.
Completed model proposals and reviews are captured before validation/acceptance,
including rejected plans and review summary/evidence. Readiness failures retain the
Task, requested authority, policy and failure location; protected checks retain
stdout/stderr separately from their acceptance receipt digest. These bounded,
redacted `diagnostic` events are explicitly non-authoritative, including when added
after a stop. Replay, acceptance and permission decisions do not read them. The
existing invocation/reservation records link each response and repair attempt; no
additional trace store or execution path is introduced. See
[diagnostic retention](run-interface.md#diagnostic-retention) for limits and omissions.
Public contract tests, native isolation tests and live-model evaluation
remain distinct validation levels (see development and isolated-worker guides).
