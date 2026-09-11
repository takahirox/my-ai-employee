# Benchmark transport

`fleet-bench --request request.json --response response.json` runs the ordinary
Fleet engine with explicit disposable state. It does not use a benchmark-specific
worker loop or edit replay path. The response uses `pocket-agent-v1`; `completed`
means the controller finished, not that the external grader found the result correct.

The request has `protocol`, `operation: "run"`, absolute `control_dir`, `workspace`
and `public_checks`, `instruction`, positive `seconds`, `writable_roots: ["src",
"output"]`, and a complete current configuration in `settings.config`.
The standard optional `model` and `effort` fix all stage/reviewer selections for the
experiment. Direct product callers may alternatively supply top-level `config`;
supplying both configuration forms is rejected. Both public input directories must be
inside the private control directory. The public workspace contains only `input`,
`src` and `output`; the checks directory contains `smoke.py` and `execution.py`.

Public check bytes become operator-owned command definitions before workers run.
Goal acceptance must retain the public smoke check. Only an independently verified
Candidate can be exported, and changes to input files or unexpected output roots
are rejected. The target is checked against its original snapshot before export.
No hidden grader state enters the Run.

A Usage Limit returns `outcome: "usage_limit"` with the Run identity and available
usage evidence. No reset or provider fallback is attempted. Unknown accounting
stays null. Use disposable delegated model credentials only for explicitly opted-in
live evaluation; the regression tests use scripted models and disposable fixtures.

Legacy benchmark profiles and controller entry points have been removed. There is
no migration adapter for their configuration.

`operation: "cleanup"` uses the same request (even when `seconds` has reached zero).
It stops the disposable Run and reconciles only its owned resource ledgers, without
model access. It returns `cleaned` only after confirmed cleanup, and is idempotent.
History is retained for diagnosis. Missing Docker access or uncertain creation/
cleanup leaves the state intact and fails explicitly. The benchmark separately
terminates its controller process group; cleanup must not race an active controller.

Protected public checks execute the original `smoke.py` and `execution.py` in a
temporary directory with normal module/script context. Structural smoke validates
an optional execution declaration but never runs its script. Creating an executable
artifact is not proof that its external operation has already completed.
