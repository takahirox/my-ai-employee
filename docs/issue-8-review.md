# Issue 8 review: parent-candidate semantic evidence

The suggested first milestone fits the existing parent evaluation path without a second scheduler
or a model-controlled acceptance state. Fleet first composes accepted task results and completes all
required deterministic parent evaluators. An optional AI observer then reviews that exact candidate
and emits typed evidence. The existing Trust Kernel remains the only component that selects
`PASS`, `REPAIR`, `ESCALATE`, or `FAIL`.

## Accepted milestone

- Review is disabled by default. The Project Harness must set
  `verification.review.parent_semantic_review: true`, and the operator must separately select a
  `default_parent_reviewer_strategy` marked `parent_reviewer_eligible: true`. The strategy must
  remain inside the selected strategy set and Harness worker allowlists.
- The observer runs only after all required deterministic parent evaluators pass. It receives the
  original Goal, accepted graph, node generation/attempt bindings, deterministic evidence, and the
  exact composed patch. It receives body-free descriptors for other artifacts; descriptor source,
  store locator, and other metadata are not exported. The exact candidate patch is the sole artifact
  body exported, is digest-verified, must be unredacted UTF-8, and is capped at 1 MB and the Harness
  artifact budget. Enabling the feature therefore explicitly permits that candidate body to reach
  the configured reviewer backend.
- Codex and Claude observers have tools, approvals, mutable sessions, repository/rules discovery,
  and conversation history disabled. The observer runs in the assessment directory rather than the
  candidate workspace. The model cannot mutate the graph, candidate, files, policy, approval,
  repair loop, or promotion state.
- `ParentSemanticReviewRequest`, `Result`, and `Decision` bind the exact run, accepted graph
  revision, generation, review attempt, candidate revision/descriptor/artifact, composition,
  Harness, policy, model/strategy, deterministic ledgers, criteria, nodes, and artifacts. Foreign,
  stale, incomplete, secret, or unknown references fail closed. Findings include a category,
  severity, confidence, observed/inferred basis, affected criteria/nodes, observation, rationale,
  evidence/artifact references, and an optional bounded repair objective.
- Wire-array order is not authoritative. The parser canonicalizes finding, coverage, limitation,
  and reference arrays after strict field/type checks; duplicates still fail closed. Internal
  records require unique canonical ordering.
- The Trust Kernel ignores non-blocking severities. Coverage limitations produce `ESCALATE`.
  Blocking inferred or uncertain findings produce `ESCALATE`. A blocking finding without a bounded
  repair objective produces `FAIL`. Only fully bound blocking findings with repair objectives
  produce `REPAIR`; no accepted blocking finding produces `PASS`.
- Accepted semantic request/result/decision digests and finding digests are merged into parent Goal
  evidence and affected criterion evidence. `ParentCandidateEvaluationRecord` is not extended,
  preserving its existing schema and digest contract; its existing decision/status and
  Goal-evaluator digest carry the authoritative outcome.
- `REPAIR` also persists a `ParentSemanticRepairRequest` containing accepted finding digests,
  affected node IDs, and bounded objectives. This is a typed authority handoff for the existing
  Issue 10 repair/replan boundary. This milestone does **not** automatically schedule a multi-node
  repair or let the reviewer initiate one.
- Replay and Inspector show deterministic and semantic evidence separately and never open artifact
  bodies or invoke a worker, evaluator, reviewer, composer, or promoter. Re-evaluation of the same
  candidate/graph/strategy/policy reuses one complete stored semantic chain; missing or ambiguous
  evidence fails closed.

## Compatibility and non-goals

Disabled Harness/operator fields are removed before legacy digest calculation, so projects and
operator configurations that do not opt in retain their pre-Issue-8 digests. Existing parent
candidate records and legacy replay remain readable.

This milestone does not implement generic observability (Issue 14), adaptive reviewer selection,
multiple reviewers/voting, numeric quality scores, auto-approval (Issue 9), arbitrary artifact-body
egress, reviewer tools, or an unattended parent repair scheduler.
