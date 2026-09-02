# Public operational incident reporting runbook

Fleet can prepare a deliberately small report for an unexpected Fleet product failure and, only through an operator command, publish it to one public GitHub repository. This is an opt-in operational channel, not a vulnerability-reporting channel. It is off by default.

## Configure the Project Harness

Add an explicit `incident_reporting` block to the repository's `.fleet/project.yaml`. Reporting defaults to `off`; the documented default opt-in posture is `approval_required`:

```yaml
schema_version: 2
incident_reporting:
  mode: approval_required
  target_repository: owner/public-repository
  repository_key_env: FLEET_INCIDENT_KEY
  outbox_path: ~/.fleet/incident-reporting/outbox.sqlite3
  retention_hours: 168
  approval_hours: 24
  daily_limit: 3
  pending_cap: 20
```

In `off` mode Fleet rejects target, key, and auto-allowlist fields. Enabled modes require a single public GitHub `owner/repository` target and the name of an environment variable containing a repository-specific key. Provisional Harnesses cannot enable reporting. Harness configuration records repository-scoped intent; it contains neither the key nor a GitHub token and does not itself authorize a network call.

Keep `approval_required` unless there is a reviewed reason to omit per-report approval. In `auto` mode, both allowlists must be explicit, non-empty, unique, and narrow:

```yaml
incident_reporting:
  mode: auto
  target_repository: owner/public-repository
  repository_key_env: FLEET_INCIDENT_KEY
  auto_categories: [worker_boundary_failure]
  auto_failures: [runtime_error]
```

The only accepted category values are `trust_kernel_failure`, `persistence_failure`, and `worker_boundary_failure`.
The only accepted failure values are `assertion_error`, `os_error`, `runtime_error`, `type_error`, and `value_error`. Wildcards, unknown values, and an empty category or failure allowlist are rejected. A report in `auto` mode must match both allowlists. `auto` removes the approval requirement for a matching report; it does not make the Harness itself a publishing action.

## What qualifies

Preparation runs only after a Graph run has status `failed` and exactly one authoritative, current terminal closure. Fleet then accepts only its closed mapping of internal Doctor codes:

- `deadline_watchdog_timeout`
- `structured_output_missing`
- `envelope_invalid`
- `worker_result_absent`
- `process_cleanup_failed`
- `diagnostic_persistence_failed`
- `repair_exhausted`
- `diff_hunk_ambiguous`
- `run_lease_expired`
- `owner_fence_violation`

Succeeded, exhausted, blocked, cancelled, and skipped outcomes are not eligible. Neither are user-code failures, test failures, policy denials, approval waits, invalid requests, expected cancellations, unknown incident codes, or non-authoritative closures. In particular, a denied operation or a failing project verification command is not a Fleet product incident.
Reporting never changes the run's terminal outcome, and preparation cannot publish.

## Fixed public schema

The public JSON object is closed: extra fields and nonconforming types are rejected. Its complete schema is:

| Field | Public value |
| --- | --- |
| `schema_version` | literal `1` |
| `category` | one of the three categories above |
| `terminal_state` | literal `failed` |
| `disposition` | literal `internal_product_failure` |
| `failure` | one of the five failure classes above |
| `exception_class` | `AssertionError`, `KeyError`, `OSError`, `RuntimeError`, `TimeoutError`, `TypeError`, or `ValueError` |
| `stage` | `runtime`, `storage`, `policy`, or `worker_boundary` |
| `version` | validated semantic version, at most 128 characters |
| `commit` | 40 lowercase hexadecimal characters |
| `duration_bucket` | bounded integer bucket from 0 through 3,600 seconds |
| `memory_bucket` | bounded integer bucket from 0 through 8,192 MiB |
| `reproduction` | literal `synthetic_reproduction_v1` |
| `fingerprint` | 64 lowercase hexadecimal characters |
| `occurrences` | integer from 1 through 999 |

`synthetic_reproduction_v1` is a public contract marker, not captured reproduction steps. Fleet does not reconstruct or publish the original inputs. Reproduction must use maintainer-created synthetic inputs derived only from the public classifications and build coordinates.
A model cannot author the public body, and the private diagnosis cannot be used as prose: Fleet deterministically validates, composes, scans, sizes, and renders only the fields above.

The public report and its title, labels, marker, and occurrence summary must pass the public sink checks. The JSON and rendered body are capped at 4,096 bytes. The following data is forbidden and must never be put into incident publishing:

- private diagnosis detail, raw evidence, exception messages, stack traces, logs, stdout, stderr, prompts, tasks, conversations, transcripts, user or test output, source code, file contents, patches, diffs, artifacts, and artifact digests;
- filesystem paths, private URLs, SSH locations, repository-local filenames, branches, workspaces, host or user names, IP addresses, environment data, process arguments, internal identifiers, UUIDs, canaries, and free-form messages;
- credentials, passwords, authorization headers, bearer values, API keys, access or refresh tokens, private keys, GitHub/OpenAI/AWS-style tokens or keys, and the repository key or its environment-variable name;
- personal, customer, proprietary source, or other sensitive data, regardless of whether it matches a known secret pattern;
- any field not listed in the fixed schema, any unknown enum, or any value that fails the type, format, character, size, or sensitive-text checks.

