# Fleet stage-contract audit (#74)

Audited baseline: published main `c695d82`, plus this change. The initial local main
`fd64ae4` did **not** include the published #64–#68 fixes. An audit against that old
checkout would incorrectly identify already-fixed incidents as new defects.

## Ownership and transitions

Semantic requirement IDs identify **what** must be proved; request/result/artifact
digests identify **which exact evidence** proves it. Neither substitutes for the other.
Model process exit zero and helpful prose are never authoritative task completion.

| Boundary | Consumer contract and producer's way to satisfy it | Guard / regression family |
| --- | --- | --- |
| Operator → Goal | Exact statement, task kind, process authority and mandatory criteria. Contradictory artifact/process needs fail before dispatch. | `cli._work_goal`; `test_graph_patchless`, `test_cli_task_routing` |
| Goal → semantic assessment | Versioned task type/scope/reasoning rubric, capabilities and exact allowed strategy set; schema expresses enum/bounds. | `semantic_assessment_schema_json`; `test_work_orchestration_v2`, `test_task_orchestration` |
| Assessment → Planner | Accepted Goal and eligible routes, finite node/resource limits; operator strategy identity is not model-owned. | `task_planning`, planner/route cases in `test_task_orchestration` and `test_cli_graph_e2e` |
| Planner → Plan Reviewer | Exact proposed graph/request bindings; unique findings are normalized when their order has no meaning. | `test_plan_review_adapters::test_claude_wrapper_accepts_unsorted_findings_and_returns_canonical_review` (#26) |
| Plan Review → graph | Runtime kernel accepts/rejects exact graph revision; opaque record IDs cannot replace node/criterion IDs. | `test_task_orchestration` tampered-review/replay cases |
| Graph → WorkerRequest | Immutable graph/node/generation/attempt, original criteria, available evidence and explicit remaining budgets. Result strategies must be able to satisfy criteria. | `test_missing_criterion_evidence_capability_fails_before_runner`; `test_issue74_contract_audit` (#43) |
| Worker invocation → result | New read-only wire v3 supplies only substantive content. Runtime attributes it to the originating request after transport correlation and cancellation checks. Legacy bound v2 is validated, never relabelled. | `worker_attribution`; `test_issue85_attribution` |
| Result → typed acceptance | Full binding, content bounds, exact authorized SHA-256 evidence set, no actions for a read-only result, explicit byte budget. Runtime allocates the later artifact digest. | `test_graph_typed_results`, `test_work_orchestration_v2` evidence-schema/authority tests (#54) |
| Proposal → patch | Visible edit paths must exactly match parsed, canonical diff paths; optional accepted headers normalized deterministically. Ambiguous counts, traversal, stale baseline and unauthorized edits fail. | header-less/hunk/path/preflight tests in `test_work_orchestration_v2`, `test_multi_hunk_recount` (#60/#64) |
| Candidate → verification | Runtime binds semantic requirement IDs to opaque ProcessRequest IDs and exact candidate/workspace digests. All required checks run under policy. | node-verification-binding tests in `test_work_orchestration_v2` (#55) |
| Verification → task acceptance/review | Successful process results must match the bound requests; mandatory criterion ledger and independent review remain required. Post-verification candidate must still match. | `test_node_verification_workspace_mutation_fails_with_exact_bounded_evidence`; `test_closed_loop_orchestration` (#66) |
| Accepted nodes → composition | Exact accepted artifacts, distinct owned workspaces, unchanged canonical patches and deterministic ordering; conflicting edits fail closed. | `test_graph_composition`; `test_canonical_diff_ignores_only_declared_untracked_generated_files` (#65) |
| Composition → parent evaluation | Exact composed candidate and full Goal coverage, not just a child PASS. Request-specific frozen checks add to shared Harness checks. | `test_graph_execution`, `test_issue83_goal_acceptance` |
| Failure → Repair/Retry/Replan | Feedback references accepted failures/requests; same generation/attempt fences and bounded reservations apply. No free-form failure message grants authority. | `test_closed_loop_orchestration`, `test_task_orchestration` |
| Predecessor → successor context | Structured outputs name exact accepted revision/generation and runtime-generated artifacts; authorized evidence set is explicit. | `test_graph_typed_results`, `test_graph_patchless` |
| Parent PASS → approval/promotion/replay | Original Goal/effective Harness/operator authority and exact evaluated patch must still match. Explicit approval and promotion remain separate. | `test_cli_graph_e2e`, `test_issue83_goal_acceptance`, `test_parent_review` |

Replay uses the persisted graph acceptance, criterion bindings, result/descriptor
provenance and kernel validation; it does not call a model or infer missing bindings
from a process name, tuple position, natural-language citation or current node state.
The new Goal checks are reconstructed from the persisted Goal; re-reading or editing
the original acceptance input cannot weaken them. A changed Harness digest blocks reuse.

## Findings and corrections in this change

1. Read-only result producers previously copied Fleet-owned attribution fields. Wire
   v3 removes that burden; transport/request correlation occurs before attribution,
   followed by existing typed acceptance and replay checks. Foreign, cancelled,
   malformed, unauthorized and contradictory legacy results remain rejected (#85).
2. Selected typed acceptance, its artifact descriptor and explanatory event previously
   had separate commits. They now commit in one fenced SQLite transaction. Orphaned
   bytes after a crash confer no authority. External execution and the entire graph
   lifecycle are **not** claimed to be atomic.
3. A direct accepted read-only request missing `artifact_bytes` now fails before even
   probing the worker. Normal graph construction already supplies this field (#43).
4. Shared checks alone can pass an incorrect task-specific result. An explicitly
   accepted Goal file adds frozen, executable checks without weakening shared checks
   or treating a model's assertions as evidence (#83).
5. Native filesystem capture avoids asking a model to serialize a patch in the new
   opt-in isolation mode. The legacy parser and all genuine rejection checks remain.

## Classification and false-rejection metric

Inspector/explain retain the deepest stable cause as well as graph wrappers:
`DIFF_HUNK_AMBIGUOUS`/`PATCH_PREFLIGHT_FAILED` are patch-contract failures;
`VERIFICATION_BINDING_INVALID`/`TYPED_RESULT_STALE` are binding failures;
`TYPED_RESULT_EVIDENCE_UNAUTHORIZED` is rejected evidence;
`VERIFICATION_WORKSPACE_MUTATED` is a lifecycle failure;
`VERIFICATION_FAILED` with matching evidence is a failed quality check;
policy, budget, timeout and infrastructure failures remain distinct.
`test_run_explanation` checks bounded body-free causal diagnostics (#64).

A contract failure is **not automatically a false rejection**. In particular the old
benchmark's malformed multi-file diff/hunk output is not evidence that a valid patch
was rejected. Neither successful model exit nor a worker's claim establishes validity.

`python -m ai_employee.contract_audit observations.json` summarizes explicit offline
adjudications. Each JSON-array entry supplies run/candidate identity, accepted outcome,
stable code, independent candidate/evidence digests, substantive verdict, confirmed
contract defect and legitimate-rejection verdict. The independent candidate must match.
False rejection requires substantive PASS, a confirmed contract defect, rejection and
no legitimate safety/staleness/malformed-output reason. Duplicate candidates fail.
Unknown adjudications are reported separately; an empty denominator produces `null`,
never a fabricated 0% rate. This report is analysis, not a new acceptance authority.

No new population failure-rate or model-superiority claim is made by this audit.
