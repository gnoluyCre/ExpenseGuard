"""F4 policy provenance, index outbox, bindings, and report snapshots.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28

Legacy policy rows remain intact and explicitly unavailable to F4. No family,
hash, parser, chunk, or quote offsets are guessed during the backfill.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _tenant_fk(table_name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenant.id"],
        name=op.f(f"fk_{table_name}_tenant_id_tenant"),
        ondelete="RESTRICT",
    )


def _preflight_legacy_policy_tenants() -> None:
    bind = op.get_bind()
    clause_mismatch = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM policy_clause AS clause
                JOIN policy_document AS document ON document.id = clause.document_id
                WHERE clause.tenant_id <> document.tenant_id
            )
            """
        )
    )
    finding_mismatch = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM finding
                JOIN policy_clause AS clause ON clause.id = finding.clause_id
                WHERE finding.clause_id IS NOT NULL
                  AND finding.tenant_id <> clause.tenant_id
            )
            """
        )
    )
    if clause_mismatch or finding_mismatch:
        raise RuntimeError(
            "cannot upgrade 0005: legacy policy tenant mismatch detected; "
            "repair the source data before applying composite tenant foreign keys"
        )


def upgrade() -> None:
    """Install the complete CP-F4.1 persistence contract."""
    _preflight_legacy_policy_tenants()
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "policy_family",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(512), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_family_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_family"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_family")),
        sa.UniqueConstraint(
            "tenant_id", "stable_key", name=op.f("uq_policy_family_tenant_id_stable_key")
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_family_id_tenant_id"),
        sa.CheckConstraint(
            "char_length(stable_key) BETWEEN 1 AND 128",
            name=op.f("ck_policy_family_stable_key_length"),
        ),
    )
    op.create_index(op.f("ix_policy_family_tenant_id"), "policy_family", ["tenant_id"])

    op.create_table(
        "policy_source_blob",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_source_blob_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_source_blob"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_source_blob")),
        sa.UniqueConstraint(
            "tenant_id",
            "content_sha256",
            name=op.f("uq_policy_source_blob_tenant_id_content_sha256"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "storage_key",
            name=op.f("uq_policy_source_blob_tenant_id_storage_key"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_source_blob_id_tenant_id"),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_policy_source_blob_size_positive")),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name=op.f("ck_policy_source_blob_content_sha256_length"),
        ),
    )
    op.create_index(op.f("ix_policy_source_blob_tenant_id"), "policy_source_blob", ["tenant_id"])

    document_columns: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
        ("family_id", sa.Uuid()),
        ("source_blob_id", sa.Uuid()),
        ("content_sha256", sa.String(64)),
        ("mime_type", sa.String(255)),
        ("size_bytes", sa.Integer()),
        ("extracted_text_sha256", sa.String(64)),
        ("parser_version", sa.String(128)),
        ("chunker_version", sa.String(128)),
        ("created_by", sa.Uuid()),
        ("published_by", sa.Uuid()),
        ("published_at", sa.DateTime(timezone=True)),
        ("failure_code", sa.String(128)),
    )
    for name, type_ in document_columns:
        op.add_column("policy_document", sa.Column(name, type_, nullable=True))
    op.add_column(
        "policy_document",
        sa.Column(
            "status",
            sa.Enum(
                "legacy_unpublished",
                "draft",
                "indexing",
                "published",
                "failed",
                name="policy_document_status_enum",
                native_enum=False,
            ),
            server_default="legacy_unpublished",
            nullable=False,
        ),
    )
    op.execute("UPDATE policy_document SET status = 'legacy_unpublished'")
    op.alter_column("policy_document", "status", server_default=None)
    op.create_unique_constraint(
        "uq_policy_document_id_tenant_id", "policy_document", ["id", "tenant_id"]
    )
    op.create_unique_constraint(
        "uq_policy_document_id_tenant_id_family_id",
        "policy_document",
        ["id", "tenant_id", "family_id"],
    )
    op.create_unique_constraint(
        op.f("uq_policy_document_tenant_id_family_id_version"),
        "policy_document",
        ["tenant_id", "family_id", "version"],
    )
    op.create_foreign_key(
        "fk_policy_document_family_tenant",
        "policy_document",
        "policy_family",
        ["family_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_document_source_blob_tenant",
        "policy_document",
        "policy_source_blob",
        ["source_blob_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_document_created_by_tenant",
        "policy_document",
        "app_user",
        ["created_by", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_document_published_by_tenant",
        "policy_document",
        "app_user",
        ["published_by", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_policy_document_effective_interval"),
        "policy_document",
        "expiry_date IS NULL OR expiry_date > effective_date",
    )
    op.create_check_constraint(
        op.f("ck_policy_document_legacy_or_complete"),
        "policy_document",
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
    )
    op.create_check_constraint(
        op.f("ck_policy_document_lifecycle_fields"),
        "policy_document",
        "(status = 'published' AND published_by IS NOT NULL "
        "AND published_at IS NOT NULL AND failure_code IS NULL) OR "
        "(status = 'failed' AND published_by IS NULL "
        "AND published_at IS NULL AND failure_code IS NOT NULL) OR "
        "(status IN ('legacy_unpublished', 'draft', 'indexing') "
        "AND published_by IS NULL AND published_at IS NULL "
        "AND failure_code IS NULL)",
    )
    op.create_index("ix_policy_document_family_id", "policy_document", ["family_id"])
    op.create_index("ix_policy_document_source_blob_id", "policy_document", ["source_blob_id"])
    op.create_exclude_constraint(
        "ex_policy_document_published_effective_interval",
        "policy_document",
        ("tenant_id", "="),
        ("family_id", "="),
        (
            sa.func.daterange(
                sa.column("effective_date"),
                sa.func.coalesce(sa.column("expiry_date"), sa.text("'infinity'::date")),
                "[)",
            ),
            "&&",
        ),
        where=sa.text("status = 'published'"),
        using="gist",
    )

    clause_columns: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
        ("family_id", sa.Uuid()),
        ("ordinal", sa.Integer()),
        ("text_sha256", sa.String(64)),
        ("source_locator_json", postgresql.JSONB(astext_type=sa.Text())),
        ("source_start", sa.Integer()),
        ("source_end", sa.Integer()),
    )
    for name, type_ in clause_columns:
        op.add_column("policy_clause", sa.Column(name, type_, nullable=True))
    op.drop_constraint(
        "fk_policy_clause_document_id_policy_document", "policy_clause", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_policy_clause_document_tenant",
        "policy_clause",
        "policy_document",
        ["document_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_clause_document_tenant_family",
        "policy_clause",
        "policy_document",
        ["document_id", "tenant_id", "family_id"],
        ["id", "tenant_id", "family_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_policy_clause_id_tenant_id", "policy_clause", ["id", "tenant_id"]
    )
    op.create_unique_constraint(
        "uq_policy_clause_id_tenant_id_document_id",
        "policy_clause",
        ["id", "tenant_id", "document_id"],
    )
    op.create_unique_constraint(
        op.f("uq_policy_clause_document_id_ordinal"),
        "policy_clause",
        ["document_id", "ordinal"],
    )
    op.create_check_constraint(
        op.f("ck_policy_clause_legacy_or_complete"),
        "policy_clause",
        "(family_id IS NULL AND ordinal IS NULL AND text_sha256 IS NULL "
        "AND source_locator_json IS NULL AND source_start IS NULL "
        "AND source_end IS NULL) OR "
        "(family_id IS NOT NULL AND ordinal > 0 AND text_sha256 IS NOT NULL "
        "AND char_length(text_sha256) = 64 AND source_locator_json IS NOT NULL "
        "AND ((source_start IS NULL AND source_end IS NULL) OR "
        "(source_start >= 0 AND source_end > source_start)))",
    )
    op.create_index("ix_policy_clause_family_id", "policy_clause", ["family_id"])

    op.drop_constraint("fk_finding_clause_id_policy_clause", "finding", type_="foreignkey")
    op.create_foreign_key(
        "fk_finding_clause_tenant",
        "finding",
        "policy_clause",
        ["clause_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_finding_id_tenant_id_file_version_id",
        "finding",
        ["id", "tenant_id", "file_version_id"],
    )
    op.create_unique_constraint(
        "uq_validation_run_id_tenant_id_file_version_id",
        "validation_run",
        ["id", "tenant_id", "file_version_id"],
    )

    op.create_table(
        "policy_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("clause_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("chunker_version", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_chunk_document_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["clause_id", "tenant_id", "document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_policy_chunk_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_chunk"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_chunk")),
        sa.UniqueConstraint(
            "clause_id", "chunk_no", name=op.f("uq_policy_chunk_clause_id_chunk_no")
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_chunk_id_tenant_id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "document_id",
            name="uq_policy_chunk_id_tenant_document",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "document_id",
            "clause_id",
            name="uq_policy_chunk_id_tenant_document_clause",
        ),
        sa.CheckConstraint("chunk_no > 0", name=op.f("ck_policy_chunk_chunk_no_positive")),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name=op.f("ck_policy_chunk_offsets_valid"),
        ),
        sa.CheckConstraint("char_length(text) > 0", name=op.f("ck_policy_chunk_text_nonempty")),
        sa.CheckConstraint(
            "char_length(text_sha256) = 64",
            name=op.f("ck_policy_chunk_text_sha256_length"),
        ),
    )
    op.create_index(op.f("ix_policy_chunk_tenant_id"), "policy_chunk", ["tenant_id"])
    op.create_index(op.f("ix_policy_chunk_document_id"), "policy_chunk", ["document_id"])
    op.create_index(op.f("ix_policy_chunk_clause_id"), "policy_chunk", ["clause_id"])

    op.create_table(
        "policy_index_generation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("manifest_revision", sa.Integer(), nullable=False),
        sa.Column("collection_name", sa.String(255), nullable=False),
        sa.Column("collection_alias", sa.String(255), nullable=False),
        sa.Column("vector_size", sa.Integer(), nullable=False),
        sa.Column("distance", sa.String(32), nullable=False),
        sa.Column("embedding_model_family", sa.String(255), nullable=False),
        sa.Column("embedding_model_id", sa.String(255), nullable=False),
        sa.Column("embedding_model_revision", sa.String(255), nullable=False),
        sa.Column("embedding_model_fingerprint", sa.String(64), nullable=False),
        sa.Column("rerank_model_family", sa.String(255), nullable=False),
        sa.Column("rerank_model_id", sa.String(255), nullable=False),
        sa.Column("rerank_model_revision", sa.String(255), nullable=False),
        sa.Column("rerank_model_fingerprint", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(128), nullable=False),
        sa.Column("chunker_version", sa.String(128), nullable=False),
        sa.Column("source_manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("expected_point_count", sa.Integer(), nullable=False),
        sa.Column("completed_point_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "building",
                "active",
                "failed",
                "retired",
                name="policy_index_generation_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_policy_index_generation_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_index_generation"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_index_generation")),
        sa.UniqueConstraint(
            "tenant_id",
            "generation",
            name=op.f("uq_policy_index_generation_tenant_id_generation"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_index_generation_id_tenant_id"),
        sa.CheckConstraint(
            "generation > 0", name=op.f("ck_policy_index_generation_generation_positive")
        ),
        sa.CheckConstraint(
            "manifest_revision > 0",
            name=op.f("ck_policy_index_generation_manifest_revision_positive"),
        ),
        sa.CheckConstraint(
            "vector_size > 0", name=op.f("ck_policy_index_generation_vector_size_positive")
        ),
        sa.CheckConstraint(
            "expected_point_count >= 0 AND completed_point_count >= 0 "
            "AND completed_point_count <= expected_point_count",
            name=op.f("ck_policy_index_generation_counts_valid"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR completed_point_count = expected_point_count",
            name=op.f("ck_policy_index_generation_active_complete"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL)",
            name=op.f("ck_policy_index_generation_failure_consistent"),
        ),
    )
    op.create_index(
        op.f("ix_policy_index_generation_tenant_id"), "policy_index_generation", ["tenant_id"]
    )
    op.create_index(
        "uq_policy_index_generation_one_active_per_tenant",
        "policy_index_generation",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "policy_document_index",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("index_generation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "indexing",
                "completed",
                "failed",
                name="policy_document_index_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("expected_point_count", sa.Integer(), nullable=False),
        sa.Column("completed_point_count", sa.Integer(), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_document_index_document_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_generation_id", "tenant_id"],
            ["policy_index_generation.id", "policy_index_generation.tenant_id"],
            name="fk_policy_document_index_generation_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_document_index"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_document_index")),
        sa.UniqueConstraint(
            "document_id",
            "index_generation_id",
            name=op.f("uq_policy_document_index_document_id_index_generation_id"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_document_index_id_tenant_id"),
        sa.CheckConstraint(
            "expected_point_count >= 0 AND completed_point_count >= 0 "
            "AND completed_point_count <= expected_point_count",
            name=op.f("ck_policy_document_index_counts_valid"),
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR completed_point_count = expected_point_count",
            name=op.f("ck_policy_document_index_completed_counts_match"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND failure_code IS NOT NULL) OR "
            "(status <> 'failed' AND failure_code IS NULL)",
            name=op.f("ck_policy_document_index_failure_consistent"),
        ),
    )
    op.create_index(
        op.f("ix_policy_document_index_tenant_id"), "policy_document_index", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_policy_document_index_document_id"), "policy_document_index", ["document_id"]
    )
    op.create_index(
        op.f("ix_policy_document_index_index_generation_id"),
        "policy_document_index",
        ["index_generation_id"],
    )

    op.create_table(
        "policy_index_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("index_generation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "operation",
            sa.Enum(
                "upsert",
                "delete",
                name="policy_index_operation_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                name="policy_index_job_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("attempt_limit", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(128), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["policy_document.id", "policy_document.tenant_id"],
            name="fk_policy_index_job_document_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id", "tenant_id", "document_id"],
            ["policy_chunk.id", "policy_chunk.tenant_id", "policy_chunk.document_id"],
            name="fk_policy_index_job_chunk_tenant_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_generation_id", "tenant_id"],
            ["policy_index_generation.id", "policy_index_generation.tenant_id"],
            name="fk_policy_index_job_generation_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("policy_index_job"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_index_job")),
        sa.UniqueConstraint(
            "chunk_id",
            "index_generation_id",
            "operation",
            name=op.f("uq_policy_index_job_chunk_id_index_generation_id_operation"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_policy_index_job_id_tenant_id"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_limit > 0 AND attempt_count <= attempt_limit",
            name=op.f("ck_policy_index_job_attempts_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_owner IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND lease_owner IS NULL "
            "AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name=op.f("ck_policy_index_job_lease_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND last_failure_code IS NOT NULL) OR (status <> 'failed')",
            name=op.f("ck_policy_index_job_failure_consistent"),
        ),
    )
    for column_name in ("tenant_id", "document_id", "chunk_id", "index_generation_id"):
        op.create_index(
            op.f(f"ix_policy_index_job_{column_name}"), "policy_index_job", [column_name]
        )
    op.create_index(
        "ix_policy_index_job_claim",
        "policy_index_job",
        ["tenant_id", "status", "available_at", "lease_expires_at"],
    )

    op.create_table(
        "rule_policy_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_config_id", sa.Uuid(), nullable=False),
        sa.Column("policy_family_id", sa.Uuid(), nullable=False),
        sa.Column("policy_document_id", sa.Uuid(), nullable=False),
        sa.Column("policy_clause_id", sa.Uuid(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_sha256", sa.String(64), nullable=False),
        sa.Column("clause_text_sha256", sa.String(64), nullable=False),
        sa.Column("citation_order", sa.Integer(), nullable=False),
        sa.Column("binding_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["rule_config_id", "tenant_id"],
            ["rule_config.id", "rule_config.tenant_id"],
            name="fk_rule_policy_binding_rule_config_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_family_id", "tenant_id"],
            ["policy_family.id", "policy_family.tenant_id"],
            name="fk_rule_policy_binding_family_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_document_id", "tenant_id", "policy_family_id"],
            ["policy_document.id", "policy_document.tenant_id", "policy_document.family_id"],
            name="fk_rule_policy_binding_document_tenant_family",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_clause_id", "tenant_id", "policy_document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_rule_policy_binding_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_rule_policy_binding_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("rule_policy_binding"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_policy_binding")),
        sa.UniqueConstraint(
            "tenant_id",
            "binding_fingerprint",
            name=op.f("uq_rule_policy_binding_tenant_id_binding_fingerprint"),
        ),
        sa.UniqueConstraint(
            "rule_config_id",
            "policy_document_id",
            "citation_order",
            name=op.f("uq_rule_policy_binding_rule_config_id_policy_document_id_citation_order"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_rule_policy_binding_id_tenant_id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "policy_family_id",
            "policy_document_id",
            "policy_clause_id",
            name="uq_rule_policy_binding_citation_identity",
        ),
        sa.CheckConstraint(
            "citation_order BETWEEN 1 AND 3",
            name=op.f("ck_rule_policy_binding_citation_order_range"),
        ),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start",
            name=op.f("ck_rule_policy_binding_quote_offsets_valid"),
        ),
        sa.CheckConstraint(
            "char_length(quote) > 0 AND quote ~ '\\S'",
            name=op.f("ck_rule_policy_binding_quote_nonblank"),
        ),
        sa.CheckConstraint(
            "char_length(clause_text_sha256) = 64",
            name=op.f("ck_rule_policy_binding_clause_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(quote_sha256) = 64",
            name=op.f("ck_rule_policy_binding_quote_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(binding_fingerprint) = 64",
            name=op.f("ck_rule_policy_binding_fingerprint_length"),
        ),
    )
    for column_name in (
        "tenant_id",
        "rule_config_id",
        "policy_document_id",
        "policy_clause_id",
    ):
        op.create_index(
            op.f(f"ix_rule_policy_binding_{column_name}"),
            "rule_policy_binding",
            [column_name],
        )

    _create_report_tables()
    _create_immutability_triggers()


def _create_report_tables() -> None:
    """Create report snapshot and artifact tables after policy bindings."""
    op.create_table(
        "report_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "in_progress",
                "completed",
                name="report_run_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("report_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("ruleset_fingerprint", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("attention_mapping_version", sa.String(64), nullable=False),
        sa.Column("policy_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("binding_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("stored_row_count", sa.Integer(), nullable=False),
        sa.Column("validated_row_count", sa.Integer(), nullable=False),
        sa.Column("flagged_row_count", sa.Integer(), nullable=False),
        sa.Column("manual_review_row_count", sa.Integer(), nullable=False),
        sa.Column("passed_row_count", sa.Integer(), nullable=False),
        sa.Column("parse_error_row_count", sa.Integer(), nullable=False),
        sa.Column("report_item_count", sa.Integer(), nullable=False),
        sa.Column("verified_citation_count", sa.Integer(), nullable=False),
        sa.Column("unavailable_citation_count", sa.Integer(), nullable=False),
        sa.Column("high_attention_row_count", sa.Integer(), nullable=False),
        sa.Column("manual_attention_row_count", sa.Integer(), nullable=False),
        sa.Column("cleared_row_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_run_file_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id", "tenant_id", "file_version_id"],
            ["validation_run.id", "validation_run.tenant_id", "validation_run.file_version_id"],
            name="fk_report_run_validation_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_report_run_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_report_run_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("report_run"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_run")),
        sa.UniqueConstraint("file_version_id", name=op.f("uq_report_run_file_version_id")),
        sa.UniqueConstraint(
            "tenant_id",
            "report_fingerprint",
            name=op.f("uq_report_run_tenant_id_report_fingerprint"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name=op.f("uq_report_run_tenant_id_idempotency_key_hash"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_report_run_id_tenant_id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_report_run_id_tenant_id_file_version_id",
        ),
        sa.CheckConstraint(
            "char_length(report_fingerprint) = 64",
            name=op.f("ck_report_run_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "char_length(source_content_sha256) = 64",
            name=op.f("ck_report_run_source_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(ruleset_fingerprint) = 64",
            name=op.f("ck_report_run_ruleset_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_report_run_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_report_run_request_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "stored_row_count >= 0 AND validated_row_count >= 0 "
            "AND flagged_row_count >= 0 AND manual_review_row_count >= 0 "
            "AND passed_row_count >= 0 AND parse_error_row_count >= 0 "
            "AND report_item_count >= 0 AND verified_citation_count >= 0 "
            "AND unavailable_citation_count >= 0 "
            "AND high_attention_row_count >= 0 "
            "AND manual_attention_row_count >= 0 AND cleared_row_count >= 0",
            name=op.f("ck_report_run_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "stored_row_count = validated_row_count + parse_error_row_count",
            name=op.f("ck_report_run_stored_count_consistent"),
        ),
        sa.CheckConstraint(
            "validated_row_count = flagged_row_count + manual_review_row_count + passed_row_count",
            name=op.f("ck_report_run_validated_count_consistent"),
        ),
        sa.CheckConstraint(
            "manual_attention_row_count = manual_review_row_count + parse_error_row_count",
            name=op.f("ck_report_run_manual_attention_count_consistent"),
        ),
        sa.CheckConstraint(
            "high_attention_row_count = flagged_row_count AND cleared_row_count = passed_row_count",
            name=op.f("ck_report_run_attention_counts_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name=op.f("ck_report_run_completion_consistent"),
        ),
    )
    for column_name in ("tenant_id", "file_version_id", "validation_run_id"):
        op.create_index(op.f(f"ix_report_run_{column_name}"), "report_run", [column_name])

    op.create_table(
        "report_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("rule_config_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.String(128), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=True),
        sa.Column("source_outcome", sa.String(64), nullable=False),
        sa.Column("source_verdict", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("reasoning_snapshot", sa.Text(), nullable=True),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "attention_group",
            sa.Enum(
                "high_attention",
                "manual_attention",
                "cleared",
                name="report_attention_group_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "citation_status",
            sa.Enum(
                "verified",
                "unavailable",
                name="report_citation_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("requires_manual_citation", sa.Boolean(), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_item_report_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id", "tenant_id", "file_version_id"],
            ["finding.id", "finding.tenant_id", "finding.file_version_id"],
            name="fk_report_item_finding_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_item_file_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rule_config_id", "tenant_id"],
            ["rule_config.id", "rule_config.tenant_id"],
            name="fk_report_item_rule_config_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("report_item"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_item")),
        sa.UniqueConstraint(
            "report_run_id",
            "finding_id",
            name=op.f("uq_report_item_report_run_id_finding_id"),
        ),
        sa.UniqueConstraint("id", "tenant_id", name="uq_report_item_id_tenant_id"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "report_run_id",
            name="uq_report_item_id_tenant_id_report_run_id",
        ),
        sa.CheckConstraint(
            "source_outcome IN ('passed', 'flagged', 'unavailable', 'exempted')",
            name=op.f("ck_report_item_source_outcome_values"),
        ),
        sa.CheckConstraint(
            "source_verdict IN ('flagged', 'manual_review', 'passed')",
            name=op.f("ck_report_item_source_verdict_values"),
        ),
        sa.CheckConstraint(
            "attention_group IN ('high_attention', 'manual_attention', 'cleared')",
            name=op.f("ck_report_item_attention_group_values"),
        ),
        sa.CheckConstraint(
            "citation_status IN ('verified', 'unavailable')",
            name=op.f("ck_report_item_citation_status_values"),
        ),
        sa.CheckConstraint(
            "(citation_status = 'verified' AND requires_manual_citation = false) OR "
            "(citation_status = 'unavailable' AND requires_manual_citation = true)",
            name=op.f("ck_report_item_citation_manual_consistent"),
        ),
        sa.CheckConstraint(
            "char_length(source_content_sha256) = 64",
            name=op.f("ck_report_item_source_hash_length"),
        ),
    )
    op.create_index(op.f("ix_report_item_tenant_id"), "report_item", ["tenant_id"])
    op.create_index(op.f("ix_report_item_report_run_id"), "report_item", ["report_run_id"])
    op.create_index(
        "ix_report_item_report_attention_order",
        "report_item",
        ["report_run_id", "attention_group", "row_no", "rule_id", "rule_version", "finding_id"],
    )

    op.create_table(
        "report_parse_error",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=False),
        sa.Column("column_name", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_parse_error_report_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_parse_error_file_version_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("report_parse_error"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_parse_error")),
        sa.UniqueConstraint(
            "report_run_id",
            "row_no",
            "error_code",
            "column_name",
            name=op.f("uq_report_parse_error_report_run_id_row_no_error_code_column_name"),
        ),
        sa.CheckConstraint(
            "char_length(source_content_sha256) = 64",
            name=op.f("ck_report_parse_error_source_hash_length"),
        ),
    )
    op.create_index(op.f("ix_report_parse_error_tenant_id"), "report_parse_error", ["tenant_id"])
    op.create_index(
        op.f("ix_report_parse_error_report_run_id"), "report_parse_error", ["report_run_id"]
    )
    op.create_index(
        "ix_report_parse_error_report_order",
        "report_parse_error",
        ["report_run_id", "row_no", "error_code", "column_name"],
    )

    op.create_table(
        "report_citation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("report_item_id", sa.Uuid(), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("policy_family_id", sa.Uuid(), nullable=False),
        sa.Column("policy_document_id", sa.Uuid(), nullable=False),
        sa.Column("policy_clause_id", sa.Uuid(), nullable=False),
        sa.Column("family_stable_key", sa.String(128), nullable=False),
        sa.Column("document_title", sa.String(512), nullable=False),
        sa.Column("document_version", sa.String(64), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("document_content_sha256", sa.String(64), nullable=False),
        sa.Column("clause_no", sa.String(64), nullable=False),
        sa.Column("hierarchy_path", sa.String(1024), nullable=True),
        sa.Column("clause_text", sa.Text(), nullable=False),
        sa.Column("clause_text_sha256", sa.String(64), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=False),
        sa.Column("quote_end", sa.Integer(), nullable=False),
        sa.Column("quote_sha256", sa.String(64), nullable=False),
        sa.Column("verification_status", sa.String(32), nullable=False),
        sa.Column("citation_order", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_item_id", "tenant_id", "report_run_id"],
            ["report_item.id", "report_item.tenant_id", "report_item.report_run_id"],
            name="fk_report_citation_item_tenant_report",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["policy_family_id", "tenant_id"],
            ["policy_family.id", "policy_family.tenant_id"],
            name="fk_report_citation_family_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_document_id", "tenant_id", "policy_family_id"],
            ["policy_document.id", "policy_document.tenant_id", "policy_document.family_id"],
            name="fk_report_citation_document_tenant_family",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["policy_clause_id", "tenant_id", "policy_document_id"],
            ["policy_clause.id", "policy_clause.tenant_id", "policy_clause.document_id"],
            name="fk_report_citation_clause_tenant_document",
            ondelete="RESTRICT",
        ),
        _tenant_fk("report_citation"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_citation")),
        sa.UniqueConstraint(
            "report_item_id",
            "citation_order",
            name=op.f("uq_report_citation_report_item_id_citation_order"),
        ),
        sa.CheckConstraint(
            "citation_order BETWEEN 1 AND 3",
            name=op.f("ck_report_citation_citation_order_range"),
        ),
        sa.CheckConstraint(
            "quote_start >= 0 AND quote_end > quote_start",
            name=op.f("ck_report_citation_quote_offsets_valid"),
        ),
        sa.CheckConstraint(
            "char_length(quote) > 0 AND quote ~ '\\S'",
            name=op.f("ck_report_citation_quote_nonblank"),
        ),
        sa.CheckConstraint(
            "char_length(document_content_sha256) = 64",
            name=op.f("ck_report_citation_document_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(clause_text_sha256) = 64",
            name=op.f("ck_report_citation_clause_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(quote_sha256) = 64",
            name=op.f("ck_report_citation_quote_hash_length"),
        ),
        sa.CheckConstraint(
            "verification_status = 'verified_exact'",
            name=op.f("ck_report_citation_verified_exact_only"),
        ),
    )
    op.create_index(op.f("ix_report_citation_tenant_id"), "report_citation", ["tenant_id"])
    op.create_index(op.f("ix_report_citation_report_run_id"), "report_citation", ["report_run_id"])
    op.create_index(
        op.f("ix_report_citation_report_item_id"), "report_citation", ["report_item_id"]
    )

    op.create_table(
        "report_export",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "in_progress",
                "completed",
                "failed",
                name="report_export_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("artifact_storage_key", sa.String(1024), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(128), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id"],
            ["report_run.id", "report_run.tenant_id"],
            name="fk_report_export_report_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_report_export_created_by_tenant",
            ondelete="RESTRICT",
        ),
        _tenant_fk("report_export"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_export")),
        sa.UniqueConstraint(
            "report_run_id",
            "format",
            "template_version",
            name=op.f("uq_report_export_report_run_id_format_template_version"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name=op.f("uq_report_export_tenant_id_idempotency_key_hash"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "artifact_storage_key",
            name=op.f("uq_report_export_tenant_id_artifact_storage_key"),
        ),
        sa.CheckConstraint("format = 'xlsx'", name=op.f("ck_report_export_xlsx_only")),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_report_export_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_report_export_request_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND artifact_storage_key IS NULL "
            "AND artifact_sha256 IS NULL AND size_bytes IS NULL "
            "AND completed_at IS NULL AND failure_code IS NULL) OR "
            "(status = 'completed' AND artifact_storage_key IS NOT NULL "
            "AND artifact_sha256 IS NOT NULL AND char_length(artifact_sha256) = 64 "
            "AND size_bytes > 0 AND completed_at IS NOT NULL AND failure_code IS NULL) OR "
            "(status = 'failed' AND artifact_storage_key IS NULL "
            "AND artifact_sha256 IS NULL AND size_bytes IS NULL "
            "AND completed_at IS NOT NULL AND failure_code IS NOT NULL)",
            name=op.f("ck_report_export_lifecycle_fields"),
        ),
    )
    op.create_index(op.f("ix_report_export_tenant_id"), "report_export", ["tenant_id"])
    op.create_index(op.f("ix_report_export_report_run_id"), "report_export", ["report_run_id"])


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_reject_update_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in (
        "policy_source_blob",
        "rule_policy_binding",
        "report_item",
        "report_parse_error",
        "report_citation",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION f4_reject_update_delete();
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_policy_family_stable_identity() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               OR (to_jsonb(NEW) - 'display_name') <> (to_jsonb(OLD) - 'display_name') THEN
                RAISE EXCEPTION 'policy_family stable identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_family_stable_identity
        BEFORE UPDATE OR DELETE ON policy_family
        FOR EACH ROW EXECUTE FUNCTION f4_policy_family_stable_identity();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_reject_new_legacy_document() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'legacy_unpublished is reserved for the 0005 backfill';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_document_reject_new_legacy
        BEFORE INSERT ON policy_document
        FOR EACH ROW WHEN (NEW.status = 'legacy_unpublished')
        EXECUTE FUNCTION f4_reject_new_legacy_document();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_completed_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'completed' THEN
                RAISE EXCEPTION '% completed row is immutable: % is not permitted',
                    TG_TABLE_NAME, TG_OP;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in ("report_run", "report_export"):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_completed_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION f4_completed_immutable();
            """
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_published_document_immutable() RETURNS trigger AS $$
        BEGIN
            IF OLD.status = 'published' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'published policy_document is immutable';
                END IF;
                IF OLD.expiry_date IS NULL
                   AND NEW.expiry_date IS NOT NULL
                   AND NEW.expiry_date > OLD.effective_date
                   AND (to_jsonb(NEW) - 'expiry_date') = (to_jsonb(OLD) - 'expiry_date') THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'published policy_document is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_document_published_immutable
        BEFORE UPDATE OR DELETE ON policy_document
        FOR EACH ROW EXECUTE FUNCTION f4_published_document_immutable();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f4_published_clause_immutable() RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM policy_document
                WHERE id = OLD.document_id
                  AND tenant_id = OLD.tenant_id
                  AND status = 'published'
            ) THEN
                RAISE EXCEPTION 'published policy_clause is immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_clause_published_immutable
        BEFORE UPDATE OR DELETE ON policy_clause
        FOR EACH ROW EXECUTE FUNCTION f4_published_clause_immutable();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_policy_chunk_published_immutable
        BEFORE UPDATE OR DELETE ON policy_chunk
        FOR EACH ROW EXECUTE FUNCTION f4_published_clause_immutable();
        """
    )


def _guard_downgrade() -> None:
    bind = op.get_bind()
    protected_data = bind.scalar(
        sa.text(
            """
            SELECT
                EXISTS (SELECT 1 FROM policy_document WHERE status = 'published')
                OR EXISTS (SELECT 1 FROM rule_policy_binding)
                OR EXISTS (SELECT 1 FROM report_run)
                OR EXISTS (SELECT 1 FROM report_export)
            """
        )
    )
    if protected_data:
        raise RuntimeError(
            "cannot downgrade 0005 while published policy, binding, report, or export "
            "evidence exists; restore the verified pre-0005 backup into an isolated database"
        )


def downgrade() -> None:
    """Return to 0004 only when no F4 delivery evidence would be destroyed."""
    _guard_downgrade()

    op.execute("DROP TRIGGER IF EXISTS trg_policy_chunk_published_immutable ON policy_chunk")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_clause_published_immutable ON policy_clause")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_document_published_immutable ON policy_document")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_document_reject_new_legacy ON policy_document")
    op.execute("DROP TRIGGER IF EXISTS trg_policy_family_stable_identity ON policy_family")
    for table_name in ("report_export", "report_run"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_completed_immutable ON {table_name}")
    for table_name in (
        "report_citation",
        "report_parse_error",
        "report_item",
        "rule_policy_binding",
        "policy_source_blob",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION IF EXISTS f4_published_clause_immutable()")
    op.execute("DROP FUNCTION IF EXISTS f4_published_document_immutable()")
    op.execute("DROP FUNCTION IF EXISTS f4_reject_new_legacy_document()")
    op.execute("DROP FUNCTION IF EXISTS f4_policy_family_stable_identity()")
    op.execute("DROP FUNCTION IF EXISTS f4_completed_immutable()")
    op.execute("DROP FUNCTION IF EXISTS f4_reject_update_delete()")

    op.drop_table("report_export")
    op.drop_table("report_citation")
    op.drop_table("report_parse_error")
    op.drop_index("ix_report_item_report_attention_order", table_name="report_item")
    op.drop_table("report_item")
    op.drop_table("report_run")
    op.drop_table("rule_policy_binding")
    op.drop_table("policy_index_job")
    op.drop_table("policy_document_index")
    op.drop_table("policy_index_generation")
    op.drop_table("policy_chunk")

    op.drop_constraint(
        "uq_validation_run_id_tenant_id_file_version_id",
        "validation_run",
        type_="unique",
    )
    op.drop_constraint("uq_finding_id_tenant_id_file_version_id", "finding", type_="unique")
    op.drop_constraint("fk_finding_clause_tenant", "finding", type_="foreignkey")
    op.create_foreign_key(
        "fk_finding_clause_id_policy_clause",
        "finding",
        "policy_clause",
        ["clause_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_index("ix_policy_clause_family_id", table_name="policy_clause")
    op.drop_constraint(op.f("ck_policy_clause_legacy_or_complete"), "policy_clause", type_="check")
    op.drop_constraint(
        op.f("uq_policy_clause_document_id_ordinal"), "policy_clause", type_="unique"
    )
    op.drop_constraint("uq_policy_clause_id_tenant_id_document_id", "policy_clause", type_="unique")
    op.drop_constraint("uq_policy_clause_id_tenant_id", "policy_clause", type_="unique")
    op.drop_constraint(
        "fk_policy_clause_document_tenant_family", "policy_clause", type_="foreignkey"
    )
    op.drop_constraint("fk_policy_clause_document_tenant", "policy_clause", type_="foreignkey")
    op.create_foreign_key(
        "fk_policy_clause_document_id_policy_document",
        "policy_clause",
        "policy_document",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )
    for column_name in (
        "source_end",
        "source_start",
        "source_locator_json",
        "text_sha256",
        "ordinal",
        "family_id",
    ):
        op.drop_column("policy_clause", column_name)

    op.execute(
        "ALTER TABLE policy_document "
        "DROP CONSTRAINT ex_policy_document_published_effective_interval"
    )
    op.drop_index("ix_policy_document_source_blob_id", table_name="policy_document")
    op.drop_index("ix_policy_document_family_id", table_name="policy_document")
    op.drop_constraint(
        op.f("ck_policy_document_lifecycle_fields"), "policy_document", type_="check"
    )
    op.drop_constraint(
        op.f("ck_policy_document_legacy_or_complete"), "policy_document", type_="check"
    )
    op.drop_constraint(
        op.f("ck_policy_document_effective_interval"), "policy_document", type_="check"
    )
    for constraint_name in (
        "fk_policy_document_published_by_tenant",
        "fk_policy_document_created_by_tenant",
        "fk_policy_document_source_blob_tenant",
        "fk_policy_document_family_tenant",
    ):
        op.drop_constraint(constraint_name, "policy_document", type_="foreignkey")
    op.drop_constraint(
        op.f("uq_policy_document_tenant_id_family_id_version"),
        "policy_document",
        type_="unique",
    )
    op.drop_constraint(
        "uq_policy_document_id_tenant_id_family_id", "policy_document", type_="unique"
    )
    op.drop_constraint("uq_policy_document_id_tenant_id", "policy_document", type_="unique")
    for column_name in (
        "failure_code",
        "published_at",
        "published_by",
        "created_by",
        "status",
        "chunker_version",
        "parser_version",
        "extracted_text_sha256",
        "size_bytes",
        "mime_type",
        "content_sha256",
        "source_blob_id",
        "family_id",
    ):
        op.drop_column("policy_document", column_name)

    op.drop_table("policy_source_blob")
    op.drop_table("policy_family")
    # btree_gist is intentionally retained: it may predate this migration or
    # be used by objects outside ExpenseGuard's Alembic ownership.
