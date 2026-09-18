# Architecture

Fleet owns the Goal, Plan, scheduling, environment grants, budgets, immutable
Candidates, verification and history. The native worker owns its internal work.
There is no action proposal vocabulary and no replay of implementation edits.

`models.py` defines frozen, strict contracts. Original Input remains unchanged.
Clarification records success criteria, exact source references, assumptions
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

Work time limits are opt-in: omitted/null wall, cumulative active, invocation and
check durations impose no hard work deadline. `Journal.remaining_wall` owns the
wall-time definition, including the union of approval waits when policy excludes
them. Admission caps a call by the minimum of the configured remaining budgets;
it rechecks under the reservation lock. Active time sums settled actual durations
and elapsed time of every open call. A call never reserves future active seconds
exclusively, so an active-only budget admits parallel work. Live cancellation checks
the same shared sum; all calls stop at exhaustion (subject to process polling and
cleanup latency). Token/cost admission reservations and invocation counts remain
unchanged. Resume keeps the original clock and charges. After exclusive ownership
and confirmed resource reconciliation, interrupted reservations close with elapsed
time through reconciliation charged as a conservative upper bound, explicitly marked
`reservation_recovered`. Unknown token/cost charges and external-effect leases remain.

Preflight and native setup consume the invocation's existing deadline. The model's
execution budget is reduced before dispatch, with bounded `execution_budget`
observations recording the post-preflight and native-dispatch allowances. Protected
checks and authority application use the same reservation rule. A Run stop takes
precedence over transport retry classification; interrupted external operations
still retain uncertainty. Late check output remains diagnostic evidence and cannot
become an accepted check receipt after expiry. Mandatory verification, configured
review and cleanup are unchanged; no fixed completion reserve or extra budget is
introduced. These rules align deadlines, not guarantee task completion within them.
Inspector budget data exposes elapsed and charged active seconds even when limits
are unset. Optional supervision observes progress without imposing a work deadline.
Docker API operations, individual native probes, ownership-watch startup and cleanup
retain finite control-plane timeouts; they do not bound the productive session.

`stage_contracts.py` binds each invocation to `stage-contract-6`, its exact input
snapshot/context, Run policy, registered checks and evaluation target. Pydantic
owns structural and proposal-local constraints; the binding projects existing
references into native provider schemas, prompts, validation and repair feedback.
A new DAG can define its own local IDs. Review findings must cover exactly the
review/verification criterion namespace. Recovery cannot redefine historical tasks.

Clarification uses structured `unresolved` needs whose meaning is owned by
`models.py`: `human_input` requires a concrete question, a relevant original
reference, reason and investigation evidence; `investigation` enters existing
bounded response repair; `environment` reports a blocker and stops after acceptance.
Only accepted human-input needs cause durable `clarification_wait`. Pending
inspection and ordinary deferred work do not become questions just because they
are unresolved. Mixed needs are repaired first, then environmental stops take
precedence over human waits. Goal acceptance uses the same disposition.

`source_refs.py` owns Original Input reference semantics. Runtime splits the exact
original into line-preserving fragments, grouping consecutive lines when needed to
keep at most 128 IDs. Requirements, unresolved needs and downstream outcomes select ordered, nonempty,
unique `original_refs` (such as `s1`, `s2`); models do not reproduce quotations.
StageContract binds the source digest and exact fragment map to every invocation;
provider schemas constrain all source reference fields to those IDs. The same resolver
validates clarification and Goal acceptance, supplies exact separate quotations to
clarification review, and derives CLI/Inspector source evidence from stored original
input. No whitespace or Unicode normalization or semantic segmentation is applied.
IDs are local to the bound original: the same short ID in another Run resolves only
to that Run's source. Original Input provenance and contract-version checks protect
resume; a valid reference does not establish that its criterion/need is semantically
supported. That remains the role of independent review.

The same definitions supply field descriptions/provider schema, clarification and
review context, admission and repair feedback. Review remains governed by Run
policy; no classifier call or new retry budget is added. Both clarification and its
review see a bounded inventory from their actual immutable input snapshot. The
native adapter identifies its execution workspace separately from paths in the
original request. An inventory is not content-inspection evidence, and reported
model evidence does not independently prove file absence or classify arbitrary
natural language correctly. Existing semantic review, native preflight and
protected verification remain distinct safeguards. Environment reports retain
bounded diagnostics and never request an implicit permission expansion.

