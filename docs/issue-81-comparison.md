# Isolated worker comparison: first real-model pair

## Outcome

Both the proposal arm and the isolated arm reached Fleet's `ready_to_promote`
state on a small whitespace-normalization repair. Each had four successful
independent verification results (acceptance and regression at node and parent
levels). Neither arm promoted changes into the source repository.

This is a one-task, one-pair feasibility observation, **not evidence of general
productivity superiority**. #81 remains open because aggregate native process/tool
budget semantics and stronger credential separation still need resolution.

## Controlled setup

- Model: `gpt-5.6-luna`, effort `low`, one worker; web search and native multi-agent
  features explicitly disabled in both arms.
- Runtime: Codex CLI `0.144.4`, Linux arm64, the same immutable image in both arms:
  `sha256:2446176326e8dfdf83d730bd9bafc696799d75f13fa5b469ea8adea672d50010`.
- Fleet base: `bcc1b5f`, with the exec permission-selection correction in this PR.
  The later stderr-only quota safeguard and report-serialization fix were tested
  deterministically, not with additional model calls.
- Identical initial Git tree: `42224425c0fdfe775d73c2d457fa01b9eb8cb916`.
- Identical task, visible checks, fixed routing, 240-second Harness wall allowance,
  and the existing fixed graph's at-most-two attempts / one Fleet repair ceiling.
  Each reserved worker attempt had a 120-second wall allowance. Both successful
  arms needed just one worker result and no Fleet repair.
- Proposal first, isolated second. Cache/order effects are not controlled by this
  single pair; there is no randomization, confidence interval or human adjudication.

The task starts from:

```python
def normalize_label(value):
    return value.strip().lower().replace(" ", "-")
```

The requested correction collapses runs of spaces, tabs and newlines to a single
hyphen while preserving lowercase, empty strings and existing hyphens. The initial
acceptance check genuinely fails and the existing-behavior regression check passes.
Both arms receive an instruction to observe the failing check before correcting it.

## Observed results

| Measurement | Proposal | Isolated |
| --- | --- | --- |
| Fleet final status | ready_to_promote | ready_to_promote |
| Independent checks succeeded | 4/4 | 4/4 |
| Worker results | 1 | 1 |
| Worker protocol failures after CLI correction | 0 | 0 |
| Observed failed local commands | 1 | 1 |
| Observed successful local commands | 1 | 2 |
| Worker adapter duration, seconds | 24.411 | 20.984 |
| End-to-end measured wall time, seconds | 31.432 | unavailable |
| Input tokens (including cached) | 36,019 | 54,358 |
| Cached input tokens | 22,016 | 41,728 |
| Output tokens | 894 | 550 |
| Human interventions during the successful pair | 0 | 0 |
| Human active time / monetary cost | unavailable | unavailable |

Worker duration includes the adapter's container and transport overhead; it is not
pure model compute time. Command counts come from native completed command events,
not a count of every descendant OS process. A successful command event is not itself
Fleet acceptance. Input and cached-input counts must not be added together.

Exact accepted candidate artifact SHA-256 values:

- Proposal: `360067b5fc67a979d982af7bea0eb9a7cf9a2f03cc7fbed915d7c5f183740990`
- Isolated: `b4d9adaf0e6bfb9e9fd259808a474dfb539b0339841d7bee81af1243ce796a78`

The proposal used `'-'.join(value.split()).lower()`. The isolated arm used
`re.sub(r'\s+', '-', value.strip().lower())`. Both pass the declared checks; neither
solution nor this small test suite establishes correctness for all possible inputs.

## Experiment failures and recovery

An initial setup pair failed before model execution because `--permission-profile`
was mistakenly passed to `codex exec`. That option belongs to `codex sandbox`;
`exec` selects the same profile using `-c default_permissions=...`. Its failures
are setup failures, not discarded unfavorable model samples. The Docker regression
now tests the actual native argument builder, not merely `codex exec --help`.

After correction, both model runs and their Fleet verification completed. The
reporter then failed to serialize the isolated arm's immutable activity mappings
with the standard JSON encoder. The table above was recovered from saved Fleet
records and the already-written proposal report, without rerunning the models.
The isolated end-to-end monotonic timer was not persisted before that reporter
failure, so it remains unavailable. Worker duration and usage were persisted.
The reporter now uses Fleet's canonical serializer with an offline regression test.

Setup/debugging effort is excluded from the zero in-run intervention count and was
not timed. No usage-reset ticket, allowance purchase or provider/model fallback was
used. Private authentication, operator configuration and raw databases are not
published with this report.

## Reproduction

The opt-in test `tests/test_issue81_live_comparison.py` contains the complete fixture
and both transports. It invokes real Fleet CLI orchestration in both arms; the
proposal adapter and wire format are unchanged. A test-only process executor runs
its Codex invocation inside the same container profile (read-only candidate tools)
and routes verification to fresh credential-free, network-disabled containers.
The isolated arm uses the production `IsolatedCodexWorker` and verifier directly.
Thus this compares worker workflows under a common Docker transport, not the old
host-native deployment against Docker or the full pocket-agent-bench suite.

Run only on a trusted local machine with explicit operator-delegated authentication:

```sh
env PYTHONPATH=src \
  FLEET_LIVE_IMAGE=sha256:REPLACE_WITH_IMMUTABLE_IMAGE_ID \
  FLEET_LIVE_AUTH_FILE=/absolute/path/to/delegated/auth.json \
  FLEET_LIVE_MODEL=gpt-5.6-luna \
  FLEET_LIVE_OUTPUT=/absolute/path/to/new-private-results-directory \
  python -m pytest tests/test_issue81_live_comparison.py --tb=short
```

No opt-in means no real model test. Do not configure this authentication in public
CI. Keep the pytest temporary records private. Task failures are recorded outcomes,
not instructions to retry until a favorable result appears. Usage-limit errors must
stop the pair, and missing measurements must not be converted to zero.

## Budget and security assessment

Validated: read-only container root, no host mounts, protected Git baseline,
credential-free independent verification, CPU/memory/live-PID limits, direct-network
denial, rejection of non-provider CONNECT destinations, and timeout/cancellation
cleanup. Both real-model arms preserve the original source.

Remaining limitations are material:

- `Harness.processes` currently reserves Fleet-mediated checks; the successful
  isolated request reserved 2 such processes but also reported 3 native commands.
  Docker's 128 live-PID ceiling does not bound cumulative native command launches.
  A shared aggregate tool/process budget is therefore **not implemented**. Do not
  present the existing Harness count as an aggregate native-command cap.
- Tokens are available at turn completion and recorded; a hard preemptive aggregate
  token/cost cap is not implemented. There is no measured monetary cost in this pair.
- A separate auth file is not inherently a least-privilege token. It delegates the
  signed-in account's existing authority. Native tools are denied command reads of
  the file by the tested inner sandbox, but the outer worker UID holds credentials.
- The CONNECT gateway restricts provider domains, not TLS paths or API operations.
  `seccomp=unconfined` remains a container/kernel trust assumption for the nested
  sandbox. This is not a hostile multi-tenant execution service.

Keep the profile experimental and opt-in; do not close #81 on this experiment alone.