Fleet, not GitHub, validates the exact labels `ai-employee-incident` and `incident:CATEGORY` before transport. This runbook does not claim that GitHub validates or pre-creates those labels.

## Private outbox, retention, and deduplication

The outbox defaults to `~/.fleet/incident-reporting/outbox.sqlite3`. It must be an absolute or home-relative local path, never a URL or traversal path. Fleet creates a missing parent privately with mode `0700`; an existing parent must be a non-symlink directory with no group or other permission bits. The database must be a non-symlink regular file and is set to mode `0600`. Store it on a trusted local filesystem and include it in the operator's private data-retention and backup policy.

Only validated public report JSON and bounded metadata are stored in this outbox; raw evidence and private diagnosis text are not. Expired rows are purged when outbox operations run. Defaults and accepted bounds are:

| Control | Default | Accepted range |
| --- | ---: | ---: |
| retention | 168 hours | 1-720 hours |
| approval window | 24 hours | 1-168 hours |
| publications per target repository per UTC day | 3 | 1-20 |
| pending entries per target repository | 20 | 1-100 |

Occurrence counts saturate at 999. Listing is repository-scoped and bounded to at most 100 rows. Approval is bound to the current report and preview digests and expires at the earlier of its approval window or the outbox row's retention expiry. A changed report, occurrence count, or preview makes an old digest stale.

The 64-character fingerprint is an HMAC under the repository-specific key over the closed incident classification plus version and commit.
The outbox deduplicates by target repository and fingerprint. The public issue contains a separate keyed marker; publishing searches that repository for the marker. If a matching issue exists, Fleet posts a bounded occurrence-summary comment instead of creating another issue. New occurrences update the count and require a new preview and, in `approval_required` mode, a new approval.

## Operator procedure

Export the repository key named by `repository_key_env`. It must be at least 32 bytes. Review `fleet inspect RUN_ID` and the private outbox before publishing. From the target repository, use this exact approval-required sequence:

```text
fleet incidents list
fleet incidents preview FINGERPRINT
fleet incidents approve FINGERPRINT --preview-digest DIGEST
fleet incidents publish RUN_ID FINGERPRINT --preview-digest DIGEST
```

`list` emits only validated outbox metadata. `preview` is the review boundary: inspect its exact title, body, labels, marker, report digest, and preview digest. Pass that exact preview digest to `approve`, then pass it again to `publish`.
In `auto` mode the allowlists supply the bounded authorization, so `approve` is not allowed; preview and explicit `publish` remain the operator procedure. A repeated publish of an already-published fingerprint returns its stored receipt without another transport call.

Every incident-command failure writes only a stable uppercase error code to stderr and exits with status `2`; success exits `0`. Do not publish after an unexpected preview or a failed inspection.
`fleet inspect RUN_ID` exposes at most 20 sanitized incident records containing only state, closed incident/error codes, fingerprints and digests, expiry, issue number and public URL, authorization mode/digest, and authorization/publication timestamps. It excludes record IDs, run and policy bindings, terminal-closure details, bodies, diagnosis, messages, paths, environment data, keys, and tokens.

## Credentials and rotation

Use two separate environment values:

- The variable named by `repository_key_env` (for example `FLEET_INCIDENT_KEY`) is a stable, random, repository-specific key of at least 32 bytes. Fleet reads it for preparation, preview, and a not-yet-completed publish. It keys fingerprints and public dedupe markers.
- `FLEET_GITHUB_ISSUES_TOKEN` is used only for GitHub transport. It should be a fine-grained token scoped to the single `target_repository` with only **Issues: write** repository permission. GitHub documents `POST /repos/{owner}/{repo}/issues` as requiring Issues repository permission (write): <https://docs.github.com/en/rest/issues/issues#create-an-issue>.

The token is read only when an authorized publish reaches the actual transport boundary. It is never placed in the Harness, CLI arguments, outbox/public report, Inspector, stdout, or stderr. Fleet does not provision tokens. Create, expire, revoke, and rotate the token with GitHub, then replace the environment value in the operator process.

Treat repository-key rotation as a deduplication identity change: drain or let pending entries expire, rotate the external secret, and re-preview and re-approve any retained entry before use. A new key changes fingerprints for newly prepared incidents and changes public issue markers, so an uncoordinated rotation can prevent matching an earlier issue. Never reuse one repository key across targets.

## Failure behavior and security boundary

The pipeline fails closed on missing or invalid configuration, keys, sanitization, schema, closure evidence, allowlist authorization, outbox permissions, caps, stale or expired approval, public rendering, transport responses, or persistence.
It does not retry GitHub requests, follow redirects, switch repositories or credentials, or fall back to private diagnosis, raw evidence, free-form model text, or a broader public body. A reporting failure cannot broaden output or change the Graph run's authoritative failure.

This channel is public operational telemetry only. Suspected vulnerabilities, exploit details, security-sensitive diagnostics, secrets, and sensitive user data must never use incident publishing.
Follow [the private vulnerability-reporting policy](../SECURITY.md) instead.
