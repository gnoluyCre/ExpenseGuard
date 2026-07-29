"""Strict internal models for F5 review and sampling workflows."""

from __future__ import annotations

import unicodedata
import uuid
from datetime import date, datetime
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models.findings import ReviewDecision, SamplingReviewDecision
from app.db.models.reports import (
    ReportAttentionGroup,
    ReportCitationStatus,
)

SamplingAlgorithmVersion = Literal["sha256-rank-v1"]
SAMPLING_ALGORITHM_VERSION: Final[SamplingAlgorithmVersion] = "sha256-rank-v1"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SamplingConfigParameters(_StrictModel):
    """The complete immutable parameter snapshot used by one sampling plan."""

    rate_bps: int = Field(ge=1, le=10_000)
    min_sample_size: int = Field(ge=1)
    max_sample_size: int = Field(ge=1)
    algorithm_version: SamplingAlgorithmVersion = SAMPLING_ALGORITHM_VERSION

    @model_validator(mode="after")
    def validate_sample_size_order(self) -> Self:
        if self.max_sample_size < self.min_sample_size:
            raise ValueError("max_sample_size must be greater than or equal to min_sample_size")
        return self


class SamplingSelection(_StrictModel):
    """One selected eligible row with its frozen deterministic rank evidence."""

    row_no: int = Field(ge=1)
    selection_rank: int = Field(ge=1)
    selection_score_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SamplingConfigCreateCommand(SamplingConfigParameters):
    """Validated service command for an append-only tenant config version."""

    expected_current_version: int = Field(ge=0)
    change_reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("change_reason")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ValueError("change_reason must not contain control characters")
        return value


class SamplingConfigResult(SamplingConfigParameters):
    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_by: uuid.UUID
    created_at: datetime
    change_reason: str
    reused_existing: bool


class SamplingPlanResult(SamplingConfigParameters):
    id: uuid.UUID
    tenant_id: uuid.UUID
    report_run_id: uuid.UUID
    file_version_id: uuid.UUID
    sampling_config_id: uuid.UUID
    config_version: int = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed_hex: str = Field(pattern=r"^[0-9a-f]{64}$")
    eligible_count: int = Field(ge=0)
    sample_size: int = Field(ge=0)
    created_by: uuid.UUID
    created_at: datetime
    selections: tuple[SamplingSelection, ...]
    reused_existing: bool


class FindingReviewCommand(_StrictModel):
    decision: ReviewDecision
    note: str | None = Field(default=None, min_length=1, max_length=2_000)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_note(self) -> Self:
        _validate_decision_note(
            note=self.note,
            required=self.decision is ReviewDecision.FALSE_POSITIVE,
        )
        return self


class SamplingReviewCommand(_StrictModel):
    decision: SamplingReviewDecision
    note: str | None = Field(default=None, min_length=1, max_length=2_000)
    idempotency_key: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_note(self) -> Self:
        _validate_decision_note(
            note=self.note,
            required=self.decision is SamplingReviewDecision.MISSED_ISSUE,
        )
        return self


class FindingReviewResult(_StrictModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    report_run_id: uuid.UUID
    report_item_id: uuid.UUID
    file_version_id: uuid.UUID
    finding_id: uuid.UUID
    decision: ReviewDecision
    reviewer_id: uuid.UUID
    reviewed_at: datetime
    note: str | None
    reused_existing: bool


class SamplingReviewResult(_StrictModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    sampling_audit_id: uuid.UUID
    sampling_plan_id: uuid.UUID
    report_run_id: uuid.UUID
    file_version_id: uuid.UUID
    decision: SamplingReviewDecision
    reviewer_id: uuid.UUID
    reviewed_at: datetime
    note: str | None
    reused_existing: bool


ReviewQueueKind = Literal["finding", "clearance_sample"]
ReviewQueueStatus = Literal["pending", "completed"]
SamplingPlanStatus = Literal["completed", "legacy_not_initialized"]


class ReviewQueueItem(_StrictModel):
    kind: ReviewQueueKind
    status: ReviewQueueStatus
    target_id: uuid.UUID
    report_run_id: uuid.UUID
    file_version_id: uuid.UUID
    report_completed_at: datetime
    row_no: int = Field(ge=1)
    attention_group: ReportAttentionGroup | None
    finding_id: uuid.UUID | None
    rule_id: str | None
    rule_version: str | None
    sampling_plan_id: uuid.UUID | None
    selection_rank: int | None = Field(default=None, ge=1)
    decision: ReviewDecision | SamplingReviewDecision | None
    reviewer_id: uuid.UUID | None
    reviewed_at: datetime | None


class ReviewQueuePage(_StrictModel):
    items: tuple[ReviewQueueItem, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ReviewSummary(_StrictModel):
    report_run_id: uuid.UUID
    sampling_status: SamplingPlanStatus
    finding_pending: int = Field(ge=0)
    finding_completed: int = Field(ge=0)
    finding_confirmed: int = Field(ge=0)
    finding_false_positive: int = Field(ge=0)
    sample_eligible: int = Field(ge=0)
    sample_selected: int = Field(ge=0)
    sample_pending: int = Field(ge=0)
    sample_completed: int = Field(ge=0)
    sample_clearance_confirmed: int = Field(ge=0)
    sample_missed_issue: int = Field(ge=0)


class ReviewCitationEvidence(_StrictModel):
    id: uuid.UUID
    report_item_id: uuid.UUID
    binding_id: uuid.UUID
    citation_order: int = Field(ge=1, le=3)
    policy_family_id: uuid.UUID
    family_stable_key: str
    policy_document_id: uuid.UUID
    document_title: str
    document_version: str
    effective_date: date
    expiry_date: date | None
    document_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_clause_id: uuid.UUID
    clause_no: str
    hierarchy_path: str | None
    clause_text: str
    clause_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: str
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewItemEvidence(_StrictModel):
    id: uuid.UUID
    finding_id: uuid.UUID
    row_no: int = Field(ge=1)
    rule_id: str
    rule_version: str | None
    source_outcome: str
    source_verdict: str
    reason_code: str
    reasoning_snapshot: str | None
    evidence_snapshot: dict[str, Any] | None
    attention_group: ReportAttentionGroup
    citation_status: ReportCitationStatus
    requires_manual_citation: bool
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    citations: tuple[ReviewCitationEvidence, ...]


class FindingReviewDetail(_StrictModel):
    report_run_id: uuid.UUID
    report_item: ReviewItemEvidence
    raw_row: dict[str, Any]
    normalized_row: dict[str, Any] | None
    existing_review: FindingReviewResult | None


class ClearanceReviewDetail(_StrictModel):
    report_run_id: uuid.UUID
    sampling_audit_id: uuid.UUID
    sampling_plan_id: uuid.UUID
    file_version_id: uuid.UUID
    row_no: int = Field(ge=1)
    raw_row: dict[str, Any]
    normalized_row: dict[str, Any] | None
    source_verdict: Literal["passed"]
    ruleset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleared_items: tuple[ReviewItemEvidence, ...]
    existing_review: SamplingReviewResult | None


def _validate_decision_note(*, note: str | None, required: bool) -> None:
    if required and note is None:
        raise ValueError("note is required for this decision")
    if note is not None and any(unicodedata.category(character) == "Cc" for character in note):
        raise ValueError("note must not contain control characters")
