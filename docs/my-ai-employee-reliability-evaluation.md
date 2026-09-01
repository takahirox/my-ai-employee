# My AI Employee Reliability Evaluation

## Scope

This note records only observed evidence from the three-loop reliability effort. It does not
estimate population rates or compare model quality.

## Confirmed behavior and observations

- Before Loop 1, fixed compatibility runs had `max_retries=0` and `max_repairs=0`.
- Loop 1 gives edit-required compatibility runs `max_attempts=2`, `max_repairs=1`,
  and `max_loop_iterations=2`, while `max_retries` remains zero. It splits total process,
  wall-time, and artifact reservations between the initial attempt and repair. Legacy/no-capability
  compatibility runs and typed non-mutating results retain their prior contracts.
- An edit-required mutating request now rejects an empty proposal envelope as
  `WORKER_PROTOCOL_ERROR`.
- In the broad Loop 2 run, attempt 0 ended in `WORKER_PROTOCOL_ERROR`; attempt 1 used the
  same strategy and started with three accepted feedback digests. That repair did not complete:
  it reached the 900-second timeout.
- Two Luna evaluation invocations timed out without results. Narrowed Sol invocations returned
  results, but their count was not recorded here, so no completion rate or model-superiority
  claim can be made.
- Loop 2 process handling drains and counts stderr, retaining only bounded diagnostic bytes
  when truncated, without invalidating otherwise valid stdout. Stdout overflow remains
  terminal.
- Model/backend outer fallback remains forbidden; repair stays on the selected strategy.

## Compact before/after metrics

| Metric | Before Loop 1 / earlier evaluation | After Loop 1: broad Loop 2 run |
| --- | --- | --- |
| First-attempt completion | 0/2 Luna invocations; both timed out without results | 0/1 |
| Accepted repair started | 0/N compatibility runs; unavailable with zero repair budget, and N was not recorded | 1/1 |
| Accepted repair completed | 0/N compatibility runs; same denominator limitation | 0/1 |
| Timeout | 2/2 Luna invocations | 1/1 |
| Boundary failure | Not reported; denominator unavailable | 1/1 (`WORKER_PROTOCOL_ERROR` on attempt 0) |
| Outer fallback required | Not reported; denominator unavailable | Not measured; 0 fallbacks executed/1 because fallback is forbidden |

Denominators are per cell because the earlier compatibility and Luna cohorts are not the same
runs. The after column is one broad run, so these counts are an incident record, not statistical
evidence. The number of narrowed Sol runs was not supplied and is excluded from the table.

This new-file-only change was the single declared Loop 3 evaluation (`n=1`). It completed on
attempt 0 in 185.8 seconds, produced one typed edit proposal, passed all three exact-candidate
Harness commands below, and reached `completed` after exact-digest approval and promotion. Its
loop transition was `PASS`; no repair, retry, or model/backend fallback ran.

## Deterministic offline regressions

From a checkout whose locked development environment is already provisioned:

```bash
uv run --offline pytest -q tests/test_task_orchestration.py::test_fixed_compatibility_graph_preserves_non_mutating_contract_and_budget tests/test_task_orchestration.py::test_mutating_compatibility_graph_reserves_one_bounded_repair
uv run --offline pytest -q tests/test_work_orchestration_v2.py::test_scripted_adapter_rejects_empty_edit_required_mutating_envelope tests/test_closed_loop_orchestration.py::test_malformed_patch_protocol_failure_gets_one_bound_correction_turn
uv run --offline pytest -q tests/test_controlled_services_v2.py::test_process_executor_truncates_diagnostic_stderr_without_failing_valid_stdout
```

These regressions use local fixtures and subprocesses only; they require no model or network
service.

## Exact-candidate Harness verification

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

All three commands must pass against the exact composed candidate. A proposal-time pass against
the unchanged base checkout is not counted.
