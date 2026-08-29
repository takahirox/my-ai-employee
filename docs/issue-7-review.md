# Issue 7 review: independent task-result review

Issue 7 fits the existing authority split only as an optional gate after deterministic node
verification. Issue 10 already owns bounded `REPAIR` transitions and Issue 11 already binds repair
requests to fresh, revision-specific worker context. This milestone therefore adds one semantic
evidence producer and connects its accepted findings to those existing mechanisms; it does not add
a second scheduler or a reviewer-controlled state machine.

## Accepted first milestone

- Review is disabled by default. A Project Harness must enable independent task review and the
  operator must separately designate one configured strategy as reviewer-eligible.
- That double opt-in authorizes sending only the task objective/criteria, exact `WorkerRequest` and
  `WorkerResult` (including a proposed unified diff or typed result), deterministic verification
  evidence, and sanitized body-free artifact descriptors to the configured reviewer backend. It
  does not authorize arbitrary artifact bodies, the repository, conversation history, secrets, or
  any read/tool capability.
- Review runs only for a node result which has passed deterministic criterion evaluation. A fresh
  reviewer process receives the node objective and criteria, the exact worker request/result,
  deterministic evidence/evaluator digests, and bounded artifact descriptors. It receives no
  repository access, mutable workspace, prior conversation, or tool authority.
- The model-controlled response is one strict typed payload. Findings identify category, severity,
  confidence, fact-versus-inference basis, affected criteria, exact evidence/artifact digests, and
  the smallest repair objective. Unknown, stale, unbound, duplicate, or unordered references fail
  closed.
- Trusted request, result, and decision records bind reviewer strategy, accepted graph revision,
  node generation and attempt, worker request/result, deterministic evidence, evaluator evidence,
  and artifact descriptors. Finding digests and all input digests are persisted; artifact bodies
  are not copied into review records.
- The Trust Kernel alone maps accepted findings: no blocking finding is `PASS`; an uncertain or
  explicitly operator-required blocking finding is `ESCALATE`; an unrecoverable blocking finding is
  `FAIL`; otherwise an actionable blocking finding is `REPAIR` while the existing repair and
  resource budgets remain. Any declared coverage limitation is fail-closed `ESCALATE`. Exhaustion
  uses Issue 10's `REPAIR_BUDGET_EXHAUSTED` escalation.
- `REPAIR` feeds the trusted review request/result/decision digests into the existing fresh worker
  context. The worker then runs again, followed by deterministic verification and a new independent
  review. Reviewer output never mutates files or advances graph state directly.
- Replay and Inspector reconstruct the persisted verification -> review -> Trust Kernel decision
  chain without invoking a worker, evaluator, reviewer, composer, or promoter. Resume accepts only
  exact revision/generation/attempt bindings and never reviews an already-decided result again.

## Deliberately deferred

This milestone does not implement Issue 8 parent-candidate semantic review, adaptive reviewer
routing, multiple reviewers, voting or scoring, a generalized review framework, reviewer-authored
policy, or automatic promotion. Deterministic checks remain authoritative whenever a property is
executable.

Codex review is ephemeral and read-only with approvals disabled, user config/rules ignored, and
both `shell_tool` and `unified_exec` disabled. Claude review supplies an empty tool set and disables
session persistence. Compatibility digests omit the new opt-in fields only while they retain their
disabled defaults, so a pre-Issue-7 paused run can resume without weakening enabled review
authority.
