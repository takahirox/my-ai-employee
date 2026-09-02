"""Adversarial tests for the opt-in incident reporting boundary."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_employee.incident_reporting import (
    Category,
    Diagnosis,
    Disposition,
    Failure,
    FakeTransport,
    IncidentError,
    Mode,
    Outbox,
    Policy,
    Report,
    Stage,
    TerminalState,
    compose,
    public_json,
    qualifies_for_reporting,
    render_public_issue,
)

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
KEY = b"k" * 32
OTHER_KEY = b"q" * 32
COMMIT = "a" * 40
REPOSITORY = "owner/repository"
CANARY = "incident-canary-62-do-not-publish"


def diagnosis(**changes: object) -> Diagnosis:
    values: dict[str, object] = {
        "category": Category.KERNEL,
        "terminal_state": TerminalState.FAILED,
        "disposition": Disposition.INTERNAL_PRODUCT_FAILURE,
        "failure": Failure.RUNTIME,
        "stage": Stage.RUNTIME,
        "private_detail": CANARY,
    }
    values.update(changes)
    return Diagnosis.model_validate(values)


def report(**changes: object) -> Report:
    result = compose(diagnosis(), KEY, "1.2.3", COMMIT, 1, 64)
    return result.model_copy(update=changes)


def approval_policy(**changes: object) -> Policy:
    values: dict[str, object] = {
        "mode": Mode.APPROVAL_REQUIRED,
        "repository": REPOSITORY,
    }
    values.update(changes)
    return Policy.model_validate(values)


def auto_policy(**changes: object) -> Policy:
    values: dict[str, object] = {
        "mode": Mode.AUTO,
        "repository": REPOSITORY,
        "auto_categories": (Category.KERNEL,),
    }
    values.update(changes)
    return Policy.model_validate(values)


def outbox_path(tmp_path: Path, name: str = "outbox.sqlite3") -> Path:
    return tmp_path / "private" / name


def test_private_and_public_models_are_strict_frozen_and_extra_forbid() -> None:
    instances = (diagnosis(), report(), Policy())
    for instance in instances:
        payload = instance.model_dump()
        payload["unknown"] = "denied"
        with pytest.raises(ValidationError):
            type(instance).model_validate(payload)
        field = next(iter(type(instance).model_fields))
        with pytest.raises(ValidationError):
            setattr(instance, field, getattr(instance, field))

    raw = diagnosis().model_dump(mode="json")
    with pytest.raises(ValidationError):
        Diagnosis.model_validate(raw)


@pytest.mark.parametrize(
    "terminal_state",
    [state for state in TerminalState if state is not TerminalState.FAILED],
)
def test_only_failed_terminal_state_qualifies(terminal_state: TerminalState) -> None:
    candidate = diagnosis(terminal_state=terminal_state)
    assert not qualifies_for_reporting(candidate)
    with pytest.raises(IncidentError, match=r"^NOT_TERMINAL_INTERNAL$"):
        compose(candidate, KEY, "1.2.3", COMMIT, 1, 64)


@pytest.mark.parametrize(
    "disposition",
    [
        disposition
        for disposition in Disposition
        if disposition is not Disposition.INTERNAL_PRODUCT_FAILURE
    ],
)
def test_only_internal_product_failure_qualifies(disposition: Disposition) -> None:
    candidate = diagnosis(disposition=disposition)
    assert not qualifies_for_reporting(candidate)
    with pytest.raises(IncidentError, match=r"^NOT_TERMINAL_INTERNAL$"):
        compose(candidate, KEY, "1.2.3", COMMIT, 1, 64)


def test_internal_failed_event_qualifies_and_fingerprint_is_repository_keyed() -> None:
    candidate = diagnosis()
    assert qualifies_for_reporting(candidate)
    first = compose(candidate, KEY, "1.2.3", COMMIT, 1, 64)
    assert first == compose(candidate, KEY, "1.2.3", COMMIT, 1, 64)
    assert first.fingerprint != compose(candidate, OTHER_KEY, "1.2.3", COMMIT, 1, 64).fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.fingerprint)


@pytest.mark.parametrize(
    ("metric", "boundaries"),
    (
        ("duration", (0, 1, 5, 15, 30, 60, 300, 900, 3_600)),
        ("memory", (0, 64, 128, 256, 512, 1_024, 2_048, 4_096, 8_192)),
    ),
)
def test_every_bucket_boundary_and_ceiling(metric: str, boundaries: tuple[int, ...]) -> None:
    for index, boundary in enumerate(boundaries):
        arguments = {"duration": 0, "memory": 0, metric: boundary}
        actual = compose(diagnosis(), KEY, "1.2.3", COMMIT, **arguments)
        assert getattr(actual, f"{metric}_bucket") == boundary
        if index + 1 < len(boundaries):
            arguments[metric] = boundary + 0.001
            actual = compose(diagnosis(), KEY, "1.2.3", COMMIT, **arguments)
            assert getattr(actual, f"{metric}_bucket") == boundaries[index + 1]

    arguments = {"duration": 0, "memory": 0, metric: boundaries[-1] + 1}
    actual = compose(diagnosis(), KEY, "1.2.3", COMMIT, **arguments)
    assert getattr(actual, f"{metric}_bucket") == boundaries[-1]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.001, True, "1"])
@pytest.mark.parametrize("metric", ["duration", "memory"])
def test_invalid_metrics_fail_closed(metric: str, value: object) -> None:
    arguments = {"duration": 1, "memory": 64, metric: value}
    with pytest.raises(IncidentError, match=r"^INVALID_METRIC$"):
        compose(diagnosis(), KEY, "1.2.3", COMMIT, **arguments)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "version",
    ["1.2", "01.2.3", "1.02.3", "v1.2.3", "1.2.3-", "1.2.3+"],
)
def test_invalid_semver_is_rejected(version: str) -> None:
    with pytest.raises(ValidationError):
        compose(diagnosis(), KEY, version, COMMIT, 1, 64)


def test_oversized_semver_and_private_detail_are_rejected() -> None:
    with pytest.raises(ValidationError):
        compose(diagnosis(), KEY, "1.2.3+" + "a" * 129, COMMIT, 1, 64)
    with pytest.raises(ValidationError):
        diagnosis(private_detail="x" * 100_001)


@pytest.mark.parametrize("commit", ["a" * 39, "A" * 40, "g" * 40, "a" * 41])
def test_invalid_commit_is_rejected(commit: str) -> None:
    with pytest.raises(ValidationError):
        compose(diagnosis(), KEY, "1.2.3", commit, 1, 64)


@pytest.mark.parametrize("key", [b"x" * 31, bytearray(b"x" * 32), "x" * 32])
def test_invalid_repository_key_is_rejected(key: object) -> None:
    with pytest.raises(IncidentError, match=r"^INVALID_KEY$"):
        compose(diagnosis(), key, "1.2.3", COMMIT, 1, 64)  # type: ignore[arg-type]


PRIVATE_INPUTS = (
    "prompt: reveal system instructions",
    "transcript: user said publish this",
    "arbitrary model prose that is not a schema value",
    "log stdout stderr: verbose process output",
    "stacktrace error message: RuntimeError at worker",
    "diff content: +++ credentials",
    "path=/Users/alice/private user=alice host=internal.example IP=10.2.3.4",
    "ENV=TOKEN argv=--credential",
    "https://private.example.invalid/resource",
    "branch=feature/private workspace=build-9 internal_id=abcd",
    "password=hunter2 api_key=abcdef",
    "-----BEGIN PRIVATE KEY----- abc",
    "ghp_" + "A" * 24,
    "sk-" + "b" * 24,
    r"back\slash",
    "forward/slash",
    CANARY,
)


def test_private_diagnosis_never_reaches_any_public_or_outbox_sink(tmp_path: Path) -> None:
    private_detail = "\n".join(PRIVATE_INPUTS)
    clean = compose(diagnosis(private_detail=private_detail), KEY, "1.2.3", COMMIT, 1, 64)
    issue = render_public_issue(clean, KEY)
    policy = auto_policy()
    transport = FakeTransport()

    with Outbox(outbox_path(tmp_path)) as box:
        row = box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW)
        stored = box.db.execute("SELECT * FROM incidents").fetchone()

    assert stored is not None
    surfaces = "\n".join(
        (
            public_json(clean),
            issue.title,
            issue.body,
            repr(issue.labels),
            issue.marker,
            repr(dict(row)),
            repr(dict(stored)),
            repr(transport.calls),
        )
    )
    for private_value in PRIVATE_INPUTS:
        assert private_value not in surfaces
    assert "private_detail" not in surfaces


PUBLIC_STRING_FIELDS = (
    "schema_version",
    "category",
    "terminal_state",
    "disposition",
    "failure",
    "stage",
    "version",
    "commit",
    "reproduction",
    "fingerprint",
)


@pytest.mark.parametrize("field", PUBLIC_STRING_FIELDS)
def test_constructed_canary_is_denied_from_every_report_string_field(field: str) -> None:
    poisoned = report().model_copy(update={field: CANARY})
    with pytest.raises((IncidentError, ValidationError)):
        public_json(poisoned)
    with pytest.raises((IncidentError, ValidationError)):
        render_public_issue(poisoned, KEY)


@pytest.mark.parametrize("payload", PRIVATE_INPUTS)
def test_all_private_input_classes_fail_closed_when_injected_as_public_prose(
    payload: str,
) -> None:
    poisoned = report().model_copy(update={"version": payload})
    with pytest.raises((IncidentError, ValidationError)):
        public_json(poisoned)
    with pytest.raises((IncidentError, ValidationError)):
        render_public_issue(poisoned, KEY)


def test_constructed_unknown_field_and_outbox_injection_are_denied(tmp_path: Path) -> None:
    poisoned = Report.model_construct(**report().model_dump())
    poisoned.__dict__["private_detail"] = CANARY
    with pytest.raises(IncidentError, match=r"^PUBLIC_FIELD_DENIED$"):
        public_json(poisoned)
    with Outbox(outbox_path(tmp_path)) as box:
        with pytest.raises(IncidentError):
            box.enqueue(poisoned, approval_policy(), NOW)
        assert box.db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0


def test_constructed_private_repository_is_denied_before_persistence(tmp_path: Path) -> None:
    policy = Policy.model_construct(
        mode=Mode.AUTO,
        repository=f"owner/{CANARY}",
        auto_categories=(Category.KERNEL,),
        retention_hours=168,
        approval_hours=24,
        daily_limit=3,
        pending_cap=20,
    )
    with Outbox(outbox_path(tmp_path)) as box:
        with pytest.raises(IncidentError):
            box.enqueue(report(), policy, NOW)
        assert box.db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0


def test_rendering_is_deterministic_and_labels_are_closed() -> None:
    first = render_public_issue(report(), KEY)
    second = render_public_issue(report(), KEY)
    assert first == second
    assert first.labels == ("ai-employee-incident", "incident:trust_kernel_failure")
    assert first.title == "[incident] trust_kernel_failure: runtime_error at runtime"
    assert re.fullmatch(r"<!-- ai-employee-incident:[0-9a-f]{64} -->", first.marker)
    assert first.body.endswith(first.marker)
    assert json.loads(public_json(report())) == report().model_dump(mode="json")


def test_outbox_creates_private_parent_and_database_and_rejects_symlinks(
    tmp_path: Path,
) -> None:
    database = outbox_path(tmp_path)
    with Outbox(database):
        assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600

    broad = tmp_path / "broad"
    broad.mkdir(mode=0o755)
    os.chmod(broad, 0o755)
    with pytest.raises(IncidentError, match=r"^PRIVATE_PARENT_REQUIRED$"):
        Outbox(broad / "outbox.sqlite3")

    private = tmp_path / "symlinks"
    private.mkdir(mode=0o700)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    parent_link = private / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(IncidentError, match=r"^PRIVATE_PARENT_REQUIRED$"):
        Outbox(parent_link / "outbox.sqlite3")

    target = private / "target.sqlite3"
    with Outbox(target):
        pass
    database_link = private / "database-link.sqlite3"
    database_link.symlink_to(target)
    with pytest.raises(IncidentError, match=r"^PRIVATE_DATABASE_REQUIRED$"):
        Outbox(database_link)


def test_mode_off_auto_allowlist_pending_cap_and_occurrence_cap(tmp_path: Path) -> None:
    first = report()
    second = report(fingerprint="b" * 64)
    with Outbox(outbox_path(tmp_path)) as box:
        with pytest.raises(IncidentError, match=r"^POLICY_OFF$"):
            box.enqueue(first, Policy(), NOW)
        with pytest.raises(IncidentError, match=r"^AUTO_CATEGORY_DENIED$"):
            box.enqueue(
                first,
                auto_policy(auto_categories=(Category.STORAGE,)),
                NOW,
            )

        capped = approval_policy(pending_cap=1)
        box.enqueue(first, capped, NOW)
        with pytest.raises(IncidentError, match=r"^OUTBOX_CAP$"):
            box.enqueue(second, capped, NOW)

        row = box.enqueue(first.model_copy(update={"occurrences": 998}), capped, NOW)
        assert row["occurrence_count"] == 999
        row = box.enqueue(first, capped, NOW)
        assert row["occurrence_count"] == 999
        assert json.loads(row["report_json"])["occurrences"] == 999


def test_retention_purge_preview_digest_and_stale_preview(tmp_path: Path) -> None:
    policy = approval_policy(retention_hours=1)
    clean = report()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        first = box.preview(clean.fingerprint, policy, KEY, NOW)
        second = box.preview(clean.fingerprint, policy, KEY, NOW)
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{64}", first.digest)
        assert re.fullmatch(r"[0-9a-f]{64}", first.report_digest)

        box.enqueue(clean, policy, NOW + timedelta(minutes=1))
        with pytest.raises(IncidentError, match=r"^STALE_PREVIEW$"):
            box.approve(clean.fingerprint, first.digest, policy, NOW + timedelta(minutes=1))

        assert box.purge(NOW + timedelta(hours=1, minutes=1)) == 1
        assert box.db.execute("SELECT count(*) FROM incidents").fetchone()[0] == 0


def test_approval_digest_mismatch_and_expiry_fail_closed(tmp_path: Path) -> None:
    policy = approval_policy(approval_hours=1)
    clean = report()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        with pytest.raises(IncidentError, match=r"^STALE_PREVIEW$"):
            box.approve(clean.fingerprint, "0" * 64, policy, NOW)

        approval = box.approve(clean.fingerprint, preview.digest, policy, NOW)
        assert re.fullmatch(r"[0-9a-f]{64}", approval)
        box.db.execute(
            "UPDATE incidents SET approval_digest=? WHERE fingerprint=?",
            ("f" * 64, clean.fingerprint),
        )
        box.db.commit()
        with pytest.raises(IncidentError, match=r"^APPROVAL_REQUIRED$"):
            box.publish(clean.fingerprint, preview.digest, policy, KEY, FakeTransport(), NOW)

        assert box.approve(clean.fingerprint, preview.digest, policy, NOW) == approval
        with pytest.raises(IncidentError, match=r"^APPROVAL_EXPIRED$"):
            box.publish(
                clean.fingerprint,
                preview.digest,
                policy,
                KEY,
                FakeTransport(),
                NOW + timedelta(hours=1),
            )


EXPECTED_COLUMNS = set(
    [
        "repository",
        "fingerprint",
        "report_json",
        "report_digest",
        "status",
        "occurrence_count",
        "created_at",
        "updated_at",
        "expires_at",
        "preview_digest",
        "preview_report_digest",
        "approval_digest",
        "approval_expires_at",
        "issue_number",
        "public_url",
        "public_report_digest",
        "authorization_mode",
        "authorization_digest",
        "authorized_at",
        "published_at",
    ]
)


def test_publish_create_stores_only_public_report_and_receipt_evidence(
    tmp_path: Path,
) -> None:
    clean = compose(
        diagnosis(private_detail="credentials at /private/path " + CANARY),
        KEY,
        "1.2.3",
        COMMIT,
        1,
        64,
    )
    policy = auto_policy()
    transport = FakeTransport()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        receipt = box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW)
        row = box.db.execute("SELECT * FROM incidents").fetchone()
        log_count = box.db.execute("SELECT count(*) FROM publication_log").fetchone()[0]

    assert receipt == (1, f"https://github.com/{REPOSITORY}/issues/1")
    assert row is not None
    assert set(row.keys()) == EXPECTED_COLUMNS
    assert row["report_json"] == public_json(clean)
    assert row["issue_number"] == receipt[0]
    assert row["public_url"] == receipt[1]
    assert row["authorization_mode"] == Mode.AUTO.value
    for column in (
        "report_digest",
        "preview_digest",
        "preview_report_digest",
        "public_report_digest",
        "authorization_digest",
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", row[column])
    assert log_count == 1
    serialized = repr(dict(row))
    assert CANARY not in serialized
    assert "private_detail" not in serialized
    assert [call[0] for call in transport.calls] == [
        "find_issue_by_marker",
        "create_issue",
    ]


def test_existing_marker_updates_occurrences_instead_of_creating(tmp_path: Path) -> None:
    clean = report()
    policy = auto_policy()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        transport = FakeTransport(
            existing={
                (REPOSITORY, preview.issue.marker): (
                    7,
                    f"https://github.com/{REPOSITORY}/issues/7",
                )
            }
        )
        assert box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW) == (
            7,
            f"https://github.com/{REPOSITORY}/issues/7",
        )

    assert [call[0] for call in transport.calls] == [
        "find_issue_by_marker",
        "update_occurrence_summary",
    ]
    assert transport.calls[-1][-1].startswith("Occurrences: 2 of 999;")


@pytest.mark.parametrize(
    ("operation", "existing"),
    (
        ("find_issue_by_marker", False),
        ("create_issue", False),
        ("update_occurrence_summary", True),
    ),
)
def test_transport_failure_rolls_back_and_retry_records_one_receipt(
    tmp_path: Path, operation: str, existing: bool
) -> None:
    clean = report()
    policy = auto_policy()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        known = (
            {
                (REPOSITORY, preview.issue.marker): (
                    8,
                    f"https://github.com/{REPOSITORY}/issues/8",
                )
            }
            if existing
            else None
        )
        transport = FakeTransport(failures={operation: 1}, existing=known)
        with pytest.raises(IncidentError, match=r"^TRANSPORT_FAILURE$"):
            box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW)
        failed_row = box.db.execute("SELECT * FROM incidents").fetchone()
        assert failed_row["status"] == "pending"
        assert failed_row["issue_number"] is None
        assert box.db.execute("SELECT count(*) FROM publication_log").fetchone()[0] == 0

        receipt = box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW)
        assert box.db.execute("SELECT count(*) FROM publication_log").fetchone()[0] == 1
        assert box.db.execute("SELECT issue_number FROM incidents").fetchone()[0] == receipt[0]

    assert sum(call[0] == operation for call in transport.calls) == 2
    if operation == "create_issue":
        assert transport.next_issue == 2


def test_private_transport_receipt_is_rejected_and_rolled_back(tmp_path: Path) -> None:
    clean = report()
    policy = auto_policy()
    with Outbox(outbox_path(tmp_path)) as box:
        box.enqueue(clean, policy, NOW)
        preview = box.preview(clean.fingerprint, policy, KEY, NOW)
        transport = FakeTransport(
            existing={
                (REPOSITORY, preview.issue.marker): (
                    4,
                    f"https://private.example.invalid/{CANARY}",
                )
            }
        )
        with pytest.raises(IncidentError, match=r"^INVALID_TRANSPORT_RECEIPT$"):
            box.publish(clean.fingerprint, preview.digest, policy, KEY, transport, NOW)
        row = box.db.execute("SELECT status,public_url FROM incidents").fetchone()
        assert tuple(row) == ("pending", None)


def test_daily_publication_limit_is_repository_scoped(tmp_path: Path) -> None:
    policy = auto_policy(daily_limit=1)
    first = report()
    second = compose(diagnosis(failure=Failure.OS), KEY, "1.2.3", COMMIT, 1, 64)
    with Outbox(outbox_path(tmp_path)) as box:
        for candidate in (first, second):
            box.enqueue(candidate, policy, NOW)
        first_preview = box.preview(first.fingerprint, policy, KEY, NOW)
        second_preview = box.preview(second.fingerprint, policy, KEY, NOW)
        box.publish(first.fingerprint, first_preview.digest, policy, KEY, FakeTransport(), NOW)
        with pytest.raises(IncidentError, match=r"^DAILY_LIMIT$"):
            box.publish(
                second.fingerprint,
                second_preview.digest,
                policy,
                KEY,
                FakeTransport(),
                NOW,
            )
        assert box.db.execute("SELECT count(*) FROM publication_log").fetchone()[0] == 1
