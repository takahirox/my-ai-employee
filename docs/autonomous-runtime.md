# Autonomous runtime implementation (#167)

The implementation under `ai_employee.autonomous` is in development. It is not
yet the product's default execution path, and this document does not claim that
Issue #167's acceptance criteria are complete.

The new contracts separate immutable Goal/Task definitions from runtime-owned
attempt identities, authority versions and content-addressed Candidates. Workers
edit real workspaces. Verification runs in separate copies, and successful file
results can be published to a new directory without replaying worker edit actions.

The runtime currently supports clarification, one-step planning, parallel ready
tasks, explicit integration inputs, bounded task retries, forward graph extensions
after task/goal verification failure, persisted worker selection, and a Run-wide
reservation ledger. The journal records configuration, Goal, plans, attempts,
lineage, verification and authority request/approval/application events. Inspection
and completed-run replay do not call models. Unknown external effects and usage
limits prevent automatic continuation.

Initial repository input contains tracked files only. Candidate capture rejects
symlinks and special files and checks file identity while copying. Runtime metadata
and integration input directories are excluded from published content. Mandatory
check definitions come from the snapshotted operator configuration, not worker
files. Native preflight failures never fall back to unrestricted execution.

The native adapter uses Codex permission profiles with minimal filesystem reads;
it does not grant host-wide read access. Claude uses restricted mode, mandatory
sandboxing and disabled unsandboxed fallback. Native command verification currently
uses the installed Codex sandbox command for either worker backend. Configuration
references: [Codex permissions](https://learn.chatgpt.com/docs/permissions) and
[Claude sandboxing](https://code.claude.com/docs/en/sandboxing).

## Development verification

Focused offline regressions use scripted model responses and real disposable
workspaces/journals. These tests exercise the runtime's normal execution path, not
the old typed proposal transport. They do not establish live-model quality or
native sandbox enforcement. Run repository checks as documented in
[development.md](development.md).

## Work remaining before switching the product default

- Review and harden native backend enforcement, process cleanup, authority
  application, concurrent interruption and crash recovery.
- Extend regression coverage for multiple integrations, stale downstream results,
  approval/revocation, verification evidence, and failure/recovery boundaries.
- Complete operator CLI and Inspector integration, benchmark connections, and
  configurable supervision/escalation behavior.
- Run required checks and credential-free native isolation tests in supported
  environments; keep live-model tests explicitly opt-in.
- Switch the supported product entry points and remove obsolete proposal,
  re-execution and compatibility paths, plus contradictory docs/tests, only after
  the new path is accepted. Do not add a legacy migration subsystem.
