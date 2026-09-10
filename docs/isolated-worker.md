# Isolated worker setup and tests

Fleet supports a prepared Linux Docker image running Codex 0.154.0 with its native
sandbox. Docker Desktop on macOS can supply the required process namespace.
Linux hosts must permit the nested native sandbox; a host that rejects it is
unsupported and fails closed. Do not disable host protections to make a test pass.

Build the image explicitly, then put its immutable `sha256:` ID in the Run's
`isolation.image`. The image recipe is
[`docker/autonomous-worker.Dockerfile`](../docker/autonomous-worker.Dockerfile).
The build context contains only runtime recipes. `isolation.auth_file` is an
explicitly delegated model authentication file; never use ordinary history or
normal operator credentials as test fixtures. The image itself contains no auth.

The profile configures CPU, memory, process admissions and writable storage.
Workers receive copies of permitted inputs. The native sandbox adds resource
restrictions inside the container; the process guard terminates detached children
before capture. Actual changed files are returned, not converted to edit proposals.

Ordinary verification is documented in [development.md](development.md) and makes
no model calls. Additional model-free integration tests are opt-in:

```sh
docker build -f docker/autonomous-worker.Dockerfile -t fleet-isolation-test docker
export FLEET_TEST_DOCKER_IMAGE=$(docker image inspect --format '{{.Id}}' fleet-isolation-test)
.venv/bin/pytest tests/test_autonomous_container.py -q
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
