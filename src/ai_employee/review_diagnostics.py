"""Body-free evidence for parent review failures; never used as authority."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from .domain.base import Digest
from .domain.v2 import DigestedRecordV2

ReviewStage = Literal["request_binding", "review", "response_parse", "response_contract"]
ReviewCode = Literal[
    "PARENT_REVIEW_REQUEST_BINDING_FAILED",
    "PARENT_REVIEW_FAILED",
    "PARENT_REVIEW_INVALID_JSON",
    "PARENT_REVIEW_INVALID_SCHEMA",
    "PARENT_REVIEW_CRITERIA_MISMATCH",
    "PARENT_REVIEW_NODES_MISMATCH",
    "PARENT_REVIEW_CONTRACT_MISMATCH",
]
ExceptionKind = Literal["ValueError", "TypeError", "KeyError", "OSError", "RuntimeError", "Other"]


def exception_kind(error: Exception) -> ExceptionKind:
    # Do not persist arbitrary subclass names or exception strings.
    kinds: tuple[tuple[type[Exception], ExceptionKind], ...] = (
        (KeyError, "KeyError"),
        (TypeError, "TypeError"),
        (OSError, "OSError"),
        (RuntimeError, "RuntimeError"),
        (ValueError, "ValueError"),
    )
    for cls, label in kinds:
        if isinstance(error, cls):
            return label
    return "Other"


class ParentReviewError(ValueError):
    def __init__(
        self,
        code: ReviewCode,
        stage: ReviewStage,
        error: Exception,
        *,
        response_digest: Digest | None = None,
        expected_criteria: int | None = None,
        received_criteria: int | None = None,
        expected_nodes: int | None = None,
        received_nodes: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.exception_kind = exception_kind(error)
        self.response_digest = response_digest
        self.expected_criteria = expected_criteria
        self.received_criteria = received_criteria
        self.expected_nodes = expected_nodes
        self.received_nodes = received_nodes


class ParentReviewFailure(DigestedRecordV2):
    schema_name: ClassVar[str] = "parent_review_failure"
    request_digest: Digest
    candidate_digest: Digest
    candidate_artifact_digest: Digest
    generation: int = Field(ge=0)
    review_attempt: int | None = Field(default=None, ge=0)
    stage: ReviewStage
    code: ReviewCode
    exception_kind: ExceptionKind
    response_digest: Digest | None = None
    expected_criteria: int | None = Field(default=None, ge=0)
    received_criteria: int | None = Field(default=None, ge=0)
    expected_nodes: int | None = Field(default=None, ge=0)
    received_nodes: int | None = Field(default=None, ge=0)
    message: Literal["Parent review failed; inspect the bounded classification and references."] = (
        "Parent review failed; inspect the bounded classification and references."
    )
    omitted_content: Literal["request_response_candidate_bodies_and_exception_text"] = (
        "request_response_candidate_bodies_and_exception_text"
    )
