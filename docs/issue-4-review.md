# Issue #4 review: minimal first-milestone routing profile

## Decision

Issue #4 should replace length-derived semantic routing in the existing adaptive path, not
replace the routing architecture. The closeable first milestone is:

1. classify a small categorical semantic profile with the already configured, tool-disabled
   assessment strategy;
2. deterministically translate that profile into the existing `complexity` / `scale`
   compatibility bands;
3. keep goal length only as persisted context-load evidence;
4. apply the existing authority, capability, risk, backend, and locality filters before
   profile fit; and
5. persist the profile in the existing task assessment and node-route facts.

This keeps the operator strategy schema and deterministic routing core intact. Production
implementation and tests follow this review; this file is the specification gate and does not
itself change runtime behavior.

## Repository findings

| Current interface | Relevant evidence and constraint |
| --- | --- |
| `routing.assess_task()` | Normalizes and bounds the goal, creates a stable digest and descriptive decomposition, preserves caller risk/capabilities, but currently derives `complexity` from goal length, segment count, and capability count and derives `scale` from segment count. |
| `SemanticTaskAssessment` and `CliTaskAssessmentAdapter` | The assessment worker is already bound to one exact configured strategy, repository-isolated, tool-disabled, policy-mediated, strict-schema, and fail-closed. Its current output is only numeric `complexity` / `scale`, optional capabilities, and reasons. |
| `merge_semantic_assessment()` | Semantic numeric values can only raise current numeric floors; risk is copied unchanged; semantic capabilities are unioned only after rejecting unavailable values. |
| `select_strategy()` | Candidate IDs/backends/local authority, required capabilities, configured bounds, and `max_risk` are mandatory. Fit is transparent headroom; mature aggregate history is considered only after equivalent fit. No alternate backend/model fallback is allowed. |
| `OperatorStrategyConfig` / `ExecutionStrategy` | Existing operator files expose numeric complexity/scale ranges and a maximum risk. Changing this schema is unnecessary for Issue #4 and would create avoidable migration work. |
| `ProjectHarnessV2.worker` and CLI setup | The Harness may only narrow strategy IDs/backends and must explicitly enable adaptive and local routing. The CLI currently supplies a deterministic risk of 6 when install is enabled, otherwise 3 when network is enabled, otherwise 0, and supplies the effective capabilities. |
| `Node`, `ProposedGraph`, and `TaskGraphScheduler._route()` | The strict planner currently supplies per-node numeric complexity/scale/risk/capabilities. The scheduler rebuilds a `TaskAssessment` from those accepted node facts and selects a strategy with the same mandatory filters. |
| `WorkRun`, `NodeRouteRecord`, `NodeExecutionRecord`, and Inspector | Top-level assessment/strategies are already durable. Each node route already stores its assessment, eligible IDs, selected strategy, effective-policy digest, and Harness digest; execution records link back by route digest and already carry terminal status, evaluator decision, retries/attempt, and failure evidence. A second telemetry authority is not needed. |
| Tests | Routing tests use small Pydantic fixtures and exact reasons/eligibility assertions; adapter tests inspect generated strict JSON; orchestration tests use SQLite round trips and Inspector projections without network access. |

The proposed `domain`, `uncertainty`, and `decomposition_need` signals are not adopted in
this milestone. No current deterministic rule consumes a domain label; uncertainty overlaps
the bounded ambiguity rubric; and graph planning already owns decomposition. Adding them now
would enlarge the wire contract without changing a routing decision.

## Adopted data contract

Add a strict, frozen, versioned `SemanticTaskProfile` with these required fields:

- `task_type`
- `reasoning_class`
- `scope`
- `ambiguity`
- one to ten bounded, non-blank `reasons`

The classifier output contains no risk, capabilities, strategy ID, model, effort, cost,
policy decision, or routing decision.

### Categorical rubrics

