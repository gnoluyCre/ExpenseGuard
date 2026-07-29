"""Immutable F4 report snapshots and XLSX artifact metadata."""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, str_enum, uuid_pk


class ReportRunStatus(StrEnum):
    """Only transactional in-progress and complete snapshots are persisted."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ReportAttentionGroup(StrEnum):
    """F4 single-axis operational grouping, independent from F8 severity."""

    HIGH_ATTENTION = "high_attention"
    MANUAL_ATTENTION = "manual_attention"
    CLEARED = "cleared"


class ReportCitationStatus(StrEnum):
    """Whether all expected citations for an item are exact and available."""

    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


class ReportExportStatus(StrEnum):
    """Lifecycle of a persisted XLSX artifact record."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ReportRun(Base, TenantScopedMixin, TimestampMixin):
    """First-success immutable report snapshot for one file revision."""

    __tablename__ = "report_run"
    __table_args__ = (
        UniqueConstraint("file_version_id"),
        UniqueConstraint("tenant_id", "report_fingerprint"),
        UniqueConstraint("tenant_id", "idempotency_key_hash"),
        UniqueConstraint("id", "tenant_id", name="uq_report_run_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_report_run_id_tenant_id_file_version_id",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_run_file_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["validation_run_id", "tenant_id", "file_version_id"],
            ["validation_run.id", "validation_run.tenant_id", "validation_run.file_version_id"],
            name="fk_report_run_validation_tenant_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_report_run_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_report_run_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_length(report_fingerprint) = 64", name="fingerprint_length"),
        CheckConstraint("char_length(source_content_sha256) = 64", name="source_hash_length"),
        CheckConstraint("char_length(ruleset_fingerprint) = 64", name="ruleset_hash_length"),
        CheckConstraint("char_length(idempotency_key_hash) = 64", name="idempotency_hash_length"),
        CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="request_fingerprint_length",
        ),
        CheckConstraint(
            "stored_row_count >= 0 AND validated_row_count >= 0 "
            "AND flagged_row_count >= 0 AND manual_review_row_count >= 0 "
            "AND passed_row_count >= 0 AND parse_error_row_count >= 0 "
            "AND report_item_count >= 0 AND verified_citation_count >= 0 "
            "AND unavailable_citation_count >= 0 "
            "AND high_attention_row_count >= 0 "
            "AND manual_attention_row_count >= 0 AND cleared_row_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "stored_row_count = validated_row_count + parse_error_row_count",
            name="stored_count_consistent",
        ),
        CheckConstraint(
            "validated_row_count = flagged_row_count + manual_review_row_count + passed_row_count",
            name="validated_count_consistent",
        ),
        CheckConstraint(
            "manual_attention_row_count = manual_review_row_count + parse_error_row_count",
            name="manual_attention_count_consistent",
        ),
        CheckConstraint(
            "high_attention_row_count = flagged_row_count AND cleared_row_count = passed_row_count",
            name="attention_counts_consistent",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="completion_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    validation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[ReportRunStatus] = mapped_column(
        str_enum(ReportRunStatus, "report_run_status_enum"), nullable=False
    )
    report_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    attention_mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    binding_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    stored_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validated_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flagged_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_review_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_error_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    report_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    verified_citation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unavailable_citation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    high_attention_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_attention_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cleared_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReportRequest(Base, TenantScopedMixin, TimestampMixin):
    """Append-only idempotency key mapping for report generation requests."""

    __tablename__ = "report_request"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key_hash"),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_request_file_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_request_report_tenant_file",
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_length(idempotency_key_hash) = 64", name="idempotency_hash_length"),
        CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="request_fingerprint_length",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    report_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class ReportItem(Base, TenantScopedMixin, TimestampMixin):
    """Finding-level evidence snapshot; multiple findings on one row stay separate."""

    __tablename__ = "report_item"
    __table_args__ = (
        UniqueConstraint("report_run_id", "finding_id"),
        UniqueConstraint("id", "tenant_id", name="uq_report_item_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "report_run_id",
            name="uq_report_item_id_tenant_id_report_run_id",
        ),
        Index(
            "ix_report_item_report_attention_order",
            "report_run_id",
            "attention_group",
            "row_no",
            "rule_id",
            "rule_version",
            "finding_id",
        ),
        ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_item_report_tenant_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["finding_id", "tenant_id", "file_version_id"],
            ["finding.id", "finding.tenant_id", "finding.file_version_id"],
            name="fk_report_item_finding_tenant_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_item_file_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rule_config_id", "tenant_id"],
            ["rule_config.id", "rule_config.tenant_id"],
            name="fk_report_item_rule_config_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_outcome IN ('passed', 'flagged', 'unavailable', 'exempted')",
            name="source_outcome_values",
        ),
        CheckConstraint(
            "source_verdict IN ('flagged', 'manual_review', 'passed')",
            name="source_verdict_values",
        ),
        CheckConstraint(
            "attention_group IN ('high_attention', 'manual_attention', 'cleared')",
            name="attention_group_values",
        ),
        CheckConstraint(
            "citation_status IN ('verified', 'unavailable')",
            name="citation_status_values",
        ),
        CheckConstraint(
            "(citation_status = 'verified' AND requires_manual_citation = false) OR "
            "(citation_status = 'unavailable' AND requires_manual_citation = true)",
            name="citation_manual_consistent",
        ),
        CheckConstraint("char_length(source_content_sha256) = 64", name="source_hash_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    finding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_config_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    source_verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    reasoning_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attention_group: Mapped[ReportAttentionGroup] = mapped_column(
        str_enum(ReportAttentionGroup, "report_attention_group_enum"), nullable=False
    )
    citation_status: Mapped[ReportCitationStatus] = mapped_column(
        str_enum(ReportCitationStatus, "report_citation_status_enum"), nullable=False
    )
    requires_manual_citation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ReportParseError(Base, TenantScopedMixin, TimestampMixin):
    """Safe parse-error snapshot for rows excluded from deterministic validation."""

    __tablename__ = "report_parse_error"
    __table_args__ = (
        UniqueConstraint("report_run_id", "row_no", "error_code", "column_name"),
        Index(
            "ix_report_parse_error_report_order",
            "report_run_id",
            "row_no",
            "error_code",
            "column_name",
        ),
        ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_parse_error_report_tenant_file",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_parse_error_file_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("char_length(source_content_sha256) = 64", name="source_hash_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str] = mapped_column(String(128), nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ReportCitation(Base, TenantScopedMixin, TimestampMixin):
    """Exact citation display snapshot independent from live policy state."""

    __tablename__ = "report_citation"
    __table_args__ = (
        UniqueConstraint("report_item_id", "citation_order"),
        ForeignKeyConstraint(
            ["report_item_id", "tenant_id", "report_run_id"],
            ["report_item.id", "report_item.tenant_id", "report_item.report_run_id"],
            name="fk_report_citation_item_tenant_report",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "binding_id",
                "tenant_id",
                "policy_family_id",
                "policy_document_id",
                "policy_clause_id",
            ],
            [
                "rule_policy_binding.id",
                "rule_policy_binding.tenant_id",
                "rule_policy_binding.policy_family_id",
                "rule_policy_binding.policy_document_id",
                "rule_policy_binding.policy_clause_id",
            ],
            name="fk_report_citation_binding_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_family_id", "tenant_id"],
            ["policy_family.id", "policy_family.tenant_id"],
            name="fk_report_citation_family_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_document_id", "tenant_id", "policy_family_id"],
            ["policy_document.id", "policy_document.tenant_id", "policy_document.family_id"],
            name="fk_report_citation_document_tenant_family",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_clause_id", "tenant_id", "policy_document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_report_citation_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("citation_order BETWEEN 1 AND 3", name="citation_order_range"),
        CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start",
            name="quote_offsets_valid",
        ),
        CheckConstraint("char_length(quote) > 0 AND quote ~ '\\S'", name="quote_nonblank"),
        CheckConstraint("char_length(document_content_sha256) = 64", name="document_hash_length"),
        CheckConstraint("char_length(clause_text_sha256) = 64", name="clause_hash_length"),
        CheckConstraint("char_length(quote_sha256) = 64", name="quote_hash_length"),
        CheckConstraint("verification_status = 'verified_exact'", name="verified_exact_only"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    report_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    policy_family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    policy_document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    policy_clause_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    family_stable_key: Mapped[str] = mapped_column(String(128), nullable=False)
    document_title: Mapped[str] = mapped_column(String(512), nullable=False)
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    document_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    clause_no: Mapped[str] = mapped_column(String(64), nullable=False)
    hierarchy_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    clause_text: Mapped[str] = mapped_column(Text, nullable=False)
    clause_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False)
    citation_order: Mapped[int] = mapped_column(Integer, nullable=False)