Stage-contract-6 rejects older journals before resuming: changed completion semantics
are not silently applied to accepted Goals. Older journals remain inspectable, including
source evidence for stage-contract-5 histories. Fresh malformed model responses use the
existing bounded output repair path.

`Clarification.clarified_goal` retains the user's final intent. Its `criteria` describe
only Fleet-owned completion conditions: evidence must be available before completion
and promotion. When Original Input explicitly authorizes execution after handoff,
`downstream_outcomes` retains the outcome, external owner, fixed `after=handoff` timing,
source references and links to artifact criteria for the verified deliverable. Links
must be unique, existing artifact criteria; they cannot relabel external-effect
criteria. No Task/Criterion-wide scope field or deferred execution state is added.
Independent review assesses original authorization and timing; verification inspects
the actual deliverable against the linked downstream requirements. The schema cannot
prove arbitrary natural-language authorization or program correctness. Fleet does not
execute or attest the downstream outcome; directly requested Fleet execution cannot be
silently weakened to delivery.

The `LIFECYCLE` definitions in `semantics.py` are consumed by Engine's existing
verification/acceptance/completion/promotion boundaries and projected through
StageContract. The lifecycle inventory comes from actual invocation inputs, including
recovery evidence and each invocation's snapshot; presence does not prove inspection.
Review identifies whether it assesses proposal feasibility or actual Candidate evidence,
without inferring a global before/after-work phase. Verification and its review share
one context containing Goal/Task scope, WorkerResult, Candidate, current checks and
accepted upstream receipts. The completion and downstream definitions are shared by
schema descriptions, generation, review, verification and repair.

Clarification's shared execution-check applicability exempts only `disposition=stop`
from mandatory-check coverage and protected external-evidence availability. Unknown
checks, original references, mappings and other structural rules still apply. A stop
must pass configured review and can never become an accepted Goal or start work.
Mixed needs use the existing disposition precedence; wait/proceed retain admission
checks. A missing execution capability is explained without output-repair exhaustion,
not treated as permission to execute.

`semantics.py` owns the shared evidence, outcome, Worker status, Finding and graph
meanings used by model descriptions, StageContract projections, validation and
runtime actions. Recovery generation and review receive the same historical Tasks,
failed dependencies and remaining growth allowance. Inspector exposes the recorded
binding, including these meanings and constraints. The
[semantic contract audit](semantic-contract-audit.md) traces consequential inputs
through their owners, producers, validators, runtime consumers and regression tests.

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
uses the same contract identity and durable reservation counter. Older contract journals
are rejected rather than giving their invocations fresh identities and accidentally
resetting counters; no migration is provided.

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

## Input preservation evidence

An explicit input-preservation criterion uses `preserved_paths` to name canonical
workspace-relative files or directory subtrees (no globs). The default exact directory comparison
includes additions, deletions, content and executable flags; `.` selects the whole
workspace. Each selection must contain a file in the initial snapshot. Empty
directories are not represented by the existing Candidate store. Criteria without
an input-preservation requirement leave this field empty.

Fleet compares the Run's saved initial snapshot with the exact Candidate using
authenticated manifests. The bounded comparison report is runtime-owned and bound
to the Run, initial tree, Candidate (including attempt and upstream lineage), Goal,
Task, policy and criterion paths. Verification and its optional review receive the
same report through their existing shared context. A missing or mismatched reference
is a runtime contract failure; actual differences prevent acceptance even if an LLM
reports success. The result Task must retain the Goal's declared paths in planning
and recovery. Natural-language interpretation of which inputs the user requires
preserving remains part of clarification and its review.

The verification event retains the report. Resume and publication recompute it
against the immutable snapshots before reusing a successful verdict. No Worker
baseline, extra model call or external check is needed. Equality proves only the
two snapshots agree, not that no temporary change occurred during execution.
Stage contract 7 introduces this field and evidence boundary; older histories remain
inspectable but cannot resume under the new contract.

Preservation criteria may explicitly set `preservation_mode: "existing"` to protect
only entries selected from the immutable initial Run snapshot, allowing additions.
The default `"exact"` mode continues to reject additions as well. Both modes reject
content, executable-bit, symlink-target and entry-type changes or deletions; neither
proves absence of transient writes. For example, selecting `spec/factories` and
`spec/support` in existing mode protects all their initial entries without enumerating
them, while permitting new support files. Normal snapshot safety and capacity limits
still apply. Planning and recovery must retain both selections and modes; comparison
evidence binds the mode and publication recomputes it. Default exact criteria retain
their historical serialization and digests.