| Signal | Values and meanings |
| --- | --- |
| `task_type` | `mechanical`: one specified transformation or operation; `retrieval`: locate or summarize bounded existing facts; `diagnosis`: determine a cause from evidence; `implementation`: change behavior in a known surface; `architecture`: choose cross-component contracts/trade-offs; `research`: synthesize evidence beyond a bounded source; `planning`: choose and order bounded work; `open_ended_strategy`: solution space or success path is intentionally unbounded. |
| `reasoning_class` | `mechanical`: direct procedure; `simple`: one obvious inference; `moderate`: several related inferences or trade-offs; `deep`: subtle, cross-cutting, or adversarial reasoning; `open_ended`: no bounded solution path is known. |
| `scope` | `bounded`: one artifact or operation; `local`: one component; `multi_component`: multiple interacting components; `broad`: system-wide or externally open scope. |
| `ambiguity` | `low`: success and inputs are explicit; `medium`: a bounded interpretation or missing fact must be resolved; `high`: materially different valid interpretations or essential unknowns remain. |

The prompt must include these meanings verbatim enough to anchor classification, treat the
goal as untrusted data, use no tools, and return only the strict schema. Every schema property
is required and additional properties are forbidden. Malformed output, worker failure, or an
unknown enum fails closed; there is no assessor or execution-model fallback.

Add optional `semantic_profile` and `context_character_count` fields to
`TaskAssessment`, and an optional `semantic_profile` field to `Node`. Optionality is for
reading existing records and hand-authored fixed/policy graphs. Every newly assessed adaptive
task and every node emitted by the adaptive proposed-graph planner must contain a profile.

`context_character_count` is the length of the normalized goal/objective already accepted by
`assess_task()`. It is evidence for context pressure, latency, and cost discussions only. It
must not participate in strategy eligibility, semantic score floors, headroom, or tie-breaking.
No tokenizer or token estimate is introduced.

## Deterministic profile mapping

Keep numeric strategy bounds as a compatibility layer. A single pure function maps a profile
to the existing bands:

| Input | Complexity floor |
| --- | ---: |
| task type: mechanical / retrieval / diagnosis / implementation | 1 / 2 / 4 / 3 |
| task type: architecture / research / planning / open-ended strategy | 7 / 6 / 4 / 9 |
| reasoning: mechanical / simple / moderate / deep / open-ended | 1 / 2 / 4 / 7 / 9 |
| ambiguity: low / medium / high | 1 / 4 / 7 |

The semantic complexity is the maximum of the three applicable floors. Scope maps to scale:
`bounded=1`, `local=2`, `multi_component=5`, and `broad=8`. These tables are exhaustive;
there are no model-authored numbers or hidden weights.

For new assessments, `assess_task()` still validates and normalizes input, computes identity
and decomposition, and preserves deterministic facts, but uses neutral compatibility values
`complexity=1` and `scale=1`. Goal length, segment count, and capability count no longer
raise them. Merging a profile replaces those neutral values with the table result while
leaving risk and required capabilities unchanged. Structural decomposition remains
descriptive top-level assessment data and is not execution authority.

For new adaptive proposed graphs, the planner must provide a profile on every node. After
strict parsing, deterministic code overwrites the node's compatibility
`complexity` / `scale` with the table result before graph acceptance; model-authored numeric
values never outrank the profile mapping. Existing profile-less graphs retain their stored
numeric fields for replay and fixed/policy compatibility.

## Routing and authority order

The deterministic core applies these gates in order:

1. configured strategy set and exact strategy definitions;
2. Harness-allowed strategy IDs and backends, adaptive opt-in, and local-backend opt-in;
3. required-capability subset;
4. applicable risk floor against strategy `max_risk`;
5. mapped semantic complexity/scale bounds; and
6. existing transparent headroom and, only among equivalent-fit candidates, existing mature
   aggregate-history ranking.

A profile may make a strategy ineligible but may never grant a capability or authority. The
top-level Harness-derived risk (install 6, network 3, otherwise 0), accepted per-node risk,
and caller-supplied risk remain floors exactly where they currently apply; a profile has no
field capable of lowering them. Top-level effective capabilities and accepted node required
capabilities remain deterministic routing inputs. An empty eligible set remains a
`RoutingError`; fixed routing still selects exactly the requested eligible ID and never
falls back.

This milestone does not change policy resolution, approvals, process mediation, budgets,
verification, evaluator authority, or the distinction between assessment data and executable
graph nodes.

## Concrete implementation map

