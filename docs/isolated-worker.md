# Bounded isolated worker profile (#81)

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

The profile declares CPU, memory, live PID and tmpfs limits, plus
`native_process_limit` (default 512). Linux seccomp notification gates non-thread
fork/vfork/clone **before execution**. The initial native process counts as one;
each admitted process-creation attempt consumes one even if the syscall later fails.
This is a conservative cumulative process-admission count, not a shell-command count.
Threads remain bounded by the concurrent container PID limit; exec does not create
a new process. Native tools cannot replace the listener or ptrace the supervisor.
clone3 returns ENOSYS (libc fallback), so unsupported programs fail rather than bypass
accounting. Linux x86-64 and arm64 are supported; unsupported kernel/ABI mechanisms
fail the model-free preflight. No extra capabilities or host mounts are granted.

The accepted single-node graph reserves native admissions **plus** node verification
processes, leaving room for parent verification within both Harness and operator
process limits. One native invocation is permitted, with no Fleet retry/repair after
failure; local corrections share that invocation's limit. Each declared independent
Python check has one process admission: checks requiring subprocesses are unsupported
in this milestone and fail closed. Time supervision includes local corrections and
destroys the entire environment at timeout/cancellation. Available native usage is
retained; unknown cost/tokens are not zero. No token/dollar cap is claimed: the Harness
does not currently declare one, and native token counts arrive at turn completion.

The Harness must explicitly budget enough processes (for example 600 for a
512-admission worker and two checks at each verification level). Its default 40
is insufficient for this profile and is **not** silently increased. A model-free
five-second startup measurement observed 51 admissions including the timeout helper;
32 cannot accommodate this CLI's startup. That failed comparison is retained.
The initial 128-admission pair also hit its limit with shell snapshots enabled.
`features.shell_snapshot=false` now disables that initialization optimization in
both comparison arms; the same five-second, credential-free measurement fell from
52 to 26 admissions. The 128-admission limit still exhausted after two/three local
commands: native tools also spawn many helper processes. The final 512-admission
profile allows room for a useful bounded local loop; it is not 512 shell commands.
All exploratory 32/128 failures are retained separately from the final configuration.
This configuration is described
in the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Minimal additions to an existing valid operator configuration and Harness:

```json
{
  "isolated_worker": {
    "backend": "docker-codex-v1",
    "image": "sha256:REPLACE_WITH_IMMUTABLE_IMAGE_ID",
    "auth_file": "/absolute/path/to/delegated/auth.json",
    "native_process_limit": 512
  }
}
```

```json
{
  "worker": {"isolated_workspace_tools": true},
  "budgets": {"wall_seconds": 240.0, "processes": 600, "worker_turns": 1}
}
```

These are fragments, not replacements for routing, commands, paths or acceptance
definitions. The reproducible [paired benchmark](issue-81-comparison.md#reproduction)
constructs a complete fixture and writes private JSON results. It is distinct from
the generic pocket-agent-bench suite: that suite's existing `fleet-single` adapter
runs proposal mode inside Harbor and does not yet select this Docker host adapter.
Do not mount a Docker socket inside a benchmark task container to connect them.

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

Docker's default AppArmor policy on the Ubuntu CI runner denies bubblewrap's nested
mount setup (`Failed to make / slave: Permission denied`). That deployment is
**unsupported** for native iteration: preflight stops before the worker runs. CI
asserts this refusal explicitly while exercising the x86-64 admission guard; local
Docker Desktop/Linux arm64 exercises the positive native path. No host AppArmor
setting is disabled and no privileged fallback is provided. Do not assume all
Linux Docker installations can run this profile merely because the guard works.

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

`tests/test_native_process_guard.py` additionally exercises cumulative admission,
thread-originated forks, CLONE_UNTRACED, detached descendants, report tampering,
supervisor termination, nested-listener denial and actual Codex sandbox compatibility.
The supervisor's final accounting overwrites untrusted report bytes only after all
worker descendants are killed/reaped. Its report inode and parent are root-owned;
any supervisor error rejects the candidate. Raw tool output is never accounting
authority. The design uses [Linux seccomp](https://www.kernel.org/doc/html/latest/userspace-api/seccomp_filter.html)
and [notification CONTINUE](https://man7.org/linux/man-pages/man2/seccomp_unotify.2.html)
only for scalar syscall admission; no mutable userspace pointers are inspected.

The [first real-model paired comparison](issue-81-comparison.md) completed both
workflows with independent acceptance. It exposed and fixed an exec CLI selection
bug that the initial model-free preflight missed. A single small task does **not**
prove a model productivity improvement. For broader comparison, run both against the
same current base and checks, preserve model/effort/time allowances, report exact
accepted outcomes, protocol failures, local corrections, wall time, interventions and
available usage. Human active time or cost not measured must remain unknown. The old
`api-corrected-v1` benchmark used a different local base and cannot substitute for it.

This remains a local, explicitly delegated, opt-in profile, not a general hostile
multi-tenant service. The provider-domain and credential-delegation trust limitations
above are unchanged. Never redeem a reset ticket to finish an evaluation.
