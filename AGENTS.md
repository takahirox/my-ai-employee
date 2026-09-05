# Agent instructions

## Usage Limit: mandatory operator constraint

- Never redeem Usage Limit reset tickets on your own, even when asked to finish a task autonomously.
- Never buy additional allowance or switch models/providers to evade a usage limit on your own.
- If a usage limit blocks work, preserve progress, report it, and stop for operator direction. A general completion instruction does not grant reset or purchase authorization.
- This constraint applies across continuations, handoffs, and context compaction.

<!-- BEGIN codex-auto-review-efficiency-v2 -->
## AutoReview efficiency (managed)

- Batch independently safe approval-requiring actions only when they share the same scope and authorization; keep unrelated or higher-risk actions separate.
- Prefer read-only checks that can run inside the sandbox before requesting broader filesystem or network access.
- Filter large output to relevant fields or exact matches, use summaries or counts first, and cap the number of lines sent for review. Do not expose secrets or unrelated host content.
- Do not repeatedly request the same boundary crossing. Reuse the prior decision when it still covers the exact action, or explain what materially changed.
- When escalation remains necessary, request the narrowest exact operation, target, and duration that completes the authorized work.
- Before a long phase of external operations, start a new thread with a concise sanitized handoff covering the objective, authorization, completed evidence, remaining work, and safety boundaries.
<!-- END codex-auto-review-efficiency-v2 -->
