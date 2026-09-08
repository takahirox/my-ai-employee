# Candidate observation and validation

The local Codex proposal transport can inspect and execute a disposable copy before
returning typed edits. Enable `worker.scratch_validation: true` in the Harness and
allow only `codex_cli` for this opt-in transport. Ordinary configurations keep their
existing read-only proposal behavior; the Docker isolated-worker profile remains a
separate, offline profile. Neither profile silently falls back to unrestricted execution.

The runtime copies the current candidate into a fresh, size-bounded directory. It
excludes host Git metadata and untracked operator files; subsequent disposable
candidate snapshots include their pending new files. Only the copy is writable.
The source repository, input directory, Harness and Codex authentication remain
protected. The worker must return typed edits: changes in scratch are never applied
implicitly. Existing evaluation/repair transitions still enforce attempts, deadlines,
exact-candidate evidence and promotion rules. Scratch trials happen within the same
native invocation; a later accepted repair receives a fresh current-candidate copy.

## Network authority

For network observation, declare exact lowercase hosts in both
`worker.observation_hosts` (Harness) and `worker_observation_hosts` (Operator Config).
The Harness cannot add hosts beyond the operator allowlist. Empty lists keep native
network access disabled. URLs, ports and wildcard rules are rejected: this is a
host-scoped capability, not a per-HTTP-method or per-port grant. Grant local literals
only for deliberately delegated local services. Do not infer network authority from
a model response or a URL found in task text.

The transport explicitly enables Codex's managed network proxy and its domain
allowlist together. It uses a per-scratch listener port; a bind conflict fails closed.
Codex versions before 0.153.4 are rejected for this mode. A credential-free native
preflight checks scratch writes, source write denial and the exact managed proxy URL
before worker generation. Unsupported native isolation fails instead of consuming
the worker's entire reasoning budget. Domain enforcement was tested with Codex
0.153.4 on Linux: allowed loopback HTTP succeeds, an unlisted local address returns
403, and bypassing the proxy cannot reach the service. Merely enabling network with
an inactive proxy would not enforce this contract.

For JavaScript observation install `chromium-headless-shell` (the optional
`docker/observation-browser.Dockerfile` layers it on a delegated Debian runtime).
Full desktop Chromium needs a singleton socket unavailable in this sandbox. The
worker receives instructions to use the managed HTTP proxy, disable Chromium's
implicit loopback bypass, and keep browser data and temporary files in scratch.
The native integration test covers a page that fetches JSON and updates its DOM;
it does not use authentication or a model. Run it with an explicitly selected image:

```sh
FLEET_TEST_OBSERVATION_IMAGE=<local-image> .venv/bin/pytest tests/test_worker_observation.py -k native
```

On hosts that cannot enforce Codex's native profile, use an appropriate supported
runtime. Do not disable enforcement to make an integration test pass.

## Public acceptance and benchmark connection

`benchmark_default` enables scratch validation and an independent parent semantic
review using the existing Sol high strategy; assessment and worker routing keep
the built-in adaptive strategy set. These additional checks consume the same total
trial allowance. This revised connection is not identical to the historical default
measurement at `51c7321` and should be identified separately in comparisons.

The trusted controller request may supply `settings.observation_hosts` and
`settings.public_acceptance`. The latter contains `checks` (the existing GoalChecks
schema, bound to the exact original instruction) and `commands` (a mapping of check
names to bounded inline Python source). The connection registers the commands and
passes an acceptance file to `fleet work`. Runtime authority and evaluator bindings
come from that frozen Harness and Goal, not worker-authored tests or claims.

Use public requirements to check executable behavior, required fields and types,
input coverage, and required protocols where applicable. Hidden grading data is
never loaded by the connection. Semantic review covers the original request beyond
structural smoke; it is fallible and does not replace an explicit executable check.
Missing check coverage must be reported as a limitation, not assumed correct.

State-changing service calls are not harmless dry runs. Test those using local
fakes or a separately delegated disposable service. The existing external execution
transport still runs a declared script once after final submission: do not also
perform its side effects during observation. Network permission never authorizes
extra business operations.

## Bounded diagnostics and output

Codex JSONL is filtered as it arrives. Final messages, usage and allowlisted error
classes are retained; tool/reasoning bodies are discarded. Retained stdout keeps the
request limit. Raw stdout is limited to 32 times that limit, capped at 64 MB, and
one event to 4 times it, capped at 4 MB. Ordinary commands keep their original raw
limits, and stderr remains separately bounded. Oversized raw streams/events still
terminate the owned process group with an output-limit failure.

The benchmark exports body-free model process diagnostics, sanitized worker boundary
diagnostics and plan-review failure classifications before discarding its private
state. These include exit status, byte counts, limits, cleanup status and invocation
bindings. Classified transport messages are hints, not proof of backend root cause;
unknown failures stay unknown. Provider message bodies and arbitrary resource fields
are not exported. Quota detection stops further model work without skipping safe
diagnostic export or redeeming allowance.

The historical plan-review and ledger-large call failures cannot be retrospectively
identified from missing logs. A synthetic large tool output reproduced the output
limit path, and the streaming change fixes that path; it does not prove the original
ledger failure had that cause. Report new trial evidence separately.
