"""Versioned policy sources, clauses, retrieval indexes, and confirmed bindings."""

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, str_enum, uuid_pk


class PolicyDocumentStatus(StrEnum):
    """Lifecycle of an immutable policy document version."""

    LEGACY_UNPUBLISHED = "legacy_unpublished"
    DRAFT = "draft"
    INDEXING = "indexing"
    PUBLISHED = "published"
    FAILED = "failed"


class PolicyIndexGenerationStatus(StrEnum):
    """Lifecycle of a tenant-local vector index generation."""

    BUILDING = "building"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"


class PolicyDocumentIndexStatus(StrEnum):
    """Completeness state for one document in one index generation."""

    PENDING = "pending"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"


class PolicyIndexJobStatus(StrEnum):
    """Transactional-outbox job state."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class PolicyIndexOperation(StrEnum):
    """Supported idempotent vector-store mutations."""

    UPSERT = "upsert"
    DELETE = "delete"


class PolicyFamily(Base, TenantScopedMixin, TimestampMixin):
    """Stable identity shared by all versions of one policy."""

    __tablename__ = "policy_family"
    __table_args__ = (
        UniqueConstraint("tenant_id", "stable_key"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_family_id_tenant_id"),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_family_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "char_length(stable_key) BETWEEN 1 AND 128",
            name="stable_key_length",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    stable_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)


class PolicySourceBlob(Base, TenantScopedMixin, TimestampMixin):
    """Content-addressed reference to a policy source in private storage."""

    __tablename__ = "policy_source_blob"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_sha256"),
        UniqueConstraint("tenant_id", "storage_key"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_source_blob_id_tenant_id"),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_source_blob_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("char_length(content_sha256) = 64", name="content_sha256_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)


class PolicyDocument(Base, TenantScopedMixin, TimestampMixin):
    """One immutable content version with a half-open effective interval."""

    __tablename__ = "policy_document"

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    family_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_blob_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(nullable=True)
    extracted_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chunker_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[PolicyDocumentStatus] = mapped_column(
        str_enum(PolicyDocumentStatus, "policy_document_status_enum"),
        nullable=False,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        Index("ix_policy_document_tenant_id_effective_date", "tenant_id", "effective_date"),
        Index("ix_policy_document_family_id", "family_id"),
        Index("ix_policy_document_source_blob_id", "source_blob_id"),
        UniqueConstraint("tenant_id", "title", "version"),
        UniqueConstraint("tenant_id", "family_id", "version"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_document_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "family_id",
            name="uq_policy_document_id_tenant_id_family_id",
        ),
        ForeignKeyConstraint(
            ["family_id", "tenant_id"],
            ["policy_family.id", "policy_family.tenant_id"],
            name="fk_policy_document_family_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_blob_id", "tenant_id"],
            ["policy_source_blob.id", "policy_source_blob.tenant_id"],
            name="fk_policy_document_source_blob_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_document_created_by_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["published_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_document_published_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "expiry_date IS NULL OR expiry_date > effective_date",
            name="effective_interval",
        ),
        CheckConstraint(
            "(status = 'legacy_unpublished' AND family_id IS NULL "
            "AND source_blob_id IS NULL AND content_sha256 IS NULL "
            "AND mime_type IS NULL AND size_bytes IS NULL "
            "AND extracted_text_sha256 IS NULL AND parser_version IS NULL "
            "AND chunker_version IS NULL AND created_by IS NULL "
            "AND published_by IS NULL AND published_at IS NULL "
            "AND failure_code IS NULL) OR "
            "(status <> 'legacy_unpublished' AND family_id IS NOT NULL "
            "AND source_blob_id IS NOT NULL AND content_sha256 IS NOT NULL "
            "AND char_length(content_sha256) = 64 AND mime_type IS NOT NULL "
            "AND size_bytes > 0 AND extracted_text_sha256 IS NOT NULL "
            "AND char_length(extracted_text_sha256) = 64 "
            "AND parser_version IS NOT NULL AND chunker_version IS NOT NULL "
            "AND created_by IS NOT NULL)",
            name="legacy_or_complete",
        ),
        CheckConstraint(
            "(status = 'published' AND published_by IS NOT NULL "
            "AND published_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND published_by IS NULL "
            "AND published_at IS NULL AND failure_code IS NOT NULL) OR "
            "(status IN ('legacy_unpublished', 'draft', 'indexing') "
            "AND published_by IS NULL AND published_at IS NULL "
            "AND failure_code IS NULL)",
            name="lifecycle_fields",
        ),
        ExcludeConstraint(
            (column("tenant_id"), "="),
            (column("family_id"), "="),
            (
                func.daterange(
                    column("effective_date"),
                    func.coalesce(column("expiry_date"), text("'infinity'::date")),
                    "[)",
                ),
                "&&",
            ),
            name="ex_policy_document_published_effective_interval",
            using="gist",
            where=text("status = 'published'"),
        ),
    )


class PolicyClause(Base, TenantScopedMixin, TimestampMixin):
    """Citation atom stored verbatim in PostgreSQL."""

    __tablename__ = "policy_clause"
    __table_args__ = (
        Index("ix_policy_clause_document_id", "document_id"),
        Index("ix_policy_clause_family_id", "family_id"),
        UniqueConstraint("document_id", "clause_no"),
        UniqueConstraint("document_id", "ordinal"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_clause_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "document_id",
            name="uq_policy_clause_id_tenant_id_document_id",
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_clause_document_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id", "family_id"],
            ["policy_document.id", "policy_document.tenant_id", "policy_document.family_id"],
            name="fk_policy_clause_document_tenant_family",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(family_id IS NULL AND ordinal IS NULL AND text_sha256 IS NULL "
            "AND source_locator_json IS NULL AND source_start IS NULL "
            "AND source_end IS NULL) OR "
            "(family_id IS NOT NULL AND ordinal > 0 AND text_sha256 IS NOT NULL "
            "AND char_length(text_sha256) = 64 AND source_locator_json IS NOT NULL "
            "AND ((source_start IS NULL AND source_end IS NULL) OR "
            "(source_start >= 0 AND source_end > source_start)))",
            name="legacy_or_complete",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    family_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    clause_no: Mapped[str] = mapped_column(String(64), nullable=False)
    hierarchy_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_locator_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PolicyChunk(Base, TenantScopedMixin, TimestampMixin):
    """Deterministic contiguous retrieval slice within one clause."""

    __tablename__ = "policy_chunk"
    __table_args__ = (
        UniqueConstraint("clause_id", "chunk_no"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_chunk_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "document_id",
            name="uq_policy_chunk_id_tenant_document",
        ),
        UniqueConstraint(
            "id",
            "tenant_id",
            "document_id",
            "clause_id",
            name="uq_policy_chunk_id_tenant_document_clause",
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_chunk_document_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["clause_id", "tenant_id", "document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_policy_chunk_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("chunk_no > 0", name="chunk_no_positive"),
        CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="offsets_valid",
        ),
        CheckConstraint("char_length(text) > 0", name="text_nonempty"),
        CheckConstraint("char_length(text_sha256) = 64", name="text_sha256_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    clause_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(128), nullable=False)


class PolicyIndexGeneration(Base, TenantScopedMixin, TimestampMixin):
    """Frozen local embedding/rerank/index provenance for one generation."""

    __tablename__ = "policy_index_generation"
    __table_args__ = (
        UniqueConstraint("tenant_id", "generation"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_index_generation_id_tenant_id"),
        Index(
            "uq_policy_index_generation_one_active_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_index_generation_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("generation > 0", name="generation_positive"),
        CheckConstraint("manifest_revision > 0", name="manifest_revision_positive"),
        CheckConstraint("vector_size > 0", name="vector_size_positive"),
        CheckConstraint(
            "expected_point_count >= 0 AND completed_point_count >= 0 "
            "AND completed_point_count <= expected_point_count",
            name="counts_valid",
        ),
        CheckConstraint(
            "status <> 'active' OR completed_point_count = expected_point_count",
            name="active_complete",
        ),
        CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL)",
            name="failure_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    collection_name: Mapped[str] = mapped_column(String(255), nullable=False)
    collection_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    vector_size: Mapped[int] = mapped_column(Integer, nullable=False)
    distance: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model_family: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    rerank_model_family: Mapped[str] = mapped_column(String(255), nullable=False)
    rerank_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    rerank_model_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    rerank_model_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(128), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[PolicyIndexGenerationStatus] = mapped_column(
        str_enum(PolicyIndexGenerationStatus, "policy_index_generation_status_enum"),
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)


class PolicyDocumentIndex(Base, TenantScopedMixin, TimestampMixin):
    """Document completeness manifest within an index generation."""

    __tablename__ = "policy_document_index"
    __table_args__ = (
        UniqueConstraint("document_id", "index_generation_id"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_document_index_id_tenant_id"),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_document_index_document_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["index_generation_id", "tenant_id"],
            ["policy_index_generation.id", "policy_index_generation.tenant_id"],
            name="fk_policy_document_index_generation_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "expected_point_count >= 0 AND completed_point_count >= 0 "
            "AND completed_point_count <= expected_point_count",
            name="counts_valid",
        ),
        CheckConstraint(
            "status <> 'completed' OR completed_point_count = expected_point_count",
            name="completed_counts_match",
        ),
        CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL)",
            name="failure_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    index_generation_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    status: Mapped[PolicyDocumentIndexStatus] = mapped_column(
        str_enum(PolicyDocumentIndexStatus, "policy_document_index_status_enum"),
        nullable=False,
    )
    expected_point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_point_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class PolicyIndexJob(Base, TenantScopedMixin, TimestampMixin):
    """PG-to-Qdrant transactional outbox without policy text or secrets."""

    __tablename__ = "policy_index_job"
    __table_args__ = (
        UniqueConstraint("chunk_id", "index_generation_id", "operation"),
        UniqueConstraint("id", "tenant_id", name="uq_policy_index_job_id_tenant_id"),
        Index(
            "ix_policy_index_job_claim",
            "tenant_id",
            "status",
            "available_at",
            "lease_expires_at",
        ),
        ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_index_job_document_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["chunk_id", "tenant_id", "document_id"],
            ["policy_chunk.id", "policy_chunk.tenant_id", "policy_chunk.document_id"],
            name="fk_policy_index_job_chunk_tenant_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["index_generation_id", "tenant_id"],
            ["policy_index_generation.id", "policy_index_generation.tenant_id"],
            name="fk_policy_index_job_generation_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND attempt_limit > 0 AND attempt_count <= attempt_limit",
            name="attempts_valid",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="lease_consistent",
        ),
        CheckConstraint(
            "(status = 'failed' AND last_failure_code IS NOT NULL) OR (status <> 'failed')",
            name="failure_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    chunk_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    index_generation_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    operation: Mapped[PolicyIndexOperation] = mapped_column(
        str_enum(PolicyIndexOperation, "policy_index_operation_enum"), nullable=False
    )
    status: Mapped[PolicyIndexJobStatus] = mapped_column(
        str_enum(PolicyIndexJobStatus, "policy_index_job_status_enum"), nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RulePolicyBinding(Base, TenantScopedMixin, TimestampMixin):
    """Configurator-confirmed exact rule-to-clause quote binding."""

    __tablename__ = "rule_policy_binding"
    __table_args__ = (
        UniqueConstraint("tenant_id", "binding_fingerprint"),
        UniqueConstraint("rule_config_id", "policy_document_id", "citation_order"),
        UniqueConstraint("id", "tenant_id", name="uq_rule_policy_binding_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "policy_family_id",
            "policy_document_id",
            "policy_clause_id",
            name="uq_rule_policy_binding_citation_identity",
        ),
        ForeignKeyConstraint(
            ["rule_config_id", "tenant_id"],
            ["rule_config.id", "rule_config.tenant_id"],
            name="fk_rule_policy_binding_rule_config_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_family_id", "tenant_id"],
            ["policy_family.id", "policy_family.tenant_id"],
            name="fk_rule_policy_binding_family_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_document_id", "tenant_id", "policy_family_id"],
            ["policy_document.id", "policy_document.tenant_id", "policy_document.family_id"],
            name="fk_rule_policy_binding_document_tenant_family",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["policy_clause_id", "tenant_id", "policy_document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_rule_policy_binding_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_rule_policy_binding_created_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("citation_order BETWEEN 1 AND 3", name="citation_order_range"),
        CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start",
            name="quote_offsets_valid",
        ),
        CheckConstraint("char_length(quote) > 0 AND quote ~ '\\S'", name="quote_nonblank"),
        CheckConstraint("char_length(clause_text_sha256) = 64", name="clause_hash_length"),
        CheckConstraint("char_length(quote_sha256) = 64", name="quote_hash_length"),
        CheckConstraint("char_length(binding_fingerprint) = 64", name="fingerprint_length"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    rule_config_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    policy_family_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    policy_document_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    policy_clause_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    quote_start: Mapped[int] = mapped_column(Integer, nullable=False)
    quote_end: Mapped[int] = mapped_column(Integer, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    clause_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    citation_order: Mapped[int] = mapped_column(Integer, nullable=False)
    binding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
