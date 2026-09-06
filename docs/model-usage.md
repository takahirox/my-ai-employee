# Model usage and stable prompts

New CLI-run model invocations persist body-free `model_usage_v2` records. Trusted
model executors cover semantic assessment, planning, plan revision, plan review,
proposal workers, task review and parent/goal review. Native workers record at
their model boundary, including failures; preflight-only failures are not model
calls. Probe, verification and arbitrary project processes are not collectors.

Records retain the graph and invocation run IDs, exact request/prompt digests,
configured backend/model/effort and Operator Config digest, duration and outcome.
Worker child-run IDs link to existing node/generation/attempt records, including
repair and retry attempts. Each invocation is counted once by record ID. Replay
only reads records; it never calls a provider or recalculates historical prices.

## Semantics and limitations

- Codex adapters use JSONL and capture only `turn.completed.usage`, not the model's
  generated `usage_json`. Completed turn counts are summed. Missing, negative,
  boolean or non-integer counts remain unavailable. Terminal error streams do not
  become successful model payloads.
- Claude reads the outer CLI result, never `structured_output.usage`. Input is
  uncached input plus cache reads plus cache writes, only when all categories are
  available. Its top-level token usage describes the main CLI loop. Its reported
  cost may cover internal subagents as well, so those scopes are not equated.
- Claude `total_cost_usd` is a **CLI estimate**, not authoritative billed cost.
  Persist that value and its basis as received; the bundled pricing version is
  unavailable. Fleet does not guess current prices, infer billed cost or convert
  subscription usage into money. Codex/Ollama cost is unavailable.
- Legacy records and unsupported transports remain unmeasured. Summary counts
  describe recorded invocation attempts, not a provider billing statement. The
  absence of old telemetry is not evidence that old runs used no tokens.
- Totals are unavailable if any included invocation lacks that metric. A known
  subtotal and reported-invocation count are shown separately. Ratios require
  complete compatible input/cache counts; zero input has no cache ratio.
- Failed or interrupted invocations preserve observed usage as an incomplete
  subtotal; unreported final-turn consumption is not silently counted as zero.
- Accounting adds no prompt or response bodies. Existing protected artifact
  handling is retained. Codex JSONL tool outputs and reasoning are removed before
  stdout artifact storage; only final-message transport and numeric usage remain.
  Raw output byte limits are still enforced before filtering. Usage has no acceptance, budget-expansion or billing
  authority. Usage limits never authorize resets, purchases or provider changes.

Inspector's run detail shows input, cached input, output, cost classification and
stage breakdown. Its raw projection includes individual invocation provenance.
Job overview aggregates its child graph runs; repository filtering restricts
usage to visible child runs. The same read-only `inspect_usage` result can be
consumed by local evaluation reports, without reading model output bodies.

## Prompt caching

`prompt_json` serializes the existing protocol/instructions/schema/rubric before
dynamic goal, node, attempt and evidence fields. Each value uses the existing
canonical serializer; authoritative record digests and schemas are unchanged.
Relevant project context remains in the bounded request. No extra context, model
answers, retention settings, response cache or provider-specific cache controls
are added. CLI-owned instructions and request routing can still limit reuse.

For equivalent before/after workloads, compare complete cached-input ratios,
known/unknown usage coverage, reported cost scope and duration by stage. Hold the
task, baseline, model, effort, budgets, attempts and grading fixed. Preserve both
failed and successful trials. Prefix equality tests demonstrate stable ordering,
**not measured cache hits or cost savings**. A live paired comparison is needed
before claiming either; fixture tests make no such claim.

Sources: [Codex JSONL](https://learn.chatgpt.com/docs/non-interactive-mode),
[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
[Claude usage and cost semantics](https://code.claude.com/docs/en/agent-sdk/cost-tracking).
