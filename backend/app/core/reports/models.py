"""Strict internal models for immutable F4 report snapshots."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models.reports import (
    ReportAttentionGroup,
    ReportCitationStatus,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReportSummary(_StrictModel):
    """Stable result returned for a newly assembled or replayed report."""

    report_run_id: uuid.UUID
    file_version_id: uuid.UUID
    validation_run_id: uuid.UUID
    report_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    stored_row_count: int = Field(ge=0)
    report_item_count: int = Field(ge=0)
    verified_citation_count: int = Field(ge=0)
    unavailable_citation_count: int = Field(ge=0)
    reused_existing: bool


class CitationSnapshot(_StrictModel):
    """One verified citation copied from PostgreSQL into a report snapshot."""

    id: uuid.UUID
    report_item_id: uuid.UUID
    binding_id: uuid.UUID
    citation_order: int = Field(ge=1, le=3)
    family_stable_key: str
    document_title: str
    document_version: str
    clause_no: str
    quote: str
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReportItemSnapshot(_StrictModel):
    """Finding-level immutable snapshot with ordered verified citations."""

    id: uuid.UUID
    finding_id: uuid.UUID
    row_no: int = Field(ge=1)
    rule_id: str
    rule_version: str | None
    source_outcome: str
    source_verdict: str
    reason_code: str
    evidence_snapshot: dict[str, Any] | None
    attention_group: ReportAttentionGroup
    citation_status: ReportCitationStatus
    requires_manual_citation: bool
    citations: tuple[CitationSnapshot, ...]


class ParseErrorSnapshot(_StrictModel):
    """Safe parse failure copied into a completed report."""

    id: uuid.UUID
    row_no: int = Field(ge=1)
    error_code: str
    column_name: str
    message: str


class ReportSnapshot(_StrictModel):
    """Completed snapshot readable without Qdrant or live policy joins."""

    summary: ReportSummary
    policy_manifest: dict[str, Any]
    binding_manifest: dict[str, Any]
    items: tuple[ReportItemSnapshot, ...]
    parse_errors: tuple[ParseErrorSnapshot, ...]
