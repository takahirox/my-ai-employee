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
