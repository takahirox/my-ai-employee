# Semantic contract audit (#197)

This is the baseline reverse audit for `stage-contract-4`. Start at consequential
Engine, Journal, Candidate and native-adapter decisions, then trace their inputs
back to generation. This table is an index to executable owners, not another
definition of their meanings. Changes must repeat the reverse review in
[development-workflow.md](development-workflow.md).

The shared sources are [semantics.py](../src/ai_employee/semantics.py), local
validators and clarification definitions in [models.py](../src/ai_employee/models.py),
and authority definitions in [capabilities.py](../src/ai_employee/capabilities.py).
[StageContract](../src/ai_employee/stage_contracts.py) binds those definitions to
the actual invocation. [native.py](../src/ai_employee/native.py) projects provider
schemas; [engine.py](../src/ai_employee/engine.py) consumes validated values.

## Reverse traceability

“Updated” includes newly shared meaning or missing producer constraints. “Audited”
means the existing owner and trust boundary remain in place. Test names below are
in `tests/`; abbreviated module names have the `test_` prefix.

| Consequential input / decision | Authoritative owner → producer projection | Review / deterministic validation → runtime consumer | Regression evidence / conclusion |
| --- | --- | --- | --- |
| Original fragments, criterion IDs and mappings → Goal acceptance/rejection | `models.Requirement`, `Clarification`; `semantics.RULES` → field descriptions, bound Original Input and criterion/check namespaces | Same original in clarification review; local uniqueness/mapping validators and `StageContract.validate` exact-fragment checks → `_clarify` | Updated: `clarification_contract`, `stage_contracts` cover foreign references, repair and immutable Goal boundaries. Natural-language completeness still requires configured semantic review. |
| Clarification need kind, question, reason, evidence and fragment → repair/stop/human wait | `CLARIFICATION_NEEDS`, `CLARIFICATION_RULES`, `Clarification.semantics()` → schema and both clarification/review bindings | Need validators, bound fragment validation, `disposition` → `_clarify`, Goal acceptance | Audited: `clarification_contract` covers investigation repair, environment stop, actual human questions, mixed-needs priority and replay. |
| Optional/mandatory protected checks, required evidence, verification plan → proposal rejection / later verification | `EVIDENCE`, Criterion/Task descriptions; operator `RunConfig.checks` → shared semantics, registered IDs and mandatory-check binding | Same evidence timing in `_review`; `StageContract._checks` and mandatory coverage → readiness and `_verify` | Updated: `semantic_contracts` real Engine fixture accepts empty optional artifact checks and future evidence with reviews enabled; `authority_contract` and `stage_contracts` retain mandatory/registered-check negatives. Covers #192. |
| Criterion outcome / Check evidence kind → external-evidence admission and acceptance | `OUTCOMES`, `external_evidence`, `AUTHORITY_RULES` → field descriptions and stage/authority projections | `Criterion.requires_external_evidence`, `Check.proves_external_effect`, `task_violation`, Goal preservation checks → readiness and `_verify` protected receipts | Updated: `stage_contracts` tests `artifact_check_cannot_establish_external_goal_even_for_offline_worker`, `explicit_external_evidence_route_is_ready_without_future_evidence`, and failed external receipts; artifact substitution remains rejected. |
| Worker status + authority request → verify/retry/request/uncertainty/quota stop | `WORKER_STATES` and local WorkerResult relationship validator → status/request descriptions and worker/reviewer/verifier semantics | Same action table in validator and `WorkerResult.action` → `_task`, completed-attempt resume; malformed negative safety reports still stop/wait before repair | Updated: `semantic_contracts` covers every status/request combination, local/external action differences, malformed quota/uncertain responses without another call, bounded repair and replay. |
| Worker evidence, summaries, reasons, assumptions and feedback → review/verification/revision | `EVIDENCE` claims/worker-evidence definitions and existing original/target binding → producer and reviewer context | Claims cannot mint receipts, policy or identity; independent Candidate inspection and protected checks → acceptance | Updated: full Engine tests in `semantic_contracts`; `autonomous_runtime` architecture canary and external-completion tests. No semantic classifier or claim-based acceptance introduced. |
| Authority fields, resource uniqueness, host syntax, supported controls and ceiling → reject/request/readiness | `Authority`, shared host pattern, `AUTHORITY_RULES`, `authority_projection` → field descriptions, schema restrictions and all applicable stage bindings | Same local/policy/support rules validate plans and Worker requests; feasibility checked before durable wait → `_authorize`, readiness/native preflight | Updated: `authority_contract` projects every runtime rejection and exercises repair; `semantic_contracts` checks host schema and repair. Unsupported or out-of-ceiling requests cannot become grants. |
| Authority request → human approval → native application | Runtime-owned versioned authority events and `Authority.within`; shared `WORKER_STATES` request semantics → model sees request as proposal, bound ceiling and actual applied context | Explicit approval and native application remain separate; Journal locks, resource leases and revocation fence publication of grant → `_task` resume and native dispatch | Audited: `autonomous_runtime` authority approval/application failure, cancellation, request rollback and remaining-budget tests. Approval/application events are not model output and cannot be repaired into existence. |
| Finding ID/count/category/passed/evidence → review rejection or Candidate verification | `FINDINGS`, `FINDING_CATEGORIES`, Finding validator; bound expected IDs → schema cardinality, ID enum and nested category/pass alternatives | Same category table validates combinations; exact coverage validation → `Verification.accepts`, review revisions, verification rejection and existing recovery | Updated: `semantic_contracts` tests all category/pass combinations, four review findings, expected/received feedback and end-to-end repair. Category is diagnostic, not a second recovery dispatcher. |
| WorkerChoice index/reason → worker selection | `SELECTION`, operator options and bound maximum index → schema bound, selection/review context | Local lower bound and bound upper limit → `_select_worker` | Updated: `semantic_contracts` proves producer/reviewer share the bound and foreign choices are rejected; quota cannot authorize a different provider. |
| Task IDs, dependencies, kind, result_task → scheduling/integration/final verification | `GRAPH`, Task/Plan validators → field descriptions and planner/reviewer/recovery bindings | Plan DAG/coverage validation; exact accepted dependency Candidates → `_execute`, materialization, Goal verification | Updated: `autonomous_runtime` multiple integrations and stale-upstream resume tests; `semantic_contracts` graph tests. Kind remains an intent label using the existing execution path. |
| supersedes, historical definitions, new IDs and failed dependencies → repair/replan/adopted graph | `GRAPH`, `StageContract` historical/failed/growth constraints → identical full recovery context for producer and reviewer | Historical equality, known supersedes, at least one new Task, growth allowance and failed dependencies checked before review → `_recover` adoption | Updated: `semantic_contracts` checks valid replacement and invalid growth/foreign target/failed dependency; `autonomous_runtime` failed Candidate repair and replan-limit tests. Removed duplicate late graph checks. |
| Candidate tree, Task/attempt digest, upstream lineage, applied authority version → freeze/accept/resume/publication | `Candidate`, `AttemptContext`, Candidates and Journal own identity; `GRAPH.candidate` → shared model-visible trust boundary; no writable identity in WorkerResult | Exact snapshot/lineage checks, independent verification and accepted event identity → `_accepted`, completion, promotion | Audited and meaning projected: architecture canary, stale-upstream identity and external-completion-crash tests in `autonomous_runtime`. A model cannot replace runtime identity or publish by claiming completion. |
| Check exit/receipt, accepted verification and completion events → acceptance/publication | Operator Check configuration, protected native execution and immutable Candidate binding → shared evidence distinctions; model receives observations as evidence | Journal-owned receipts and exact Candidate identities, all executed checks pass → `_verify`, completion and Candidates promotion | Audited: public Run/canary and external receipt negatives. Generated Findings remain assessments, not protected receipts. |
| Stage policy, limits and reservations → admission/review/retry/escalation/replan/stop | Operator `RunConfig`/`StagePolicy`/`Limits`, Journal reservation counters → bound policy digest, configured model/options and execution budget; recovery growth projected explicitly | Existing policy methods and locked counters, settled usage and elapsed time → generation/review admission and stop checks | Audited: runtime replan/quota/shared-budget tests and stage-contract repair/transport budget tests. Model output cannot increase limits or reset counters; no added calls on the simple path. |
| Provider usage/transport failure, native preflight, output cap, cancellation → stop/retry/uncertainty | Native provider/control protocol, `Usage`, Run budgets and shared authority preflight → native dispatch configuration and execution budget | Native parsing/preflight and Journal accounting → bounded transport retry or terminal stop, preserving external uncertainty | Audited: process quota detection, cleanup failure, native preflight terminal failures and external malformed-output tests. These infrastructure observations are not generated Stage decisions; asking a model to repair a real quota or failed isolation would bypass their owner. |
| OutputViolation → repair/exhaustion | Local validators, `RULES`, `AUTHORITY_RULES`, `CLARIFICATION_RULES` and bound references → original schema/contract plus shared bounded `repair_feedback` | Allowlisted/redacted context, explicit partial-context marker, unchanged revision/transport accounting → `_generate` | Updated: `semantic_contracts` verifies rules, expected/received, bounds and secret exclusion; existing crash/resume and repair-exhaustion tests remain. Rejected output is never promoted to authority. |
| Durable Stage values → resume/replay/inspection | `stage_contracts.VERSION`, binding digest and Journal history → recorded exact invocation contract | Version fence before resume; reuse only exact validated/accepted state → Engine resume; CLI `projection.stage_invocations` exposes recorded bindings | Audited with version updated: stage-contract old-journal fence, repair replay and semantic Engine replay tests. Older journals remain inspectable, not silently reinterpreted. |
| Explicit human Goal revision / final publication → new Run / exported result | CLI/Engine operation and immutable Original Input/Candidate owners; `GRAPH.candidate` explains boundary | Human revision creates linked Run; publication requires verified completion and exact Candidate → public Run interface/Candidates | Audited: public Run and architecture canary tests. Neither model summaries nor Finding categories invoke these operations. |

## Schema boundaries and review result

The provider schema enforces known check/criterion IDs, exact Finding count,
selection bounds, host syntax and nested Finding category/pass combinations.
Unique coverage, DAG relationships, historical equality, dynamic evidence routes
and the root WorkerResult status/request relationship retain deterministic
validation with the same definitions and bounded repair context. No response
wrapper or additional model call is needed.

The root WorkerResult cannot be replaced with a root `anyOf` in OpenAI Structured
Outputs. Nested `anyOf`, array size constraints and numeric bounds are supported;
the provider-subset choice was checked against the
[official schema documentation](https://developers.openai.com/api/docs/guides/structured-outputs#supported-schemas).
This is documentation/schema validation, not a live-provider test.

The reverse audit found additional producer gaps in selection bounds, recovery
review context, failed-dependency/growth admission, and authority host/duplicate
diagnostics; these are included in this change. No known independent semantic
gap was deferred. Infrastructure-owned safety decisions remain outside model
repair. Tests establish deterministic contract consistency, not perfect model
understanding or live benchmark success. Inspector reads each recorded binding's
`semantics` and `constraints`; documentation links these owners instead of
maintaining a second status/action table.
