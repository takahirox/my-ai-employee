# Migrating legacy Fleet databases

Operational Fleet history now has one canonical location: `~/.fleet/fleet.db`.
Repository-local policy remains in `.fleet/project.yaml`, but repository identity and run
history belong in the canonical catalog so the Inspector can show Active and History views
across every repository.

## Safe import

First stop all Fleet processes using the source. A remaining `-wal` or `-shm` file is treated
as evidence that the source may still be active, and import fails without opening the
destination.

```bash
fleet import-legacy-db /absolute/path/to/repository/.fleet/fleet.db
```

The importer:

- opens the source with SQLite read-only and query-only enforcement;
- verifies SQLite integrity, foreign keys, the Fleet schema version, every known table, and
  every known column before touching the destination;
- refuses symlinks, an active WAL source, unknown schema objects, and newer schemas;
- creates a private SQLite backup beside an existing destination before importing;
- merges rows in one immediate transaction, accepting exact duplicates but rejecting any
  primary-key or uniqueness collision with different content;
- copies repository registrations and parent/child run relationships with the records they
  describe;
- verifies that the source file did not change, checks destination foreign keys, commits an
  idempotency journal, restores mode `0600`, and verifies WAL mode;
- prints JSON containing the source digest, backup path, row counts, idempotency state, and
  verification result.

Running the command again with the exact same source content is safe: the journal identifies
the SHA-256 digest and reports `already_imported: true` without importing or backing up again.
A changed source is a new import and receives full validation.

## Failures and rollback

Unknown schemas, broken relationships, and collisions fail before commit. The source is never
modified. A backup created before a failed transaction is intentionally retained so an
operator always has a recovery point.

If a successful import must be rolled back, stop Fleet, retain the current database for audit,
and replace it with the reported backup using normal filesystem administration. Ensure the
restored file remains private (`0600`) before restarting Fleet. Do not combine databases by
copying SQLite files while either database is open.

After import, verify representative data through the canonical Inspector:

```bash
fleet serve
fleet inspect RUN_ID
```

Legacy runs without repository registration remain visible under `Legacy / unassigned
repository`. Graph-owned child work remains reachable through its parent task drill-down;
historical standalone work rows retain their existing audit-only behavior.
