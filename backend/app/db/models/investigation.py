"""F7 anomaly-investigation persistence models."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
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


class InvestigationProviderKind(StrEnum):
    """Provider boundary frozen into an investigation run."""

    DISABLED = "disabled"
    OPENAI_COMPATIBLE = "openai_compatible"


class InvestigationActionKind(StrEnum):
    """The only two actions accepted from the F7 agent."""

    TOOL_CALL = "tool_call"
    TERMINATE = "terminate"


class InvestigationOutcome(StrEnum):
    """Terminal outcomes; only two carry an evidence-sufficiency judgment."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    MAX_STEPS = "max_steps"
    FAILED = "failed"


class PiiKind(StrEnum):
    """PII identities that may be replaced by stable tenant tokens."""

    EMPLOYEE_NAME = "employee_name"
    EMPLOYEE_ID = "employee_id"
    NATIONAL_ID = "national_id"
    PHONE = "phone"
    BANK_ACCOUNT = "bank_account"
    SUPPLIER = "supplier"
    MERCHANT = "merchant"


def investigation_run_fk(name: str) -> ForeignKeyConstraint:
    """Bind a child fact to the complete immutable investigation identity."""

    return ForeignKeyConstraint(
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
        name=name,
        ondelete="RESTRICT",
    )


class InvestigationRun(Base, TenantScopedMixin, TimestampMixin):
    """Immutable input and provider snapshot for one explicit F6 candidate."""

    __tablename__ = "investigation_run"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            name="uq_investigation_run_identity",
        ),
        ForeignKeyConstraint(
            ["correlation_finding_id", "detection_run_id", "file_version_id", "tenant_id"],
            [
                "correlation_finding.id",
                "correlation_finding.detection_run_id",
                "correlation_finding.file_version_id",
                "correlation_finding.tenant_id",
            ],
            name="fk_investigation_run_correlation_identity",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_investigation_run_actor_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "agent_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="agent_version_format",
        ),
        CheckConstraint("action_schema_version = 1", name="action_schema_version_value"),
        CheckConstraint(
            "prompt_template_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name="prompt_template_version_format",
        ),
        CheckConstraint(
            "provider_kind IN ('disabled', 'openai_compatible')",
            name="provider_kind_values",
        ),
        CheckConstraint(
            "char_length(provider_model) BETWEEN 1 AND 255 AND provider_model !~ '[[:cntrl:]]'",
            name="provider_model_valid",
        ),
        CheckConstraint("max_steps BETWEEN 1 AND 12", name="max_steps_range"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 120", name="timeout_seconds_range"),
        CheckConstraint(
            "redaction_version ~ '^v[1-9][0-9]{0,8}$'",
            name="redaction_version_format",
        ),
        CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name="input_fingerprint_format",
        ),
        CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name="config_fingerprint_format",
        ),
        Index(
            "ix_investigation_run_candidate_created",
            "tenant_id",
            "detection_run_id",
            "correlation_finding_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    correlation_finding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    action_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_kind: Mapped[InvestigationProviderKind] = mapped_column(String(32), nullable=False)
    provider_model: Mapped[str] = mapped_column(String(255), nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    redaction_version: Mapped[str] = mapped_column(String(16), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class InvestigationRequest(Base, TenantScopedMixin, TimestampMixin):
    """Immutable idempotency-key alias for an investigation run."""

    __tablename__ = "investigation_request"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_investigation_request_tenant_idempotency_key",
        ),
        investigation_run_fk("fk_investigation_request_run_identity"),
        CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name="idempotency_key_hash_format",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="request_fingerprint_format",
        ),
        Index("ix_investigation_request_run_id", "investigation_run_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    correlation_finding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class InvestigationResult(Base, TenantScopedMixin, TimestampMixin):
    """One immutable terminal judgment for an investigation run."""

    __tablename__ = "investigation_result"
    __table_args__ = (
        UniqueConstraint(
            "investigation_run_id",
            name="uq_investigation_result_run_id",
        ),
        UniqueConstraint(
            "id",
            "investigation_run_id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            "outcome",
            "result_fingerprint",
            name="uq_investigation_result_f8_snapshot",
        ),
        investigation_run_fk("fk_investigation_result_run_identity"),
        CheckConstraint(
            "outcome IN ('sufficient', 'insufficient', 'unavailable', 'max_steps', 'failed')",
            name="outcome_values",
        ),
        CheckConstraint(
            "(outcome = 'sufficient' AND evidence_sufficient IS TRUE) OR "
            "(outcome = 'insufficient' AND evidence_sufficient IS FALSE) OR "
            "(outcome IN ('unavailable', 'max_steps', 'failed') "
            "AND evidence_sufficient IS NULL)",
            name="outcome_sufficiency_consistent",
        ),
        CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 2000 AND summary !~ '[[:cntrl:]]'",
            name="summary_valid",
        ),
        CheckConstraint(
            "reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name="reason_code_format",
        ),
        CheckConstraint("jsonb_typeof(citations_json) = 'array'", name="citations_array"),
        CheckConstraint(
            "octet_length(convert_to(citations_json::text, 'UTF8')) <= 262144",
            name="citations_size",
        ),
        CheckConstraint(
            "result_fingerprint ~ '^[0-9a-f]{64}$'",
            name="result_fingerprint_format",
        ),
        CheckConstraint("completed_at >= created_at", name="completed_after_created"),
        Index("ix_investigation_result_candidate", "correlation_finding_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    investigation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    correlation_finding_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detection_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    outcome: Mapped[InvestigationOutcome] = mapped_column(String(32), nullable=False)
    evidence_sufficient: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    citations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PiiToken(Base, TenantScopedMixin, TimestampMixin):
    """Non-reversible stable tenant token; raw PII is deliberately absent."""

    __tablename__ = "pii_token"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "token_version",
            "pii_kind",
            "source_hmac",
            name="uq_pii_token_source_identity",
        ),
        UniqueConstraint("tenant_id", "token", name="uq_pii_token_tenant_token"),
        CheckConstraint(
            "token_version ~ '^v[1-9][0-9]{0,8}$'",
            name="token_version_format",
        ),
        CheckConstraint(
            "pii_kind IN ('employee_name', 'employee_id', 'national_id', 'phone', "
            "'bank_account', 'supplier', 'merchant')",
            name="pii_kind_values",
        ),
        CheckConstraint(
            "source_hmac ~ '^[0-9a-f]{64}$'",
            name="source_hmac_format",
        ),
        CheckConstraint(
            "token ~ '^[A-Z][A-Z0-9_]{1,31}_v[1-9][0-9]{0,8}_[0-9a-f]{16}$'",
            name="token_format",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    token_version: Mapped[str] = mapped_column(String(16), nullable=False)
    pii_kind: Mapped[PiiKind] = mapped_column(String(32), nullable=False)
    source_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
