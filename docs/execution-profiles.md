# Explicit proportional orchestration

```sh
fleet work "Correct the local defect and preserve compatibility" \
  --profile lightweight --strategy baseline --operator-config operator.json
fleet work "Investigate the cross-component design" \
  --profile adaptive --operator-config operator.json
```

`lightweight` uses existing fixed routing and one-node graph authority. It omits
model-backed goal assessment, planning, plan review and per-node strategy assessment.
An explicitly configured, authorized and eligible strategy is required. Existing
`--routing-mode fixed --strategy ...` remains equivalent; adaptive remains the default.
`--profile` and `--routing-mode` are mutually exclusive. There is no new classifier,
opaque orchestration score, silent fallback or inference that an unknown task is safe.

Workers are instructed to inspect relevant code/tests within their accepted scope and
budgets before committing to implementation details, connect observations to criteria,
then implement and verify. This uses their existing read-only/native tool authority,
not a new investigation capability. Required comprehensive investigation remains
required. A proposal-only worker still cannot directly edit the source checkout.

If findings require missing criteria, new authority, larger scope or consequential
design decisions, the worker must stop with findings. Existing bounds and authoritative
acceptance decide the outcome. Start an explicit adaptive run or use a supported
bounded repair/replan transition; this initial profile does not silently upgrade a
failed lightweight task to another model or unbounded DAG.

All configured task/parent reviews, artifact review, shared and Goal checks, freshness,
cancellation/budgets and promotion approval remain intact. A Project Harness can require
plan review explicitly with `verification.review.plan_review: true`. Fixed/lightweight
invocation then fails before model access with an actionable adaptive-profile message.
The disabled default preserves historical Harness and nested-record digests.

Inspector exposes the selected path, stage disposition/reasons and timing records.
"Selected" describes the intended path, not proof that a failed preparation stage ran;
the existing usage/evidence records show actual calls. Timings record wall time until
the first worker coordinator starts (not the first source edit), and each active CLI
invocation's wall time. Active time excludes pause/resume waiting; a separate UTC-based
elapsed measurement includes it through the latest completed invocation. Incomplete
or interrupted invocation coverage leaves totals null, with a known completed subtotal.
No-worker runs
do not manufacture a pre-worker measurement. Resume preserves the original profile
and experimental guidance choice. Read-only replay never repeats model calls.

Use [private History corpus trials](history-corpus.md) for matching task/base/check/model
and budget comparisons. Compare small fixes, medium implementations and design-uncertain
cross-component work; judge independent quality and human effort before preparation
latency. Deterministic regression fixtures exercise all three classes and guidance
ON/OFF. They do not establish a live-model performance improvement.
