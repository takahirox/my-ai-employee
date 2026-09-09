# Run fact ownership audit

This resolves the bounded audit and implementation scope of [#155](https://github.com/takahirox/my-ai-employee/issues/155).
The inspected baseline is `aa25265d625f1b8189128d0c0503b0e700e5a62f`.
It includes #152–#154, which were absent from the issue's initial local review
baseline `6091ee0`. Historical failures below are not asserted to remain reproducible.
The implementation children are [#156](https://github.com/takahirox/my-ai-employee/issues/156)
(live run allowance) and [#157](https://github.com/takahirox/my-ai-employee/issues/157)
(WorkRun checkpoint and uncertain effect recovery). A final transport review added [#158](https://github.com/takahirox/my-ai-employee/issues/158)
(common CLI proposal attribution). All three are in scope for this change.

## Representation and recovery rules

**A** denotes authority: the domain decision, its scoped durable records and authorized
update path. **P** denotes a mechanically derived view or bounded dispatch snapshot.
**H** denotes immutable evidence of an earlier decision/observation. **D** denotes an
independent decision or writer that this audit found should be removed.
A fact may have several records without several authorities. In particular, a
reservation is not consumption, concurrency is not cumulative process admission,
lease expiry is not active run time, and candidate readiness is not promotion approval.

The tables describe current ownership, including the changes identified below.
“Retain” means no further duplicate-authority change was demonstrated in this audit;
it is not a proof of all possible task outcomes or backend behavior. Each test file is
under `tests/`; each source path below is under `src/ai_employee/` unless stated otherwise.

## Ownership map

| Fact and scope | Authority / writer -> readers; representation and recovery | Disposition / scenario evidence |
|---|---|---|
| Original accepted Goal, per accepted graph revision | A: accepted Goal and `TaskGraphAcceptance` in `task_orchestration.py`; CLI/acceptance -> planner, worker, review. H: accepted graph and digest-bound request; P: transport alias when local and original goals match. Read exact accepted records on resume, never recover Goal text from a planner summary. | Retain #95/#129/#141; `test_task_orchestration.py`, `test_revisioned_replan.py`, `test_work_orchestration_v2.py`. |
| Local node scope, per graph/node generation | A: accepted Graph/Node via graph acceptance; scheduler -> WorkerRequest and node evaluator. Repair feedback narrows execution through accepted transitions; original Goal is context, not extra authority. H: prior node/request revisions. | Retain; `test_closed_loop_orchestration.py`, `test_task_orchestration.py`. |
| Completion criteria, per accepted Goal/node | A: typed CompletionCriterion and Harness-bound checks (`goal_acceptance.py`, `task_orchestration.py`); acceptance -> worker, evaluator, reviewers. P: worker contract is derived; H: criterion evidence. Resume preserves criteria and verification bindings. | Retain #95/#152; `test_issue83_goal_acceptance.py`, `test_candidate_validation.py`, `test_http_validation.py`. |
| Candidate identity, per node output / parent composition | A: `graph_execution.py` accepted child result and `graph_composition.py` composition; runtime -> evaluator/promotion. H: exact descriptor, patch, workspace lineage and candidate digests. Content-equivalent requests deduplicate; source/generation bindings still validate. | Retain #109/#115; `test_graph_composition.py`, `test_workspace_lineage.py`, `test_graph_execution.py`. |
| Proposal/request IDs, per correlated invocation | A: runtime transport attribution in `worker_adapters.py`, `worker_attribution.py`; adapters -> policy/services/store. H: attributed records are reused, not re-attributed on replay. Semantic content digest is distinct from transport ID. | **Changed #158**: retain Codex behavior and share attribution with Claude/Ollama; `test_runtime_proposal_ids.py`, `test_issue85_attribution.py`. |
| Timestamps / run-worker attribution | A: runtime clock/identity at correlated acceptance; external timestamps remain observations. H: persisted metadata; replay does not consult a fresh clock to recreate it. | **Changed #158**: same common correlated acceptance point; `test_runtime_proposal_ids.py`, `test_issue85_attribution.py`. |
| Permissions / effective policy, per accepted configuration and action | A: deterministic policy composition and request-bound PolicyDecision (`domain/policy_v2.py`, `domain/policy.py`); operator/Harness/runtime -> adapters/services. Separate service and sandbox enforcement remains necessary. H: policy digests and approvals; changed input requires revalidation. | Retain; `test_policy.py`, `test_controlled_services_v2.py`, `test_issue81_isolation.py`. |
| Required and executable capabilities | A: accepted node requirements, configured backend support, effective policy; `cli.py`, graph acceptance and `worker_observation.py` derive bounded dispatch settings. P: worker capability/observation bindings; effects are enforced by services/native sandbox. H: request and preflight diagnostics. | Retain #152/#153; `test_worker_observation.py`, `test_task_orchestration.py`. Preflight cannot establish arbitrary future external availability. |
| Execution profile / adaptive path, per run | A: immutable `ExecutionProfile` and `AdaptiveExecutionDecision`; selection -> scheduler, reporting/resume. These are distinct facts: adaptive may choose a direct path without becoming lightweight. Missing historical decisions retain legacy behavior. | Retain #145/#146; `test_adaptive_execution.py`, `test_execution_profile.py`, golden `fixtures/adaptive-decision-legacy.json`. |
| Remaining active wall time, per logical run | A: `run_budget.py` RunWallBudget projected from bound start/finish receipts plus injected monotonic interval. Scheduler/services read it. H: timing/reservation snapshots; D removed: node-created-at fallback clock and copied admission remainder. Storage reads live authority inside the write transaction; parent repair also enters the same accounting scope. | **Changed #156**; `test_authority_boundaries.py`, `test_run_wall_budget.py`, `test_shared_wall_admission.py`, `test_live_worker_allowance.py`. |
| Process / worker / artifact budgets, per graph and node attempt | A: `storage.py` atomic graph reservation, `process_budget.py` mediated admissions, `artifact_budget.py` content admissions. Scheduler reserves; service boundaries consume; P: WorkerRequest ceiling. H: admissions survive failed/uncertain dispatch; replay does not refund or charge again. Native descendant containment is separate from mediated launch counts. | Retain existing transactions; #156 changes only the live wall input. `test_process_budget.py`, `test_artifact_budget.py`, `test_issue59_worker_supervision.py`. |
| Attempt / retry / repair / replan counts | A: scheduler's accepted loop transitions and graph generations (`task_orchestration.py`); routing/recovery reads counters folded from those records. H: original timeout result/context and feedback. No new attempt derives from elapsed time alone. | Retain #99/#125; `test_timeout_recovery.py`, `test_closed_loop_orchestration.py`, `test_revisioned_replan.py`. |
| Cancellation / pause, per logical run and child | A: persisted controls, scheduler-owned lifecycle; P: scoped StageCancellation and propagated child stop signals. Parent terminal transaction checks cancellation; draining pause has distinct semantics. Controls do not overwrite original results. | Retain, with live time gate #156; `test_terminal_cancellation.py`, `test_stage_control.py`, `test_graph_execution.py`. |
| Ownership / lease, per graph generation/execution attempt | A: `storage.py` atomic acquisition/heartbeat/closure and current owner row; scheduler is sole live writer, services only poll. H: acquisition, heartbeat, closure and recovery records. Transactional row determines eligibility; stale generations cannot regain authority. | Retain fences; #156 refreshes lease observation inside terminal transaction. `test_issue57_run_ownership.py`, `test_authority_boundaries.py`. |
| Verification requirements / evidence bindings | A: accepted criteria, evaluator specifications and exact CandidateRevision; `domain/evaluation.py` deterministic freshness/decision -> node/parent acceptance. H: EvaluationEvidenceLedger; P: explanations. Replay revalidates persisted evidence without running checks again. | Retain; `test_evaluation_v2.py`, `test_eval_framework.py`, `test_graph_typed_results.py`, `test_parent_review.py`. |
| Task / graph / parent completion | A: scope-specific evidence-gated transitions. `WorkCoordinator` owns child WorkRun; `TaskOrchestrator` owns GraphRun; GraphCandidateEvaluator supplies bound parent evidence. D removed: separately authored WorkRun checkpoint copies in coordinator and CLI. Checkpoint is now P, atomically generated by `save_work_run`; run_status events remain H, not competing state. | **Changed #157**; `test_authority_boundaries.py`, `test_work_orchestration_v2.py`, `test_graph_execution.py`, `test_graph_execution_patch_repair.py`. |
| Promotion authority | A: `PromotionApprovalTrustKernel` and exact source/patch/policy/approval binding; CLI/workspace applies authorized promotion. A parent PASS is necessary evidence, not an approval. H: decision/result; projection never grants publication authority. | Retain; `test_graph_execution.py`, `test_workspace_lineage.py`, `test_policy.py`. This audit does not add automatic external-effect reconciliation. |
| Model/provider usage | A: provider observation persisted by UsageRecordingExecutor (`model_usage.py`); reporting folds records with completeness/source identity. H: invocation facts; P: subtotal/aggregate. Missing provider usage remains incomplete; wrapper exit status cannot invent totals. | Retain #131/#152; `test_model_usage.py`, `test_benchmark_default.py`. |
| Replay / resume identity and effect outcome | A: accepted graph/configuration/route/candidate plus WorkRun and action-start evidence. `WorkCoordinator.resume` reads them; completed action projection skips committed work. A start without committed completion is explicit `ACTION_OUTCOME_UNKNOWN`, including when a result exists but acceptance did not commit. Terminal outcomes persist on repeated resume. | **Changed #157**; `test_authority_boundaries.py`, `test_work_orchestration_v2.py`, `test_adaptive_execution.py`, `test_history_corpus.py`. |

All durable projections retain their old serialization. WorkRun and its checkpoint
are committed together; a fault between the two SQL writes rolls back both. Existing
inconsistent checkpoints still fail closed; this change does not silently migrate or
repair operator history. Read-only inspection/replay never calls `save_work_run`.

## Historical incident classification

These classifications use the issue/PR descriptions and the corresponding source/test
boundaries listed here. They distinguish the demonstrated mechanism from unknown
causes of a particular live-model failure. Linked PR validation reports are historical
reports, not checks rerun by this audit.

| Incidents and fixes | Mechanism / assessment | Current source and evidence |
|---|---|---|
| [#95](https://github.com/takahirox/my-ai-employee/pull/95) | Planner transport bounds diverged from accepted time/retry limits; Goal context lost. Duplicate authority/semantic derivation. | `worker_adapters.py`, `task_orchestration.py`; planning and WorkerRequest contract tests. |
| [#96](https://github.com/takahirox/my-ai-employee/pull/96) | Assessment stopped lease polling; reviewer reinterpreted already-bound verification. Mixed lifecycle ownership and semantic guidance. | supervised assessment and plan review; `test_issue57_run_ownership.py`, `test_plan_review_adapters.py`. |
| [#99](https://github.com/takahirox/my-ai-employee/pull/99) | Adapter timeout and scheduler watchdog selected different recovery paths for the same result. | shared bounded timeout recovery; `test_timeout_recovery.py`. |
| [#100](https://github.com/takahirox/my-ai-employee/issues/100), [#101](https://github.com/takahirox/my-ai-employee/issues/101) -> [#105](https://github.com/takahirox/my-ai-employee/pull/105) | Parent finalization outlived owner; Task Review lacked lease supervision. Distributed lifecycle authority. | `graph_execution.py`, `stage_control.py`; owned finalization and slow-review scenarios. |
| [#102](https://github.com/takahirox/my-ai-employee/issues/102) -> [#106](https://github.com/takahirox/my-ai-employee/pull/106) | Independent stage/invocation clocks reset run allowance. | `run_budget.py`; cumulative, pause, crash and legacy interval tests. #156 removes residual secondary observations. |
| [#103](https://github.com/takahirox/my-ai-employee/issues/103) -> [#107](https://github.com/takahirox/my-ai-employee/pull/107) | Reservations existed but dispatch consumed no cumulative allowance; concurrency is a different fact. | `claim_node_process`, WorkCoordinator admission; competing connections and reopen tests. |
| [#109](https://github.com/takahirox/my-ai-employee/issues/109) -> [#115](https://github.com/takahirox/my-ai-employee/pull/115) | Equivalent composition requests recreated during resume appeared ambiguous. | content-bound deduplication in composition/evaluation; parent pause/resume scenarios. |
| [#110](https://github.com/takahirox/my-ai-employee/issues/110), [#119](https://github.com/takahirox/my-ai-employee/issues/119) -> #115 / [#121](https://github.com/takahirox/my-ai-employee/pull/121) | Pipe EOF/leader exit incorrectly implied process/group completion. Lifecycle implementation defects, not simply duplicate counters. | `services_v2/process.py`; actual bounded process and descendant tests. |
| [#111](https://github.com/takahirox/my-ai-employee/issues/111) -> #115 | Cancellation preflight raced with success publication. Atomicity defect despite a common store. | `terminalize_owned_graph_run`; `test_terminal_cancellation.py`. #156 adds live deadline/lease observation. |
| [#112](https://github.com/takahirox/my-ai-employee/issues/112) -> #115 | Normal finish and recovery independently settled one interval. | `run_budget.py`; conservative maximum once, including legacy overlap fixtures. |
| [#113](https://github.com/takahirox/my-ai-employee/issues/113) -> #115 | Writing outputs did not consume accepted artifact reservation. Missing enforcement at an authority boundary. | `artifact_budget.py`, `claim_node_artifact`; cumulative/resume/content-deduplication tests. |
| [#114](https://github.com/takahirox/my-ai-employee/issues/114), [#117](https://github.com/takahirox/my-ai-employee/issues/117), [#120](https://github.com/takahirox/my-ai-employee/issues/120) -> #115 / #121 | Isolated verification, browser/download and internal Git omitted shared control. | `isolated_execution.py`, services, `services_v2/_common.py`; `test_service_wall_deadlines.py`, `test_internal_git_supervision.py`, isolated fake-transport tests. |
| [#116](https://github.com/takahirox/my-ai-employee/issues/116) -> [#118](https://github.com/takahirox/my-ai-employee/pull/118) | Routing history confused resumed lifecycle generation with retained result generation; capacity failure with model quality. Derived-history identity/classification defect. | `routing_history.py`; retained PASS counted once under exact original bindings. |
| [#122](https://github.com/takahirox/my-ai-employee/issues/122) -> [#123](https://github.com/takahirox/my-ai-employee/pull/123) | Concurrent legacy imports mixed losing/winning start/finish identities. | atomic `put_legacy_wall_import`; competing SQLite writers and interrupted insert tests. |
| [#124](https://github.com/takahirox/my-ai-employee/issues/124) -> [#125](https://github.com/takahirox/my-ai-employee/pull/125) | Additive time reservations competed with shared active time; malformed diff and lost progress were separate transport/diagnostic defects. | live deadline admission, bounded new-file transport and progress; no assertion that all observed stalls shared a cause. |
| [#126](https://github.com/takahirox/my-ai-employee/issues/126) -> [#127](https://github.com/takahirox/my-ai-employee/pull/127) | Worker advertised original ceiling despite smaller actual attempt allowance; planner regenerated deterministic metadata. | timeout-bound request/context projection, compact bounded schema; `test_live_worker_allowance.py`. |
| [#128](https://github.com/takahirox/my-ai-employee/issues/128) -> [#129](https://github.com/takahirox/my-ai-employee/pull/129) | Single-node paraphrase could replace requested deliverable. Semantic duplication; not proof of the whole observed timeout cause. | exact single-node Goal normalization before review; multi-node decomposition retained. |
| [#132](https://github.com/takahirox/my-ai-employee/issues/132) -> [#133](https://github.com/takahirox/my-ai-employee/pull/133), [#136](https://github.com/takahirox/my-ai-employee/issues/136) -> [#137](https://github.com/takahirox/my-ai-employee/pull/137) | Model regenerated runtime-owned IDs/timestamps/attribution with invalid formatting. | attributed Codex transport, legacy metadata replacement before unchanged content validation; `test_runtime_proposal_ids.py`. |
| [#147](https://github.com/takahirox/my-ai-employee/issues/147) -> [#152](https://github.com/takahirox/my-ai-employee/pull/152), [#153](https://github.com/takahirox/my-ai-employee/pull/153) | Task observation needs mismatched offline worker; later preflight assumed exact proxy-port equality. Capability contract gap plus specific environment assumption. | candidate-copy observation, policy-bound hosts, native proxy preflight. No universal backend availability claim. |
| [#148](https://github.com/takahirox/my-ai-employee/issues/148), [#149](https://github.com/takahirox/my-ai-employee/issues/149) -> #152 / [#154](https://github.com/takahirox/my-ai-employee/pull/154) | Smoke-only criteria failed to cover behavior; review interpreted inherent observation scope as a defect. Coverage/configuration and semantic-contract issues, not solved merely by a new state store. | `goal_acceptance.py`, candidate/public HTTP checks, parent review. Hidden graders remain independent; unsupported public checks remain unverified. |
| [#150](https://github.com/takahirox/my-ai-employee/issues/150), [#151](https://github.com/takahirox/my-ai-employee/issues/151) -> #152 / #153 / #154 | Lower-level diagnostics disappeared at export/filtering; raw output cap reproduced as a possible cause. Original live failure causes remain unknown. | `model_usage.py`, `benchmark_default.py`, `inspector.py`; real-record export tests. Historical missing information cannot be reconstructed by this refactor. |

The counterexamples matter: source capture (#98), transport serialization (#153),
verification coverage and missing failure observations do not imply another authority
service is needed. #108/#118 already derive routing history from accepted runtime facts,
rather than maintaining a second mutable performance counter.

## Design decision and migration

Use existing scoped records and atomic projections. Do **not** add a second Run Event
Ledger in this change. Its only demonstrated new benefits would overlap the existing
wall intervals, ownership chain, loop transitions, admission records and work events.
Making another log authoritative while leaving their writers intact would add cutover,
ordering and dual-write hazards. No persisted schema migration is needed for #156/#157/#158.

The research comment's recommendations are addressed as follows:

- Semantic model activities remain outside deterministic acceptance. Move Codex-local
  proposal metadata allocation to the shared correlated CLI response boundary. Claude's
  schema already omitted these fields but its decoder did not supply them; Ollama's
  generic schema still requested them. All CLI workers now receive runtime metadata
  from one producer. Generic schema metadata is omitted; exact process correlation,
  cancellation, content validation and legacy non-mutating bindings remain enforced.
  Already stored records are not re-attributed on replay.
- Run control reads one active allowance. SQLite reservations and terminal publication
  consult that live allowance after entering their write transaction. Clocks, lease
  fencing, local watchdogs and cancellation keep their distinct meanings.
- WorkRun checkpoint creation moves from coordinator/CLI to `save_work_run`. The
  existing checkpoint shape is a transactionally maintained compatibility view of
  WorkRun, not a replay log. Both writers now call the same producer.
- Existing capability preflight and service/sandbox enforcement are retained. A model
  requirement or a recorded capability does not grant authority.
- Existing scope-specific completion rules and evaluation ledgers are retained.
  Candidate evidence, parent completion and promotion approval cannot be collapsed
  into one generic Boolean reducer without losing their different authority.
- Existing WorkRun action-start receipts and committed completion are enough to stop
  uncertain resume. No new provider idempotency protocol is invented. A failed outcome
  is not reset to running merely because resume was requested.
- History corpus task reproduction is distinct from deterministic lifecycle replay.
  New disposable SQLite/fake-worker scenarios complement existing legacy route,
  evaluation-ledger and parent pause/resume tests in ordinary CI.

The acceptance instant for a terminal write is its live eligibility check under the
SQLite write lock. A cancellation already committed then wins over budget exhaustion;
a later control write cannot retroactively undo an accepted completion. The same lock
serializes concurrent non-time reservations. Snapshot remaining values are diagnostic,
never permission for a future admission. No context budget means scheduler programming
error, not a fresh allowance; low-level legacy reservation callers without a live
budget retain additive reservation semantics.

For interrupted effects, the conservative continuation is `ACTION_OUTCOME_UNKNOWN`.
A process may have changed state even if its result or checkpoint never committed.
Resume preserves the start, any result/artifacts and consumed admissions, and performs
no further dispatch for that run. It does not claim exactly-once effects. An operator
can inspect retained evidence and explicitly reconcile a separate continuation within
its authorization; this change adds no blind replay or automatic refund. Completed
and failed terminal WorkRuns remain unchanged by repeated resume.

## Limits and validation

This is a finite audit, not a proof of correctness of every plug-in or external service.
No additional duplicate-authority implementation gap was established for the retained
rows. Historical causes lacking original diagnostics (#150/#151) remain unknown; their
missing evidence is explicitly outside the resolved architecture scope. Full event
sourcing, automatic external-effect reconciliation and new capability backends are
not required follow-ups for these demonstrated gaps. The shared metadata transport change is model-free; no
claim of live Claude/Ollama quality or availability is made.

The regression suite uses disposable databases and synthetic nonsecret fixtures.
`test_authority_boundaries.py` exercises late terminal publication, admission after
preflight expiry, atomic checkpoint rollback/reopen, and crash after an actual local
marker effect both before result persistence and before completion publication.
Existing scenarios cover ordinary pause/resume, legacy wall imports, immutable adaptive
record digests, concurrent process admissions, evidence freshness and cancellation.

Run the checks defined in [development.md](development.md) and CI. Actual check outcomes
are reported in the resolving PR. Unit and fake-transport success does not establish
Docker isolation, live-model quality, performance gains or external idempotency.
