# Model transport efficiency

Model prompts are not authoritative records. Optimizations must preserve accepted
requirements, policy, evidence references, attribution and strict result validation.
They do not disable assessment, planning or review stages.

## Field inventory and current decisions

| Candidate | Decision | Reason |
| --- | --- | --- |
| Identical assigned and accepted goal text | Use an explicit transport alias | Complete original text stays in `goal`; authoritative requests retain both fields. Unequal objectives remain separate. |
| `non_mutating_result_binding` in Codex worker input | Omit | The model-facing read-only schema is wire v3 content. `attribute_read_only_payload` binds runtime identity from the immutable originating request after process correlation. |
| The same binding object in generic/legacy worker input | Retain | Keep the existing prompt contract for other adapters. Legacy bound results are never silently relabeled. |
| Goal, completion criteria, capabilities, budgets, predecessor evidence and allowed evidence sources | Retain | They constrain the work or let the model choose valid evidence; apparent management information is not automatically redundant. |
| Schema in prompt and backend output-schema argument | Retain | An output constraint and model-visible guidance may serve different purposes. Source inspection alone does not establish that either copy can safely be removed for each provider. |
| Stable instructions before volatile fields | Already implemented | `prompt_transport.py` preserves this ordering; it is not a new optimization. |

The Codex omission does not change persisted requests, response schemas, attribution,
legacy-result validation, graph generation fences or permissions. It removes a
runtime-owned input object, not evidence that the model needs to cite. Do not remove
the separate `allowed_evidence` provenance merely because it contains digests.

Prompt-byte savings are deterministic. Native input, cached input, output tokens and
elapsed time must be measured separately; one run does not establish a stable speed
or billing improvement.
