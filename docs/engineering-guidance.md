# Engineering guidance

Fleet's shared `engineering_guidance.py` supplies positive simplicity guidance to
the Planner, proposal and isolated workers, Plan Review, Task Result Review and
parent semantic review. Workers and code reviewers additionally share why-first
inline-comment guidance. It is guidance, not a new authority or scoring system.

Prefer the simplest resulting design that satisfies the current requirements,
not mechanically the smallest diff. Necessary verification, compatibility,
security, performance and error handling remain required. Added DAG structure
and implementation mechanisms need current justification. Do not force reuse of
an unsuitable mechanism or expand the task into unrelated simplification work.

Comments should preserve non-obvious reasons and constraints. Public behavior,
schema and protocol documentation remains necessary where required. Preserve
useful existing comments; do not perform repository-wide comment removal. Style
preferences alone must not block correct work unless accepted project standards
make them requirements. Reviewers cannot infer defects from unavailable bodies.

## Evaluating behavior

Prompt-capture regression tests cover all six entry points, including the native
worker. These tests establish delivery of the guidance and retention of existing
authority rules; they do not establish a measured improvement in model output.

For behavioral comparisons, use the existing productivity evaluation's ablation
arms with the same task, starting commit, acceptance/regression checks, model,
effort, budgets and attempts. Freeze the code revision and prompt digests in each
arm. Do not change the grader to reward smaller output. Use these paired cases:

| Case | Acceptable simpler result | Required counter-check |
| --- | --- | --- |
| Local parser correction | Existing parser handles the missing case | Malformed input and compatibility checks still pass |
| Consumer of an existing API | Reuse the suitable API without a parallel framework | Public contracts and integration tests remain complete |
| Retry or security boundary change | Only necessary state and non-obvious rationale | Bounds, safety invariants and rationale comments remain intact |
| Required multi-component feature | Only dependency-justified nodes and abstractions | Do not under-decompose or omit required feature coverage |

Judge acceptance and regression-free quality first. Then record justified versus
unjustified concepts/layers/state, DAG nodes/edges, redundant narration versus
useful rationale comments, review burden and subsequent rework. Changed LOC and
comment counts are secondary observations, never a pass/fail rule. Record actual
tokens, calls, time and human interventions where available; mark unmeasured
values unavailable. Do not infer reduced complexity or cost from prompt wording.