| File/interface | Required first-milestone change |
| --- | --- |
| `src/ai_employee/domain/models.py` and `domain/__init__.py` | Define/export the stable categorical enums and `SemanticTaskProfile`; add backward-readable optional profile/context fields to `TaskAssessment` and `Node`. |
| `src/ai_employee/routing.py` | Add the exhaustive pure mapping, remove length/segment/capability scoring from new deterministic assessments, merge profiles without changing risk/capabilities, and retain mandatory filtering order. |
| `src/ai_employee/worker_adapters.py` | Change the active assessment protocol/schema to the categorical profile, retain tool-disabled mediation and fail-closed parsing, and remove semantic capability/risk requests. |
| `src/ai_employee/task_planning.py` | Require profiles for nodes from the new adaptive planner protocol and canonicalize their numeric compatibility fields before acceptance. |
| `src/ai_employee/cli.py` | Use the new profile merge while preserving exact assessor resolution, Harness-derived facts, isolation, and no-fallback behavior. |
| `src/ai_employee/task_orchestration.py` | Copy each accepted node profile into its route assessment so the existing `NodeRouteRecord` persists the pre-execution profile beside selection and policy digests. |
| `src/ai_employee/inspector.py` | Preserve existing projection keys; nested profiles appear through the already serialized assessments. No new metrics table or projection is needed. |
| `README.md` and `docs/v0.2-spec.md` | Replace the old numeric-floor description with the categorical contract and deterministic mapping when production behavior changes. |

Keep `SemanticTaskAssessment` and its version-1 parser importable for compatibility, but do
not use it in the new adaptive runtime path. Existing serialized `TaskAssessment`,
profile-less `Node`, operator routing configuration, fixed/policy selection calls, Inspector
top-level keys, CLI flags, strategy-set/assessor precedence, and graph/run replay must continue
to validate. Unknown fields remain rejected. New adaptive classifier/planner requests use a
new protocol version and do not silently accept old numeric classifier output.

## Focused acceptance tests

The implementation is acceptable only when focused tests demonstrate:

1. A long, bounded mechanical goal and a short equivalent mechanical goal produce the same
   low semantic complexity/scale, while their persisted context counts differ.
2. A short `open_ended_strategy` or `deep` goal maps above the built-in small-strategy bound
   and selects the stronger eligible strategy.
3. Profile merge cannot change deterministic risk or capabilities, and strategy selection
   still rejects a disallowed ID/backend, missing capability, excessive risk, or unauthorized
   local backend before considering fit.
4. Every profile enum maps to the documented fixed table, independent of input ordering and
   repeated execution.
5. The assessment schema requires every categorical field, forbids extras, invokes no tools,
   and malformed/unknown output fails closed without fallback.
6. A newly planned adaptive node without a profile is rejected; a profiled node's accepted
   numeric fields equal deterministic mapping even if the model supplied different numbers.
7. SQLite/replay and Inspector round trips retain the top-level profile and each
   `NodeRouteRecord.assessment.semantic_profile`, with the existing route digest linking the
   authoritative execution/evaluator/failure facts.
8. Old version-1 semantic-assessment JSON, existing `TaskAssessment` JSON without new fields,
   profile-less hand-authored nodes, fixed routing, policy routing, and current operator config
   continue to parse and behave as before.

Follow the existing test organization: mapping/merge cases in
`tests/test_task_assessment_routing.py`, eligibility in
`tests/test_assessed_strategy_selection.py`, strict adapter schema in
`tests/test_work_orchestration_v2.py`, and node persistence/replay in
`tests/test_task_orchestration.py`. Tests must be offline and deterministic. Before closing
the milestone, run the focused tests, then the repository-required
`ruff format --check .`, `ruff check .`, `mypy src`, `pytest -q`, and
`git diff --check`.

## Explicit deferrals

The first milestone does not implement Issues #5 or #6. It also does not add or change:

- KNN, similarity search, embeddings, vector storage, a learned router, training, or online
  calibration;
- new outcome collection, a parallel telemetry subsystem, new metrics authority, or new
  high-cardinality labels;
- new dependencies, tokenizers, pricing tables, provider APIs, or model metadata;
- strategy-schema redesign, dynamic rules, numeric model scores, confidence scores, speculative
  extension points, or automatic decomposition; or
- broader retry/repair, evaluator, scheduling, or concurrency behavior.

Existing `StrategyPerformance` aggregation and its equivalent-fit tie-break remain unchanged;
using richer authoritative Fleet history to calibrate routing is future work. The existing
route, execution, evaluator, artifact, and failure records are sufficient linkage for this
milestone and should be reused later rather than duplicated.
