# Benchmark transport

`fleet-bench --request request.json --response response.json` runs the ordinary
Fleet engine with explicit disposable state. It does not use a benchmark-specific
worker loop or edit replay path. The response uses `pocket-agent-v1`; `completed`
means the controller finished, not that the external grader found the result correct.

The request has `protocol`, `operation: "run"`, absolute `control_dir`, `workspace`
and `public_checks`, `instruction`, positive `seconds`, `writable_roots: ["src",
"output"]`, and a complete current `config`. Both public input directories must be
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
