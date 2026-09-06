# Product-owned benchmark connection

`fleet-bench` implements the generic `pocket-agent-v1` host-controller protocol.
The benchmark core does not import My AI Employee, choose its Harness, understand
its database, or manage its Docker resources. The controller is explicitly trusted
host orchestration software; all native model tools and independent checks use
the production isolated Docker path introduced by #81.

The controller creates a new public-fixture Git repository and private Fleet DB
under the supplied control directory. It reuses the ordinary CLI work orchestration
with that DB explicitly supplied; it does not monkeypatch execution or use the
normal user's Inspector DB. The only Fleet check is the benchmark's public structural
smoke check, copied verbatim. Actual scoring stays in the separate benchmark verifier.
Only the captured, Fleet-accepted candidate's `src/` and `output/` are materialized
into the disposable snapshot. No user repository promotion takes place.

The product Harness declares only untracked `__pycache__/*.pyc` files below `src/`
and `output/` (including nested packages) as generated outputs. Normal Python
imports may create these binary caches; they must not enter the text-edit proposal.
Tracked cache modifications remain captured and validated. Source files, result
artifacts and all changes under protected `input/`, `.fleet/` or `.git/` are not
excluded. This is not general binary-edit support or a relaxation of path checks.

## Runtime and local configuration

Build `docker/isolated-worker.Dockerfile`, record its immutable image ID, and use
`docker/benchmark-worker.Dockerfile` to add `/app -> /work`. Record the resulting ID
for the benchmark controller. This alias preserves the original task instructions;
it does not change the isolated worker's write or network policy. Do not mount a
Docker socket in Harbor, grant extra capabilities, or disable host AppArmor.

Install this product version in the host controller environment. Keep the following
operator profile under the benchmark's ignored `local/` directory, supplying the
actual image ID and the explicitly delegated auth file path **locally only**:

```json
{
  "protocol": "pocket-agent-v1",
  "profiles": {
    "employee-native-single": {
      "agent": "my-ai-employee",
      "mode": "single",
      "execution": "host-controller",
      "argv": ["fleet-bench", "--request", "{request}", "--response", "{response}"],
      "settings": {
        "isolated_worker": {
          "image": "sha256:REPLACE_WITH_64_HEX_IMAGE_ID",
          "auth_file": "/absolute/path/to/explicitly-delegated/auth.json",
          "native_process_limit": 512
        }
      }
    }
  }
}
```

Run all twelve unchanged tasks from pocket-agent-bench:

```sh
pocket-bench run --profile-file local/connections.json --allow-host-controller \
  --profiles employee-native-single --attempts 1 --concurrency 1 \
  --model gpt-5.6-luna --effort low --agent-seconds 180
```

The profile fixes a single Fleet node and one native invocation. The Harness budgets
600 cumulative process admissions; the native worker reserves 512, with independent
checks accounted separately. Fleet's existing graph also reserves time for parent
verification: the entire advertised wall allowance is not all model time. API tasks
can use the existing generic declared-script route; native interactive API access is
not claimed. Unknown cost and human active time remain unknown.

Every Docker container/network name is written to a private append-only resource
ledger **before** creation. Normal lifecycle cleanup is unchanged; a separate
`cleanup` protocol call also removes exactly those recorded names after a controller
timeout/cancellation. No global Docker prune or unrelated workload cleanup occurs.

`completed` in the response means the attempt returned, not that Fleet or the grader
accepted it. A failed Fleet attempt is submitted with its actual unchanged output
and normalized status for independent grading. Usage-limit failures are different:
they report `usage_limit` and stop subsequent model calls. No reset tickets, purchase,
automatic allowance expansion, provider change or fallback model is implemented.

The copied `integrations/legacy/legacy_benchmark_adapter.py` and adjacent
`fleet_wrapper.py` are historical
proposal-mode integration sources for old runtime snapshots, not the new execution
path. They must not be used with a current Fleet CLI as if they selected #81.

