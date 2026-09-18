# Isolated worker setup and tests

Fleet supports a prepared Linux Docker image running Codex 0.154.0 with its native
sandbox. Docker Desktop on macOS can supply the required process namespace.
Linux hosts must permit the nested native sandbox; a host that rejects it is
unsupported and fails closed. Do not disable host protections to make a test pass.

### Prepared dependencies and runtime readiness

Install immutable application dependencies beneath `/usr/local/lib` and executable
wrappers in `/usr/local/bin`. Both paths are already covered by the native minimal
read grant; Fleet does not grant arbitrary image or host paths. The shared command
PATH starts with `/usr/local/bin`. A wrapper sets its application's required
variables (for example `GEM_HOME`, `BUNDLE_PATH` or `NODE_PATH`) and invokes the
installed executable. Do not depend on image `ENV` surviving the native shell's
environment filtering. Use the same wrapper in worker commands and verification
checks. Relative links must resolve to readable image paths; links cannot confer
access to control directories. Dependencies remain read-only and outside captures.

For example, a derived image may install a Python package to
`/usr/local/lib/project-deps`, then install this executable wrapper as
`/usr/local/bin/project-tests`:

```sh
#!/bin/sh
export PYTHONPATH=/usr/local/lib/project-deps
exec /usr/local/bin/python -m unittest discover -s tests "$@"
```

Include dependencies and wrappers in the prepared image and select its immutable
image ID. Installation is an image-provider responsibility; Fleet does not install
missing packages or enlarge source limits after a command fails.

The worker recipe builds checksum-pinned bubblewrap 0.12.0 with one change: fresh
inner procfs mounts use `subset=pid`. Codex 0.154.0 selects `/usr/bin/bwrap`; its
bundled binary and integrity verification remain unchanged. This preserves a private
PID namespace and outer Docker proc masks. Global proc interfaces such as
`/proc/sys` and `/proc/meminfo` are deliberately unavailable. Applications requiring
those interfaces are not qualified by this contract.

The tested composition is Linux 7.2.0 ARM64, Docker 29.1.3 and Codex 0.154.0.
Older kernels can reject this restricted nested mount; a kernel update alone with
an unchanged helper did not fix the observed failure. Operators must supply a
compatible maintained host kernel; Fleet does not install kernels or change host
protections. Other architectures and runtime combinations require qualification.
Preflight tests the effective sandbox's private process status and memory maps,
not just version strings. Missing facilities report
`NATIVE_RUNTIME_PROCFS_UNAVAILABLE`; confinement/startup failures report
`NATIVE_SANDBOX_PREFLIGHT_FAILED`. This can reject previously accepted environments
whose native probe passed but whose application runtimes could not function.

Model-free qualification of that composition included RSpec 3.13.0, Vitest 3.0.5
on Node 24.13.0, read-only dependency and control-path checks, and the existing
14 Docker integration tests. It does not certify a complete application benchmark,
all native tool entry points or live-model behavior. Re-run opt-in integration tests
below against the actual prepared image; a skipped suite is not qualification.

Build the image explicitly, then put its immutable `sha256:` ID in the Run's
`isolation.image`. The image recipe is
[`docker/autonomous-worker.Dockerfile`](../docker/autonomous-worker.Dockerfile).
The build context contains only runtime recipes. `isolation.auth_file` is an
explicitly delegated model authentication file; never use ordinary history or
normal operator credentials as test fixtures. The image itself contains no auth.

