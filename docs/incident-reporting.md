# Incident reporting configuration

Project Harness v2 incident reporting is off by default. The configuration records
repository intent; it does not itself authorize publishing:

~~~json
{
  "incident_reporting": {
    "mode": "approval_required",
    "target_repository": "owner/public-repository",
    "repository_key_env": "FLEET_INCIDENT_KEY",
    "outbox_path": "~/.fleet/incident-reporting/outbox.sqlite3",
    "retention_hours": 168,
    "approval_hours": 24,
    "daily_limit": 3,
    "pending_cap": 20
  }
}
~~~

"mode" is "off", "approval_required", or "auto". Enabled modes require one public
GitHub "owner/repository" target and the name of an environment variable containing
the repository-specific key. The key itself and GitHub tokens are never Harness
fields.

"auto" also requires explicit, non-empty "auto_categories" and "auto_failures"
allowlists. Only the closed values accepted by the Harness schema are valid; wildcard
and unknown entries are rejected. Provisional Harnesses cannot enable reporting.

The outbox is private local storage and defaults below "~/.fleet". Raw evidence,
diagnostic detail, logs, prompts, transcripts, and diffs never leave local storage.
Only the separately defined public incident schema may be composed for a report.

This configuration grants no network fallback. Publishing remains subject to separate
effective-policy authorization (and approval in "approval_required" mode), plus the
configured retention, approval-window, daily, and pending limits.