Crash recovery records both creation intent and acknowledged creation. If the
controller is interrupted while Docker creation is still in flight, cleanup does
not claim success based on a momentary absence: it retains the ledger and requires
operator recovery. An unconfirmed cleanup stops subsequent benchmark model calls.

Model-free tests validate the file contract, protected public fixture, cleanup
target validation and normal Harness schema. Full Docker/real-model twelve-task
validation is a separate explicit phase; passing unit tests alone is not evidence
of benchmark success.

## Initial native connection validation (2026-09-06)

The fixed twelve-task run `employee-native-v1-20260906` completed: four successes,
eight failures, no Harbor exceptions or unscorable trials. Data tasks were 3/3,
research 1/3, coding 0/3 and API 0/3. This is an **integration diagnostic**, not a
fair product-capability score or a comparison with historical single/team runs.

Evaluated product commit: `b64b808`; benchmark commit: `3230575` (pre-existing task
corrections preserved separately at `4fad7ae`). The worker image was
`sha256:d71061257e875361e1bf9baf2f0c2db51c0fb88828c33d53b00ee97ee3783993`
(Linux arm64, Codex 0.144.4). Source implementation digest:
`f886f07ea03f3a543c2702b08386e12030066467b8993e12fc9ec6972e9032ff`.
The task/verifier image was
`sha256:2446176326e8dfdf83d730bd9bafc696799d75f13fa5b469ea8adea672d50010`;
its historical embedded Fleet installation was not invoked.

All eight failures returned Fleet status `failed` without a worker failure record;
no accepted candidate was exported. The original controller omitted the graph's
stable failure code and removed its private DB after confirmed cleanup, so the
exact per-trial rejection causes cannot be recovered from those saved summaries.
Do not attribute every failure to the model or to a single inferred cause.

A subsequent credential-free reproduction found a controller Harness defect:
the public check imports its adjacent `execution.py`, creating bytecode under
protected `.fleet/public-checks/`. A native worker running this advertised check
therefore causes its otherwise valid candidate to be rejected. A real-orchestration
Docker regression failed before the fix and passed after disabling bytecode with
`python -I -B`; the public check contents and protected-path policy are unchanged.
Responses now retain `stable_code` for future diagnosis. These are **post-run fixes**:
the original model run has not been repeated and its grades remain unchanged.

All twelve controller cleanups were confirmed, and no Fleet candidate containers
or networks remained. No reset ticket, allowance purchase, model fallback or automatic
score-improving rerun was used. Token usage was observed; cost remains unknown.
See the benchmark's `examples/employee-native-v1.json` and validation document for
the sanitized complete results. Private profiles, auth paths and native traces are
not published.

## Bytecode-capture follow-up (2026-09-06)

The full `employee-native-v2-20260906` rerun at product `07ae94e` passed 11/12 tasks.
Its remaining `code-intervals` failure was traced to a runtime-captured binary Git
patch for an untracked `src/__pycache__/*.pyc` file. The text-path validator recognized
only `src/solution.py`, causing `INVALID_REQUEST` and then `NODE_EXECUTION_FAILED`.
This was not a malformed model-authored diff. The actual saved proposal reproduced
the mismatch without invoking a model or changing its original grade.

Product `a70741f` adds the narrow generated-cache policy described above. Deterministic
Docker tests create real bytecode and exercise normal Fleet acceptance; they fail
before this fix and pass after it. Capture tests also preserve tracked changes and
deletions and keep protected paths visible to validation; CI runs that regression
without model credentials.

The targeted `employee-cache-fix-20260906` run then passed `code-intervals` through
both Fleet acceptance and the unchanged independent grader. It used one attempt,
the same recorded task/worker images, `gpt-5.6-luna`, effort low and 180 agent-seconds.
This is **one targeted pass**, not a fresh 12/12 run; the earlier 11/12 result remains
unchanged. No reset ticket, purchase, fallback, repeated attempt or grader modification
was used. Costs remain unknown; private diagnostic state stays local.
