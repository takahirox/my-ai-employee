from __future__ import annotations

from ai_employee.worker_adapters import (
    _normalize_existing_file_hunk_counts,
    _normalize_unified_diff,
)


def test_recounts_every_hunk_and_preserves_header_spelling_and_endings() -> None:
    patch = (
        "diff --git a/example.py b/example.py\r\n"
        "--- a/example.py\r\n"
        "+++ b/example.py\r\n"
        "@@ -4 +8,9 @@ first suffix\r\n"
        "-old\r\n"
        "+new\r\n"
        "@@ -10,0 +12 @@ second suffix\r\n"
        "+added\r\n"
    )
    expected = patch.replace(
        "@@ -4 +8,9 @@ first suffix\r\n",
        "@@ -4 +8,1 @@ first suffix\r\n",
    )

    normalized = _normalize_existing_file_hunk_counts(patch)

    assert normalized == expected
    assert _normalize_existing_file_hunk_counts(normalized) == normalized


def test_malformed_raw_hunk_boundary_discards_all_section_recounts() -> None:
    patch = (
        "diff --git a/example.txt b/example.txt\n"
        "--- a/example.txt\n"
        "+++ b/example.txt\n"
        "@@ -1,9 +1,9 @@\n"
        "-old\n"
        "+new\n"
        "@@ malformed\n"
        " context\n"
    )

    assert _normalize_existing_file_hunk_counts(patch) == patch


def test_unprefixed_or_empty_hunk_body_discards_all_section_recounts() -> None:
    prefix = (
        "diff --git a/example.txt b/example.txt\n"
        "--- a/example.txt\n"
        "+++ b/example.txt\n"
        "@@ -1,9 +1,9 @@\n"
        "-old\n"
        "+new\n"
    )

    for body in ("@@ -3,2 +3,2 @@\nunprefixed\n", "@@ -3 +3 @@\n\n"):
        patch = prefix + body
        assert _normalize_existing_file_hunk_counts(patch) == patch


def test_commits_valid_sections_independently_after_full_validation() -> None:
    valid = (
        "diff --git a/valid.txt b/valid.txt\n"
        "--- a/valid.txt\n"
        "+++ b/valid.txt\n"
        "@@ -1,8 +1,8 @@\n"
        "-old\n"
        "+new\n"
    )
    unsafe = (
        "diff --git a/unsafe.txt b/unsafe.txt\n"
        "--- a/unsafe.txt\n"
        "+++ b/unsafe.txt\n"
        "@@ -1,8 +1,8 @@\n"
        "-old\n"
        "+new\n"
        "@@ invalid\n"
        " context\n"
    )

    normalized = _normalize_existing_file_hunk_counts(valid + unsafe)

    assert normalized == valid.replace("-1,8 +1,8", "-1,1 +1,1") + unsafe


def test_following_headerless_pair_is_delimited_without_recounting_explicit_section() -> None:
    patch = (
        "diff --git a/first.txt b/first.txt\n"
        "--- a/first.txt\n"
        "+++ b/first.txt\n"
        "@@ -1 +1 @@\n"
        "-old first\n"
        "+new first\n"
        "--- a/second.txt\n"
        "+++ b/second.txt\n"
        "@@ -1 +1 @@\n"
        "-old second\n"
        "+new second\n"
    )

    assert _normalize_existing_file_hunk_counts(patch) == patch
    assert _normalize_unified_diff(patch) == patch.replace(
        "--- a/second.txt\n",
        "diff --git a/second.txt b/second.txt\n--- a/second.txt\n",
    )
