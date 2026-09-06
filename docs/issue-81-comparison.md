# Isolated worker comparisons

## Final bounded profile (2026-09-06)

Both arms completed the same whitespace repair with the aggregate admission guard
enabled. Each observed a real failed command, corrected the candidate, and passed
all four independent checks. No original source was promoted or modified.

| Measurement | Proposal | Isolated |
| --- | --- | --- |
| Fleet status | ready_to_promote | ready_to_promote |
| Independent checks | 4/4 | 4/4 |
| Worker invocations | 1 | 1 |
| Native process admissions / cap | 129 / 512 | 170 / 512 |
| Completed local command exit codes | 1, 0, 0 | 1, 0, 0, 0 |
| End-to-end seconds | 40.664 | 28.501 |
| Worker adapter seconds | 33.727 | 23.747 |
| Input tokens (including cached) | 48,781 | 54,482 |
| Cached input tokens | 40,960 | 39,936 |
| Output tokens | 1,276 | 591 |
| In-run human interventions | 0 | 0 |
| Human active time / monetary cost | unavailable | unavailable |

The model (`gpt-5.6-luna`, low), immutable runtime/CLI, task, initial tree and
acceptance checks are as described below. Both now disable shell snapshots and
have an enforced **total 512 native admissions per arm**, plus four independently
guarded verifier processes. The Harness explicitly declares 600 processes and
240 seconds. The isolated graph permits one invocation (local repairs inside it),
reserves 514 processes, and leaves two for parent checks. The legacy proposal graph
retains its repair semantics; the comparison transport shares its 512 admissions
across any invocations. Each observed arm used one invocation. Control-plane
container setup, Git capture, and model-free probes are not native admissions;
their time is included in wall supervision. Live PID caps also bound threads.

Candidate artifact digests:

- Proposal: `d4bd416824e863620717e25d73db0d35b455a1a4ef947268796a68e82b0dc106`
- Isolated: `360067b5fc67a979d982af7bea0eb9a7cf9a2f03cc7fbed915d7c5f183740990`

This is an exploratory implementation check, not an unbiased performance estimate.
The final configuration was chosen after failures below; no superiority claim or
success-rate pooling across configurations is justified. Authenticated experiments
used the same model without allowance resets, purchases, or provider/model changes.
No token/cost cap is claimed. Full, normalized final rows and the earlier failures
are in [the shareable report](issue-81-budget-comparison.json); raw databases remain private.

### Retained budget-calibration failures

| Configuration | Proposal | Isolated | What changed next |
| --- | --- | --- | --- |
| Cap 32, shell snapshots on | failed | budget exceeded | Model-free startup measurement |
| Cap 128, shell snapshots on | budget exceeded | budget exceeded | Disabled identical snapshot setting in both arms |
| Cap 128, shell snapshots off | budget exceeded | budget exceeded | Sized a useful native/helper-process allowance |
| Cap 512, shell snapshots off | accepted | accepted | Final profile; no further model run |

The first proposal failure escaped the test transport as an execution exception,
so that row has no completed worker accounting. Later comparison failures are
returned as typed budget failures with final admissions. Missing token totals for
interrupted invocations are **unknown**, not zero or evidence of no model usage.
The 32/128 failures do not indicate Usage Limit; they are local process-admission
limits. A no-auth, network-disabled five-second startup probe counted 52 admissions
with snapshots and 26 without (including its timeout helper). A three-command shell
control counted exactly five processes including its Python and shell launchers.
This establishes the count is not a textual command count or a concurrent PID cap.

## Earlier unguarded experiment (retained)

## Outcome

Both the proposal arm and the isolated arm reached Fleet's `ready_to_promote`
state on a small whitespace-normalization repair. Each had four successful
independent verification results (acceptance and regression at node and parent
levels). Neither arm promoted changes into the source repository.

This is a one-task, one-pair feasibility observation, **not evidence of general
productivity superiority**. At the time of this earlier experiment, aggregate native
process/tool budget enforcement was missing. The bounded experiment above supersedes
that limitation; delegated-credential/container trust assumptions remain explicit.

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

The following assessment describes the earlier experiment, not the final guard:

- `Harness.processes` currently reserves Fleet-mediated checks; the successful
  isolated request reserved 2 such processes but also reported 3 native commands.
  Docker's 128 live-PID ceiling does not bound cumulative native command launches.
  An aggregate budget was **not implemented in that version**. The final profile
  reserves and enforces native admissions; the old count remains historical evidence.
- Tokens are available at turn completion and recorded; a hard preemptive aggregate
  token/cost cap is not implemented. There is no measured monetary cost in this pair.
- A separate auth file is not inherently a least-privilege token. It delegates the
  signed-in account's existing authority. Native tools are denied command reads of
  the file by the tested inner sandbox, but the outer worker UID holds credentials.
- The CONNECT gateway restricts provider domains, not TLS paths or API operations.
  `seccomp=unconfined` remains a container/kernel trust assumption for the nested
  sandbox. This is not a hostile multi-tenant execution service.

The supported profile remains opt-in and limited to fixed Codex/offline Python tasks.
Its current guarantees and restrictions are described in [the profile](isolated-worker.md).
