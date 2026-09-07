# Default configuration benchmark connection

`fleet-bench-default REQUEST.json` runs ordinary adaptive `fleet work` defaults
inside an explicitly prepared disposable Pocket Agent Bench task container. It
expects `/app`, `/opt/pocket` public checks, `/opt/pocket/guarded-codex`, and the
explicitly delegated container-local login. It is not a host sandbox or a replacement
for the separate isolated `fleet-bench` host controller.

The request's remaining seconds include setup and patch export. The connection
deducts setup and a bounded return reserve before assigning Fleet's wall budget.
Product stdout/stderr stream directly to the trial's log files, preserving terminal
evidence during interruption. The accepted patch is applied only to the disposable
task for independent grading. Operator history must never be mounted into this fixture.

For new, nonempty LF text files, the Codex edit transport accepts `files` instead
of `unified_diff`: an array of `{path, content}` objects matching the exact ordered
`paths` declaration. Runtime code produces the new-file diff. Mixed representations,
duplicates, traversal, NUL/CR content, and oversized inputs are rejected. Existing
files still require an ordinary edit diff; new-file proposals cannot overwrite
them. Every compiled change retains the normal policy, path, evidence, and promotion
checks. Full benchmark success must be measured separately from model-free tests.
