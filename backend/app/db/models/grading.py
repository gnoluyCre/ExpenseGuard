"""F8 immutable two-dimensional grading snapshots."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, uuid_pk


class GradingRunStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class GradingSourceKind(StrEnum):
    DETERMINISTIC = "deterministic"
    CORRELATION = "correlation"


class GradingDisposition(StrEnum):
    HIGH_ATTENTION = "high_attention"
    MANUAL_ATTENTION = "manual_attention"
    CLEARED = "cleared"


def grading_run_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        [
            "grading_run_id",
            "tenant_id",
            "file_version_id",
            "validation_run_id",
            "detection_run_id",
        ],
        [
            "grading_run.id",
            "grading_run.tenant_id",
            "grading_run.file_version_id",
            "grading_run.validation_run_id",
            "grading_run.detection_run_id",
        ],
        name=name,
        ondelete="RESTRICT",
    )


class GradingConfig(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "grading_config"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_grading_config_tenant_version"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_grading_config_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "config_fingerprint",
            name="uq_grading_config_tenant_fingerprint",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "version",
            "schema_version",
            "config_fingerprint",
            "algorithm_version",
            name="uq_grading_config_snapshot",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_grading_config_creator_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("schema_version = 1", name="schema_version_value"),
        CheckConstraint("jsonb_typeof(definition_json) = 'object'", name="definition_object"),
        CheckConstraint(
            "canonical_definition::jsonb = definition_json",
            name="canonical_matches_definition",
        ),
        CheckConstraint(
            "octet_length(convert_to(canonical_definition, 'UTF8')) <= 262144",
            name="canonical_size",
        ),
        CheckConstraint("algorithm_version = 'cost-matrix-v1'", name="algorithm_version_value"),
        CheckConstraint("config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint_format"),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'", name="idempotency_key_hash_format"
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'", name="request_fingerprint_format"
        ),
        CheckConstraint(
            "char_length(change_reason) BETWEEN 1 AND 500 AND change_reason !~ '[[:cntrl:]]'",
            name="change_reason_valid",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canonical_definition: Mapped[str] = mapped_column(Text, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class GradingRun(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "grading_run"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "validation_run_id",
            "detection_run_id",
            name="uq_grading_run_identity",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "input_fingerprint",
            name="uq_grading_run_request_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "file_version_id",
            "validation_run_id",
            "detection_run_id",
            "grading_config_id",
            "input_fingerprint",
            name="uq_grading_run_business_identity",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_grading_run_file_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["validation_run_id", "tenant_id", "file_version_id"],
            ["validation_run.id", "validation_run.tenant_id", "validation_run.file_version_id"],
            name="fk_grading_run_validation_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["detection_run_id", "tenant_id", "file_version_id"],
            ["detection_run.id", "detection_run.tenant_id", "detection_run.file_version_id"],
            name="fk_grading_run_detection_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "grading_config_id",
                "tenant_id",
                "config_version",
                "config_schema_version",
                "config_fingerprint",
                "algorithm_version",
            ],
            [
                "grading_config.id",
                "grading_config.tenant_id",
                "grading_config.version",
                "grading_config.schema_version",
                "grading_config.config_fingerprint",
                "grading_config.algorithm_version",
            ],
            name="fk_grading_run_config_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_grading_run_creator_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("config_version > 0", name="config_version_positive"),
        CheckConstraint("config_schema_version = 1", name="config_schema_version_value"),
        CheckConstraint("algorithm_version = 'cost-matrix-v1'", name="algorithm_version_value"),
        CheckConstraint("jsonb_typeof(f3_manifest_json) = 'object'", name="f3_manifest_object"),
        CheckConstraint("jsonb_typeof(f6_manifest_json) = 'object'", name="f6_manifest_object"),
        CheckConstraint("jsonb_typeof(f7_manifest_json) = 'object'", name="f7_manifest_object"),
        CheckConstraint(
            "octet_length(convert_to(f3_manifest_json::text, 'UTF8')) <= 8388608",
            name="f3_manifest_size",
        ),
        CheckConstraint(
            "octet_length(convert_to(f6_manifest_json::text, 'UTF8')) <= 16777216",
            name="f6_manifest_size",
        ),
        CheckConstraint(
            "octet_length(convert_to(f7_manifest_json::text, 'UTF8')) <= 33554432",
            name="f7_manifest_size",
        ),
        CheckConstraint("config_fingerprint ~ '^[0-9a-f]{64}$'", name="config_fingerprint_format"),
        CheckConstraint("f3_manifest_fingerprint ~ '^[0-9a-f]{64}$'", name="f3_fingerprint_format"),
        CheckConstraint("f6_manifest_fingerprint ~ '^[0-9a-f]{64}$'", name="f6_fingerprint_format"),
        CheckConstraint("f7_manifest_fingerprint ~ '^[0-9a-f]{64}$'", name="f7_fingerprint_format"),
        CheckConstraint("input_fingerprint ~ '^[0-9a-f]{64}$'", name="input_fingerprint_format"),
        CheckConstraint("status IN ('in_progress', 'completed')", name="status_values"),
        CheckConstraint(
            "deterministic_item_count >= 0 AND correlation_item_count >= 0 "
            "AND high_attention_count >= 0 AND manual_attention_count >= 0 "
            "AND cleared_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "deterministic_item_count + correlation_item_count = "
            "high_attention_count + manual_attention_count + cleared_count",
            name="counts_arithmetic_consistent",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="completion_consistent",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at", name="completed_after_created"
        ),
        Index("ix_grading_run_tenant_file_created", "tenant_id", "file_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    validation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    grading_config_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    f3_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    f3_manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    f6_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    f6_manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    f7_manifest_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    f7_manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[GradingRunStatus] = mapped_column(String(32), nullable=False)
    deterministic_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    correlation_item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    high_attention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_attention_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cleared_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GradingRequest(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "grading_request"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_grading_request_tenant_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["grading_run_id", "tenant_id", "file_version_id", "input_fingerprint"],
            [
                "grading_run.id",
                "grading_run.tenant_id",
                "grading_run.file_version_id",
                "grading_run.input_fingerprint",
            ],
            name="fk_grading_request_run_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'", name="idempotency_key_hash_format"
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'", name="request_fingerprint_format"
        ),
        CheckConstraint("input_fingerprint ~ '^[0-9a-f]{64}$'", name="input_fingerprint_format"),
        Index("ix_grading_request_run_id", "grading_run_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    grading_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class GradingItem(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "grading_item"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "grading_run_id",
            "file_version_id",
            "tenant_id",
            name="uq_grading_item_identity",
        ),
        UniqueConstraint(
            "grading_run_id", "item_fingerprint", name="uq_grading_item_run_fingerprint"
        ),
        grading_run_fk("fk_grading_item_run_identity"),
        ForeignKeyConstraint(
            ["finding_id", "validation_run_id", "file_version_id", "tenant_id", "rule_kind"],
            [
                "finding.id",
                "finding.validation_run_id",
                "finding.file_version_id",
                "finding.tenant_id",
                "finding.rule_kind",
            ],
            name="fk_grading_item_finding_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "correlation_finding_id",
                "detection_run_id",
                "file_version_id",
                "tenant_id",
                "detector",
                "f6_detector_version",
                "f6_finding_key",
            ],
            [
                "correlation_finding.id",
                "correlation_finding.detection_run_id",
                "correlation_finding.file_version_id",
                "correlation_finding.tenant_id",
                "correlation_finding.detector",
                "correlation_finding.detector_version",
                "correlation_finding.finding_key",
            ],
            name="fk_grading_item_correlation_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "capability_declaration_id",
                "detection_run_id",
                "file_version_id",
                "tenant_id",
                "f6_config_fingerprint",
                "detector",
                "f6_detector_version",
                "f6_capability_status",
            ],
            [
                "capability_declaration.id",
                "capability_declaration.detection_run_id",
                "capability_declaration.file_version_id",
                "capability_declaration.tenant_id",
                "capability_declaration.config_fingerprint",
                "capability_declaration.detector",
                "capability_declaration.detector_version",
                "capability_declaration.status",
            ],
            name="fk_grading_item_capability_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "investigation_run_id",
                "correlation_finding_id",
                "detection_run_id",
                "file_version_id",
                "tenant_id",
            ],
            [
                "investigation_run.id",
                "investigation_run.correlation_finding_id",
                "investigation_run.detection_run_id",
                "investigation_run.file_version_id",
                "investigation_run.tenant_id",
            ],
            name="fk_grading_item_investigation_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "investigation_result_id",
                "investigation_run_id",
                "correlation_finding_id",
                "detection_run_id",
                "file_version_id",
                "tenant_id",
                "f7_outcome",
                "f7_result_fingerprint",
            ],
            [
                "investigation_result.id",
                "investigation_result.investigation_run_id",
                "investigation_result.correlation_finding_id",
                "investigation_result.detection_run_id",
                "investigation_result.file_version_id",
                "investigation_result.tenant_id",
                "investigation_result.outcome",
                "investigation_result.result_fingerprint",
            ],
            name="fk_grading_item_investigation_result_snapshot",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source_kind IN ('deterministic', 'correlation')", name="source_kind_values"
        ),
        CheckConstraint(
            "(source_kind = 'deterministic' AND finding_id IS NOT NULL "
            "AND correlation_finding_id IS NULL AND capability_declaration_id IS NULL "
            "AND investigation_run_id IS NULL AND investigation_result_id IS NULL "
            "AND rule_kind IS NOT NULL AND f3_outcome IN ('flagged', 'unavailable') "
            "AND f3_evidence_fingerprint IS NOT NULL AND detector IS NULL "
            "AND f6_detector_version IS NULL AND f6_finding_key IS NULL "
            "AND f6_config_fingerprint IS NULL AND f6_capability_status IS NULL "
            "AND f6_evidence_fingerprint IS NULL "
            "AND f7_outcome IS NULL AND f7_evidence_sufficient IS NULL "
            "AND f7_input_fingerprint IS NULL AND f7_config_fingerprint IS NULL "
            "AND f7_result_fingerprint IS NULL AND f7_not_run_reason_code IS NULL) OR "
            "(source_kind = 'correlation' AND finding_id IS NULL "
            "AND correlation_finding_id IS NOT NULL AND rule_kind IS NULL "
            "AND f3_outcome IS NULL AND f3_evidence_fingerprint IS NULL "
            "AND capability_declaration_id IS NOT NULL AND detector IS NOT NULL "
            "AND f6_detector_version IS NOT NULL AND f6_finding_key IS NOT NULL "
            "AND f6_config_fingerprint IS NOT NULL AND f6_capability_status IS NOT NULL "
            "AND f6_evidence_fingerprint IS NOT NULL AND f7_outcome IS NOT NULL)",
            name="source_fields_consistent",
        ),
        CheckConstraint(
            "rule_kind IS NULL OR rule_kind IN "
            "('limit', 'invoice_type', 'timeliness', 'invoice_title', 'invoice_duplicate')",
            name="rule_kind_values",
        ),
        CheckConstraint(
            "detector IS NULL OR detector IN "
            "('split_invoice', 'sequential_invoice', 'frequency_anomaly', 'spatiotemporal_tier0')",
            name="detector_values",
        ),
        CheckConstraint(
            "f6_capability_status IS NULL OR "
            "f6_capability_status IN ('enabled', 'degraded', 'unavailable')",
            name="capability_values",
        ),
        CheckConstraint(
            "f7_outcome IS NULL OR f7_outcome IN "
            "('sufficient', 'insufficient', 'unavailable', 'max_steps', 'failed', 'not_run')",
            name="f7_outcome_values",
        ),
        CheckConstraint(
            "f7_outcome IS NULL OR "
            "(f7_outcome = 'sufficient' AND f7_evidence_sufficient IS TRUE "
            "AND investigation_run_id IS NOT NULL AND investigation_result_id IS NOT NULL "
            "AND f7_input_fingerprint IS NOT NULL AND f7_config_fingerprint IS NOT NULL "
            "AND f7_result_fingerprint IS NOT NULL "
            "AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome = 'insufficient' AND f7_evidence_sufficient IS FALSE "
            "AND investigation_run_id IS NOT NULL AND investigation_result_id IS NOT NULL "
            "AND f7_input_fingerprint IS NOT NULL AND f7_config_fingerprint IS NOT NULL "
            "AND f7_result_fingerprint IS NOT NULL "
            "AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome IN ('unavailable', 'max_steps', 'failed') "
            "AND f7_evidence_sufficient IS NULL AND investigation_run_id IS NOT NULL "
            "AND investigation_result_id IS NOT NULL AND f7_input_fingerprint IS NOT NULL "
            "AND f7_config_fingerprint IS NOT NULL "
            "AND f7_result_fingerprint IS NOT NULL AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome = 'not_run' AND f7_evidence_sufficient IS NULL "
            "AND investigation_run_id IS NULL AND investigation_result_id IS NULL "
            "AND f7_input_fingerprint IS NULL AND f7_config_fingerprint IS NULL "
            "AND f7_result_fingerprint IS NULL "
            "AND f7_not_run_reason_code = 'INVESTIGATION_NOT_RUN')",
            name="f7_terminal_consistent",
        ),
        CheckConstraint("severity_impact BETWEEN 0 AND 3", name="impact_range"),
        CheckConstraint("severity_confidence BETWEEN 0 AND 3", name="confidence_range"),
        CheckConstraint(
            "disposition IN ('high_attention', 'manual_attention', 'cleared')",
            name="disposition_values",
        ),
        CheckConstraint("item_fingerprint ~ '^[0-9a-f]{64}$'", name="item_fingerprint_format"),
        CheckConstraint(
            "f3_evidence_fingerprint IS NULL OR f3_evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f3_evidence_fingerprint_format",
        ),
        CheckConstraint(
            "f6_finding_key IS NULL OR f6_finding_key ~ '^[0-9a-f]{64}$'",
            name="f6_finding_key_format",
        ),
        CheckConstraint(
            "f6_config_fingerprint IS NULL OR f6_config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f6_config_fingerprint_format",
        ),
        CheckConstraint(
            "f6_evidence_fingerprint IS NULL OR f6_evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f6_evidence_fingerprint_format",
        ),
        CheckConstraint(
            "f7_input_fingerprint IS NULL OR f7_input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f7_input_fingerprint_format",
        ),
        CheckConstraint(
            "f7_config_fingerprint IS NULL OR f7_config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f7_config_fingerprint_format",
        ),
        CheckConstraint(
            "f7_result_fingerprint IS NULL OR f7_result_fingerprint ~ '^[0-9a-f]{64}$'",
            name="f7_result_fingerprint_format",
        ),
        CheckConstraint("jsonb_typeof(reason_codes_json) = 'array'", name="reason_codes_array"),
        CheckConstraint(
            "jsonb_array_length(reason_codes_json) BETWEEN 1 AND 16",
            name="reason_codes_count",
        ),
        CheckConstraint(
            "octet_length(convert_to(reason_codes_json::text, 'UTF8')) <= 8192",
            name="reason_codes_size",
        ),
        CheckConstraint("f8_reason_codes_valid(reason_codes_json)", name="reason_codes_valid"),
        CheckConstraint("jsonb_typeof(evidence_snapshot) = 'object'", name="evidence_object"),
        CheckConstraint(
            "octet_length(convert_to(evidence_snapshot::text, 'UTF8')) <= 262144",
            name="evidence_size",
        ),
        CheckConstraint("first_row_no > 0", name="first_row_no_positive"),
        Index(
            "uq_grading_item_deterministic_source",
            "grading_run_id",
            "finding_id",
            unique=True,
            postgresql_where=text("source_kind = 'deterministic'"),
        ),
        Index(
            "uq_grading_item_correlation_source",
            "grading_run_id",
            "correlation_finding_id",
            unique=True,
            postgresql_where=text("source_kind = 'correlation'"),
        ),
        Index("ix_grading_item_run_first_row", "grading_run_id", "first_row_no"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    grading_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    validation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_kind: Mapped[GradingSourceKind] = mapped_column(String(32), nullable=False)
    finding_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    correlation_finding_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    capability_declaration_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    investigation_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    investigation_result_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rule_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f3_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    f3_evidence_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f6_detector_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    f6_finding_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f6_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f6_capability_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    f6_evidence_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f7_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    f7_evidence_sufficient: Mapped[bool | None] = mapped_column(nullable=True)
    f7_input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f7_config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f7_result_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    f7_not_run_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity_impact: Mapped[int] = mapped_column(Integer, nullable=False)
    severity_confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    disposition: Mapped[GradingDisposition] = mapped_column(String(32), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    evidence_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    item_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    first_row_no: Mapped[int] = mapped_column(Integer, nullable=False)


class GradingItemRow(Base, TenantScopedMixin, TimestampMixin):
    __tablename__ = "grading_item_row"
    __table_args__ = (
        UniqueConstraint("grading_item_id", "ordinal", name="uq_grading_item_row_item_ordinal"),
        UniqueConstraint("grading_item_id", "row_no", name="uq_grading_item_row_item_row"),
        ForeignKeyConstraint(
            ["grading_item_id", "grading_run_id", "file_version_id", "tenant_id"],
            [
                "grading_item.id",
                "grading_item.grading_run_id",
                "grading_item.file_version_id",
                "grading_item.tenant_id",
            ],
            name="fk_grading_item_row_item_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "row_no", "tenant_id"],
            ["expense_row.file_version_id", "expense_row.row_no", "expense_row.tenant_id"],
            name="fk_grading_item_row_expense_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint("row_no > 0", name="row_no_positive"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        CheckConstraint(
            "source_row_fingerprint ~ '^[0-9a-f]{64}$'",
            name="source_row_fingerprint_format",
        ),
        Index("ix_grading_item_row_run_row", "grading_run_id", "row_no"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    grading_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    grading_item_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
