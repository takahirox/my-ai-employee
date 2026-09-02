"""Security regression tests for sanitized incident outbox views."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_employee.incident_reporting import (
    Category,
    Disposition,
    Failure,
    IncidentError,
    Mode,
    Outbox,
    OutboxEntry,
    Policy,
    PublicationReceipt,
    PublicExceptionClass,
    Report,
    Stage,
    TerminalState,
)
from ai_employee.serialization import canonical_digest

NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)
CANARY = "CANARY-private-message-/tmp/key-ghp_0123456789abcdefghijkl"
ENTRY_FIELDS = {
    "repository",
    "fingerprint",
    "status",
    "occurrence_count",
    "created_at",
    "updated_at",
    "expires_at",
    "report_digest",
    "preview_digest",
    "approval_digest",
    "approval_expires_at",
    "issue_number",
    "public_url",
    "public_report_digest",
    "authorization_mode",
    "authorization_digest",
    "authorized_at",
    "published_at",
}
RECEIPT_FIELDS = {
    "repository",
    "fingerprint",
    "issue_number",
    "public_url",
    "public_report_digest",
    "authorization_mode",
    "authorization_digest",
    "authorized_at",
    "published_at",
}


def _policy(pending_cap: int = 5) -> Policy:
    return Policy(
        mode=Mode.AUTO,
        repository="owner/repository",
        auto_categories=(Category.KERNEL,),
        pending_cap=pending_cap,
    )


def _report(index: int) -> Report:
    return Report(
        schema_version="1",
        category=Category.KERNEL,
        terminal_state=TerminalState.FAILED,
        disposition=Disposition.INTERNAL_PRODUCT_FAILURE,
        failure=Failure.RUNTIME,
        exception_class=PublicExceptionClass.RUNTIME_ERROR,
        stage=Stage.RUNTIME,
        version="1.0.0",
        commit="1" * 40,
        duration_bucket=1,
        memory_bucket=64,
        reproduction="synthetic_reproduction_v1",
        fingerprint=f"{index + 1:064x}",
        occurrences=1,
    )


def _publish(box: Outbox, report: Report, policy: Policy, now: datetime, number: int) -> str:
    repository = policy.repository
    assert repository is not None
    row = box.enqueue(report, policy, now)
    report_digest = row["report_digest"]
    preview_digest = "a" * 64
    authorization_digest = canonical_digest(
        {
            "mode": policy.mode.value,
            "repository": repository,
            "report_digest": report_digest,
            "preview_digest": preview_digest,
        }
    )
    stamp = now.isoformat(timespec="seconds")
    with box.db:
        box.db.execute(
            "UPDATE incidents SET status='published',preview_digest=?,"
            "preview_report_digest=?,issue_number=?,public_url=?,public_report_digest=?,"
            "authorization_mode=?,authorization_digest=?,authorized_at=?,published_at=?,"
            "updated_at=? WHERE repository=? AND fingerprint=?",
            (
                preview_digest,
                report_digest,
                number,
                f"https://github.com/{repository}/issues/{number}",
                report_digest,
                policy.mode.value,
                authorization_digest,
                stamp,
                stamp,
                stamp,
                repository,
                report.fingerprint,
            ),
        )
    return authorization_digest


def test_views_are_exact_immutable_and_do_not_leak_unknown_surface(tmp_path) -> None:
    policy = _policy()
    report = _report(0)
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        authorization_digest = _publish(box, report, policy, NOW, 7)
        forbidden = {
            name: CANARY
            for name in (
                "report_json",
                "title",
                "body",
                "labels",
                "marker",
                "token",
                "key",
                "message",
                "path",
                "run_id",
                "node_id",
                "internal_id",
            )
        }
        with box.db:
            box.db.execute("ALTER TABLE incidents ADD COLUMN forbidden_surface TEXT")
            box.db.execute("UPDATE incidents SET forbidden_surface=?", (json.dumps(forbidden),))

        entry = box.list_entries(policy, NOW)[0]
        receipt = box.publication_receipt(report.fingerprint, policy, NOW)
        entry_dump = entry.model_dump(mode="json")
        receipt_dump = receipt.model_dump(mode="json")
        surface = json.dumps((entry_dump, receipt_dump), sort_keys=True)

        assert set(entry_dump) == ENTRY_FIELDS
        assert set(receipt_dump) == RECEIPT_FIELDS
        assert CANARY not in surface
        assert "report_json" not in surface
        assert "body" not in surface
        assert receipt.authorization_digest == authorization_digest
        assert isinstance(receipt, PublicationReceipt)

        with pytest.raises(ValidationError) as invalid_model:
            OutboxEntry.model_validate(entry_dump | {"body": CANARY})
        assert CANARY not in str(invalid_model.value)
        with pytest.raises(ValidationError):
            receipt.public_url = CANARY  # type: ignore[misc]

        with box.db:
            box.db.execute(
                "UPDATE incidents SET report_json=? WHERE fingerprint=?",
                (CANARY, report.fingerprint),
            )
        with pytest.raises(IncidentError) as invalid_row:
            box.list_entries(policy, NOW)
        assert CANARY not in str(invalid_row.value)
        assert "report_json" not in str(invalid_row.value)


@pytest.mark.parametrize(
    ("statement", "bad_value"),
    [
        ("UPDATE incidents SET report_digest=?", "not-a-digest"),
        ("UPDATE incidents SET created_at=?", "not-a-time"),
        ("UPDATE incidents SET public_url=?", CANARY),
        ("UPDATE incidents SET authorization_digest=?", "0" * 64),
        ("UPDATE incidents SET published_at=?", "2020-01-01T00:00:00+00:00"),
        ("UPDATE incidents SET issue_number=?", 0),
    ],
)
def test_malformed_rows_and_receipts_fail_closed(
    tmp_path, statement: str, bad_value: object
) -> None:
    policy = _policy()
    report = _report(0)
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        _publish(box, report, policy, NOW, 7)
        with box.db:
            box.db.execute(statement, (bad_value,))
        with pytest.raises(IncidentError) as error:
            box.publication_receipt(report.fingerprint, policy, NOW)
        assert str(bad_value) not in str(error.value)


def test_database_rejects_unknown_status_before_it_can_reach_a_view(tmp_path) -> None:
    policy = _policy()
    report = _report(0)
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        _publish(box, report, policy, NOW, 7)
        with pytest.raises(sqlite3.IntegrityError), box.db:
            box.db.execute("UPDATE incidents SET status=?", ("unknown",))


def test_listing_purges_orders_bounds_and_is_repository_scoped(tmp_path) -> None:
    policy = _policy()
    reports = [_report(index) for index in range(30)]
    with Outbox(tmp_path / "outbox.sqlite3") as box:
        for index, report in enumerate(reports):
            occurred_at = NOW - timedelta(minutes=10) + timedelta(seconds=index)
            _publish(box, report, policy, occurred_at, index + 1)
        with box.db:
            box.db.execute(
                "UPDATE incidents SET expires_at=? WHERE fingerprint=?",
                (NOW.isoformat(timespec="seconds"), reports[0].fingerprint),
            )

        entries = box.list_entries(policy, NOW)
        assert len(entries) == 25
        assert [entry.fingerprint for entry in entries] == [
            report.fingerprint for report in reversed(reports[5:])
        ]
        assert (
            box.list_entries(policy.model_copy(update={"repository": "other/repository"}), NOW)
            == []
        )
        with pytest.raises(IncidentError, match="PUBLICATION_NOT_FOUND"):
            box.publication_receipt(
                reports[1].fingerprint,
                policy.model_copy(update={"repository": "other/repository"}),
                NOW,
            )
