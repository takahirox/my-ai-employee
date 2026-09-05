# Experimental isolated worker profile (#81)

This first milestone supports one fixed-routing Codex worker and Python checks. It is
opt-in, preserves the proposal workflow, and never falls back to host execution.

The operator configuration accepts `isolated_worker` with `backend: docker-codex-v1`,
an **already built immutable Linux Docker image ID** (`sha256:...`), and an explicit
absolute `auth_file`. The image must include Python 3.12+, Git, Codex, and uid 1000.
No image is pulled or built implicitly. The Harness must also explicitly set
`worker.isolated_workspace_tools: true`: this grants local development tools inside
the candidate container, not unrestricted host processes. Existing path restrictions
and promotion authority still gate the captured edit.

`docker/isolated-worker.Dockerfile` supplies a minimal runtime recipe using the Codex
version exercised in the offline tests. Build it explicitly and record the returned
`docker image inspect --format '{{.Id}}' <tag>` ID in operator configuration. For
release reproducibility, override the two base-image arguments with pinned digests;
floating base tags alone are not a reproducible build. The existing local benchmark
runtime, not a newly built copy of this recipe, was used for the recorded tests.

The profile declares CPU, memory, live PID and tmpfs limits. Wall supervision covers
the invocation and its internal corrections; all descendants are destroyed before
capture and at cancellation/timeout. PID limits are **concurrent container processes**,
not a fabricated count of all historical forks. Fleet-mediated process/attempt budgets
remain separate. Available native usage is retained; unknown cost/tokens are not zero.
Hard aggregate native tool/usage accounting still needs evaluation before general use.

For this milestone the Harness must be offline, use only declared `python`/`python3`
commands at `.` with no host environment, and have no install or model/browser-review
requirements. Host absolute interpreter paths are not silently remapped. Unsupported
combinations fail before model execution. The native worker can edit/run/observe/repair
in one task; Fleet captures actual Git changes, applies the normal edit/path checks,
and runs each independent check in a fresh **credential-free, network-disabled**
container. Model-reported success is not acceptance.

## Isolation and credentials

No host directory, source `.git`, Fleet database, Docker socket, or unrelated untracked
host files is mounted. Candidate data is copied from the managed worktree. A root-owned
Git baseline cannot be rewritten by the uid-1000 worker. Only untracked outputs under
the accepted Harness's generated-path patterns are excluded; tracked mutations are not.
The root filesystem is read-only with all capabilities dropped and no-new-privileges.
The experimental runtime uses `seccomp=unconfined` for nested Codex sandbox compatibility;
Docker/host-kernel integrity is therefore an explicit trust assumption, not a claim of
VM-grade containment. Do not run it against hostile tenants on a shared privileged host.

An explicitly delegated auth file is copied into the disposable worker's
temporary home. Do not point it at broad ordinary host credentials without reviewing
that delegation: the worker user can read this file. It is absent from independent
verification and is not stored in history. Native arbitrary stdout/stderr is not
persisted; bounded normalized activity, usage and exit status are stored instead.
A separate file does not itself reduce account permissions or create a separate
usage allowance; do not describe ordinary login tokens as inherently scoped.

With delegated authentication, an internal Docker network reaches a dedicated CONNECT
gateway restricted to the exact model-provider hostnames on port 443; direct external
networking is probed and must fail. The gateway does not inspect TLS paths or enforce
HTTP-operation semantics. This is a provider-domain restriction, **not** perfect
separation between model and tool traffic. No credentials or request bodies are logged
by the gateway. Codex tool-sandbox networking is independently disabled. The same named
permission profile runs as a preflight before each model invocation and denies command
reads of the scoped auth file. The temporary home is a dedicated `/home/fleet` tmpfs,
outside `/tmp`, for CLI helper compatibility.

Usage-limit errors stop the graph rather than launching a Fleet repair. No reset-ticket,
purchase, provider-switch or allowance-expansion operation is implemented or authorized.

The [official Codex permissions documentation](https://learn.chatgpt.com/docs/permissions)
and [non-interactive execution documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
informed the explicit sandbox/ephemeral/config choices; native CLI flags alone are not
treated as an OS boundary.

## Evidence and remaining evaluation

`tests/test_issue81_isolation.py` uses an explicit `FLEET_TEST_DOCKER_IMAGE` opt-in for
credential-free Docker tests. A scripted native worker observes an actual failed
Python check, repairs it, and passes a fresh Fleet check via normal CLI orchestration.
Tests also check host-source preservation, no host Git/untracked-secret copy,
read-only protected paths, direct-network denial and container/descendant cleanup.

The [first real-model paired comparison](issue-81-comparison.md) completed both
workflows with independent acceptance. It exposed and fixed an exec CLI selection
bug that the initial model-free preflight missed. A single small task does **not**
prove a model productivity improvement. For broader comparison, run both against the
same current base and checks, preserve model/effort/time allowances, report exact
accepted outcomes, protocol failures, local corrections, wall time, interventions and
available usage. Human active time or cost not measured must remain unknown. The old
`api-corrected-v1` benchmark used a different local base and cannot substitute for it.

The review identified an unresolved aggregate native-tool/process-budget gap;
the live-PID cap is not an aggregate command count. Do not close #81 until its
remaining required guarantees are implemented. Never redeem a reset ticket to
finish an evaluation.