The profile configures CPU, memory, concurrent PIDs and writable storage.
New `fleet init` configurations set `isolation.workspace_mb` to 1024 (MiB);
older profiles that omit it retain 256 MiB. This is distinct from the Run's
`snapshot_max_bytes` content allowance. See the [capacity settings](run-interface.md#snapshot-and-workspace-capacity)
for overrides, transfer bounds, memory implications, and dependency storage limits.
Workers receive copies of permitted inputs. The native sandbox adds resource
restrictions inside the container; the process guard terminates detached children
before capture. Actual changed files are returned, not converted to edit proposals.

Ordinary verification is documented in [development.md](development.md) and makes
no model calls. Additional model-free integration tests are opt-in:

```sh
docker build -f docker/autonomous-worker.Dockerfile -t fleet-isolation-test docker
export FLEET_TEST_DOCKER_IMAGE=$(docker image inspect --format '{{.Id}}' fleet-isolation-test)
.venv/bin/pytest tests/test_autonomous_container.py -q

# Additional image-wrapper/transitive-link tests through worker and check paths:
docker build -f docker/dependency-fixture.Dockerfile \
  --build-arg WORKER_IMAGE="$FLEET_TEST_DOCKER_IMAGE" -t fleet-dependency-test docker
export FLEET_TEST_DEPENDENCY_IMAGE=$(docker image inspect --format '{{.Id}}' fleet-dependency-test)
.venv/bin/pytest tests/test_dependency_container.py -q
```

These use disposable inputs and no authentication. On a host known to reject
nested sandbox startup, `FLEET_TEST_EXPECT_NATIVE_UNAVAILABLE=1` verifies explicit
denial instead of success. That check is not proof of an available native sandbox.
CI retains this distinction rather than relaxing Ubuntu's default LSM restrictions.

`FLEET_TEST_NATIVE=1 .venv/bin/pytest tests/test_autonomous_native_isolation.py -q`
checks the installed host Codex filesystem/network sandbox using disposable
canaries. It does **not** certify host-only process containment or enable host-only
production execution.

Live-model evaluation is a separate explicit action, consumes the selected
provider's allowance, and is not enabled by any ordinary or isolation test command.
Stop on Usage Limit; never redeem tickets, buy allowance or switch providers to
continue a test.

### Disposable local services

An operator may set `isolation.local_service_storage_mb` to an integer from 16
through 4096 (MiB) in the Run configuration. Omission or `null` leaves the added
service environment disabled. For example, `"local_service_storage_mb": 128`
provides a 128 MiB `/fleet-runtime` tmpfs. Choose a capacity for the application's
data; this also consumes the candidate's existing memory allowance. It is separate
from workspace and snapshot allowances. The setting is saved with the Run and
affects its identity; model output cannot enable it.

The enabled native profile supports loopback TCP **within one command** using
Codex's isolated network namespace and managed proxy. An empty external host
allowlist still denies external destinations. Existing Authority and gateway rules
continue to govern external access; local DB writes are disposable local work.
Pathname Unix sockets are unsupported. Set clients explicitly to `127.0.0.1` and
configure PostgreSQL with an empty Unix socket directory (`-k ''`). No host service,
host Docker socket, or another execution's service is exposed.

The option grants storage and a supported service workflow, not a promise to deny
all local socket operations when omitted: Codex's existing external-network mode
already allows command-local TCP on Linux. Its `allow_local_binding=false` flag is
not an enforceable Linux denial. Fleet does not rely on that flag as a boundary.

Use an image/project-owned test wrapper that starts services, waits for readiness,
runs the application test, and stops services in `finally` or a shell trap. Run all
of those steps in the **same command**. Each later command starts its own services;
a background process started in one command is not a supported endpoint for another
command. Data files can remain under `/fleet-runtime` during one candidate invocation,
but a new invocation, retry or independent verification starts with empty storage.
Verification must invoke the same reproducible setup wrapper, not assume the
worker's DB state survives. Fleet deliberately adds no persistent service manager.

`/fleet-runtime` is UID-1000-owned, size-limited and mounted with `noexec,nosuid,nodev`.
It is outside both ordinary and partial source capture. Service data/logs must go
there; Fleet does not infer which files placed under `/work` are database data.
Do not link runtime files into deliverables: snapshot validation rejects links
escaping the captured source. Worker cleanup is helpful but not trusted: the existing
process guard and candidate owner stop descendants and destroy storage on completion,
failure, cancellation, timeout and controller loss. If cleanup is unconfirmed,
existing capture/acceptance restrictions still apply.

Native preflight checks runtime writes and a local TCP round trip before work.
Unavailable local capability reports `LOCAL_SERVICE_SANDBOX_UNAVAILABLE`; missing
private procfs reports the separate readiness error described above. These probes
do not establish application correctness or waive independent verification.

The optional small application fixture tests PostgreSQL SQL insertion plus Redis
cache round trips, orderly restart between commands, fresh verification, bounded
storage, network denial and partial capture without model calls:

```sh
docker build -f docker/service-fixture.Dockerfile \
  --build-arg WORKER_IMAGE="$FLEET_TEST_DOCKER_IMAGE" -t fleet-service-test docker
export FLEET_TEST_SERVICE_IMAGE=$(docker image inspect --format '{{.Id}}' fleet-service-test)
.venv/bin/pytest tests/test_local_service_container.py -q -s
```

The fixture installs Debian Bookworm PostgreSQL 15 and Redis 7 and prints exact
versions. It is not a PostgreSQL 16/pgvector, browser, or complete Chatwoot fixture.
Run the actual application's wrappers in its qualified image before claiming
benchmark readiness. No live-model test is implied by these commands.

### Temporary model capacity failures

Fresh `fleet init` configurations allow two transport retries per stage. Existing
saved configurations and an explicit `transport_retries: 0` remain unchanged.
The known Codex terminal `turn.failed` message `Selected model is at capacity.
Please try a different model.` is classified as `MODEL_AT_CAPACITY`, not as an
invariant or model-output failure. The observed CLI format has no dedicated code;
classification is limited to this exact error message and may need updating when
Codex changes its output. Ordinary model/tool text cannot trigger it.

Capacity retries keep the same model/backend, wait 5 then 10 seconds (further
configured retries cap each wait at 60 seconds), and use the existing durable
transport allowance and Run budgets. Cancellation and wall budgets remain active
during waits; an invocation's own timeout covers that invocation, not a later wait.
Owned environments are cleaned before recovery. Potentially completed external
writes still require uncertainty resolution rather than automatic replay. Exhaustion
reports `MODEL_AT_CAPACITY_RETRIES_EXHAUSTED`. Quota stops never trigger recovery,
model switching, allowance purchases, or resets.

Cumulative native process admission limits have been removed. `native_process_limit`
is no longer a supported field; old configurations and saved Runs containing it may
fail strict validation. There is no migration or compatibility execution path. Use
a current configuration for new Runs; stored history is not rewritten or deleted.
Sequential process creation does not exhaust an implicit lifetime allowance. The
concurrent PID limit (128 by default), CPU/memory/workspace controls, cancellation
and explicitly configured time budgets still apply. The lifetime supervisor remains
responsible for stopping/reaping descendants and confirming safe snapshot capture.
