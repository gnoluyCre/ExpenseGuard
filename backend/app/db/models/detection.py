"""F6 cross-row detection persistence models."""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, uuid_pk


class DetectorKind(StrEnum):
    """The four deterministic F6 statistical detectors."""

    SPLIT_INVOICE = "split_invoice"
    SEQUENTIAL_INVOICE = "sequential_invoice"
    FREQUENCY_ANOMALY = "frequency_anomaly"
    SPATIOTEMPORAL_TIER0 = "spatiotemporal_tier0"


class DetectionConfig(Base, TenantScopedMixin, TimestampMixin):
    """Immutable tenant-scoped detector profile snapshot."""

    __tablename__ = "detection_config"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_detection_config_tenant_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_detection_config_tenant_idempotency_key",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "version",
            "config_fingerprint",
            "algorithm_bundle_version",
            name="uq_detection_config_snapshot",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_detection_config_creator_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("schema_version = 1", name="schema_version_value"),
        CheckConstraint(
            "algorithm_bundle_version = 'correlation-v1'",
            name="algorithm_bundle_version_value",
        ),
        CheckConstraint(
            "jsonb_typeof(definition) = 'object'",
            name="definition_object",
        ),
        CheckConstraint(
            "definition_canonical::jsonb = definition",
            name="definition_canonical_matches",
        ),
        CheckConstraint(
            "octet_length(convert_to(definition_canonical, 'UTF8')) <= 262144",
            name="definition_canonical_size",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="config_fingerprint_format",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="idempotency_key_hash_format",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="request_fingerprint_format",
        ),
        CheckConstraint(
            "char_length(change_reason) BETWEEN 1 AND 500 AND change_reason !~ '[[:cntrl:]]'",
            name="change_reason_valid",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_canonical: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    algorithm_bundle_version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class DetectionRun(Base, TenantScopedMixin, TimestampMixin):
    """Immutable successful run for one file revision and profile fingerprint."""

    __tablename__ = "detection_run"
    __table_args__ = (
        UniqueConstraint(
            "file_version_id",
            "config_fingerprint",
            name="uq_detection_run_file_config_fingerprint",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_detection_run_id_tenant_file",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "config_fingerprint",
            name="uq_detection_run_identity",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_detection_run_file_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "detection_config_id",
                "tenant_id",
                "config_version",
                "config_fingerprint",
                "algorithm_bundle_version",
            ],
            [
                "detection_config.id",
                "detection_config.tenant_id",
                "detection_config.version",
                "detection_config.config_fingerprint",
                "detection_config.algorithm_bundle_version",
            ],
            name="fk_detection_run_config_snapshot",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_detection_run_creator_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("config_version > 0", name="config_version_positive"),
        CheckConstraint(
            "algorithm_bundle_version = 'correlation-v1'",
            name="algorithm_bundle_version_value",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="config_fingerprint_format",
        ),
        CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="input_fingerprint_format",
        ),
        CheckConstraint(
            "run_fingerprint ~ '^[0-9a-f]{64}$'",
            name="run_fingerprint_format",
        ),
        CheckConstraint(
            "source_row_count >= 0 AND parsed_row_count >= 0 "
            "AND error_row_count >= 0 AND finding_count >= 0",
            name="counts_non_negative",
        ),
        CheckConstraint(
            "source_row_count = parsed_row_count + error_row_count",
            name="source_count_consistent",
        ),
        CheckConstraint(
            "completed_at >= created_at",
            name="completed_after_created",
        ),
        Index("ix_detection_run_tenant_file_created", "tenant_id", "file_version_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_config_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_bundle_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    run_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parsed_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DetectionRequest(Base, TenantScopedMixin, TimestampMixin):
    """Immutable idempotency-key alias for a completed detection run."""

    __tablename__ = "detection_request"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_detection_request_tenant_idempotency_key",
        ),
        ForeignKeyConstraint(
            ["detection_run_id", "tenant_id", "file_version_id"],
            ["detection_run.id", "detection_run.tenant_id", "detection_run.file_version_id"],
            name="fk_detection_request_run_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="idempotency_key_hash_format",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="request_fingerprint_format",
        ),
        Index("ix_detection_request_run_id", "detection_run_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class CorrelationFindingRow(Base, TenantScopedMixin, TimestampMixin):
    """Physical participating-row identity for a correlation finding."""

    __tablename__ = "correlation_finding_row"
    __table_args__ = (
        UniqueConstraint(
            "finding_id",
            "row_no",
            name="uq_correlation_finding_row_finding_row",
        ),
        UniqueConstraint(
            "finding_id",
            "ordinal",
            name="uq_correlation_finding_row_finding_ordinal",
        ),
        ForeignKeyConstraint(
            ["finding_id", "detection_run_id", "file_version_id", "tenant_id"],
            [
                "correlation_finding.id",
                "correlation_finding.detection_run_id",
                "correlation_finding.file_version_id",
                "correlation_finding.tenant_id",
            ],
            name="fk_correlation_finding_row_finding_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "row_no", "tenant_id"],
            ["expense_row.file_version_id", "expense_row.row_no", "expense_row.tenant_id"],
            name="fk_correlation_finding_row_expense_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint("row_no > 0", name="row_no_positive"),
        CheckConstraint("ordinal > 0", name="ordinal_positive"),
        Index(
            "ix_correlation_finding_row_expense_identity",
            "file_version_id",
            "row_no",
            "tenant_id",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