class ReportExport(Base, TenantScopedMixin, TimestampMixin):
    """Idempotent XLSX artifact record; binary generation belongs to CP-F4.4."""

    __tablename__ = "report_export"
    __table_args__ = (
        UniqueConstraint("report_run_id", "format", "template_version"),
        UniqueConstraint("tenant_id", "idempotency_key_hash"),
        UniqueConstraint("tenant_id", "artifact_storage_key"),
        ForeignKeyConstraint(
            ["report_run_id", "tenant_id"],
            ["report_run.id", "report_run.tenant_id"],
            name="fk_report_export_report_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_report_export_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("format = 'xlsx'", name="xlsx_only"),
        CheckConstraint("char_length(idempotency_key_hash) = 64", name="idempotency_hash_length"),
        CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="request_fingerprint_length",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND artifact_storage_key IS NULL "
            "AND artifact_sha256 IS NULL AND size_bytes IS NULL "
            "AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'completed' AND artifact_storage_key IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND char_length(artifact_sha256) = 64 "
            "AND size_bytes > 0 AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND artifact_storage_key IS NULL "
            "AND artifact_sha256 IS NULL AND size_bytes IS NULL "
            "AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name="lifecycle_fields",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    report_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ReportExportStatus] = mapped_column(
        str_enum(ReportExportStatus, "report_export_status_enum"), nullable=False
    )
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
