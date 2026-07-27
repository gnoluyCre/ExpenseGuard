"""F3 deterministic validation persistence.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-28

This migration replaces only the file content-hash uniqueness required for
revision lineages. All other protected uniqueness and audit constraints from
0001-0003 remain untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add immutable validation snapshots and revision lineage storage."""
    op.create_unique_constraint(
        "uq_app_user_id_tenant_id",
        "app_user",
        ["id", "tenant_id"],
    )

    op.add_column(
        "file_version",
        sa.Column("revision_no", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("file_version", sa.Column("source_file_version_id", sa.Uuid(), nullable=True))
    op.add_column("file_version", sa.Column("root_file_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "file_version",
        sa.Column(
            "revision_reason",
            sa.Enum(
                "ruleset_change",
                "mapping_change",
                name="revision_reason_enum",
                native_enum=False,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "file_version",
        sa.Column("revision_request_key_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "file_version",
        sa.Column("revision_request_fingerprint", sa.String(64), nullable=True),
    )
    op.drop_constraint(
        "uq_file_version_tenant_id_content_hash",
        "file_version",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_file_version_tenant_id_content_hash_revision_no",
        "file_version",
        ["tenant_id", "content_hash", "revision_no"],
    )
    op.create_check_constraint(
        op.f("ck_file_version_revision_no_positive"),
        "file_version",
        "revision_no > 0",
    )
    op.create_check_constraint(
        op.f("ck_file_version_revision_reason_values"),
        "file_version",
        "revision_reason IS NULL OR revision_reason IN ('ruleset_change', 'mapping_change')",
    )
    op.create_check_constraint(
        op.f("ck_file_version_revision_lineage"),
        "file_version",
        "(revision_no = 1 AND source_file_version_id IS NULL "
        "AND root_file_version_id IS NULL AND revision_reason IS NULL "
        "AND revision_request_key_hash IS NULL "
        "AND revision_request_fingerprint IS NULL) OR "
        "(revision_no > 1 AND source_file_version_id IS NOT NULL "
        "AND root_file_version_id IS NOT NULL AND revision_reason IS NOT NULL "
        "AND revision_request_key_hash IS NOT NULL "
        "AND revision_request_fingerprint IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_file_version_source_file_version_tenant",
        "file_version",
        "file_version",
        ["source_file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_file_version_root_file_version_tenant",
        "file_version",
        "file_version",
        ["root_file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_file_version_revision_one_content_hash",
        "file_version",
        ["tenant_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("revision_no = 1"),
    )
    op.create_index(
        "uq_file_version_source_request_key",
        "file_version",
        ["tenant_id", "source_file_version_id", "revision_request_key_hash"],
        unique=True,
        postgresql_where=sa.text("revision_request_key_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_file_version_source_file_version_id",
        "file_version",
        ["source_file_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_file_version_root_file_version_id",
        "file_version",
        ["root_file_version_id"],
        unique=False,
    )

    op.add_column(
        "rule_config",
        sa.Column("config_fingerprint", sa.String(64), nullable=True),
    )
    op.add_column("rule_config", sa.Column("created_by", sa.Uuid(), nullable=True))
    op.add_column(
        "rule_config",
        sa.Column("backfilled_legacy", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.execute("UPDATE rule_config SET backfilled_legacy = true")
    op.create_unique_constraint(
        "uq_rule_config_id_tenant_id",
        "rule_config",
        ["id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_rule_config_created_by_tenant",
        "rule_config",
        "app_user",
        ["created_by", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "validation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_version_id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_fingerprint", sa.String(64), nullable=False),
        sa.Column("ruleset_manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "in_progress",
                "completed",
                name="validation_run_status_enum",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("total_row_count", sa.Integer(), nullable=False),
        sa.Column("evaluated_row_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("flagged_count", sa.Integer(), nullable=False),
        sa.Column("manual_review_count", sa.Integer(), nullable=False),
        sa.Column("parse_failed_count", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name=op.f("ck_validation_run_status_values"),
        ),
        sa.CheckConstraint(
            "total_row_count >= 0 AND evaluated_row_count >= 0 "
            "AND passed_count >= 0 AND flagged_count >= 0 "
            "AND manual_review_count >= 0 AND parse_failed_count >= 0",
            name=op.f("ck_validation_run_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "total_row_count = evaluated_row_count + parse_failed_count",
            name=op.f("ck_validation_run_total_count_consistent"),
        ),
        sa.CheckConstraint(
            "evaluated_row_count = passed_count + flagged_count + manual_review_count",
            name=op.f("ck_validation_run_evaluated_count_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name=op.f("ck_validation_run_completion_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_validation_run_file_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_validation_run_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_validation_run_triggered_by_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_validation_run_tenant_id_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_validation_run"),
        sa.UniqueConstraint("file_version_id", name="uq_validation_run_file_version_id"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_validation_run_id_tenant_id"),
    )
    op.create_index("ix_validation_run_tenant_id", "validation_run", ["tenant_id"])
    op.create_index(
        "ix_validation_run_mapping_version_id",
        "validation_run",
        ["mapping_version_id"],
    )

    op.create_table(
        "validation_dependency",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("depended_file_version_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id", "tenant_id"],
            ["validation_run.id", "validation_run.tenant_id"],
            name="fk_validation_dependency_validation_run_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["depended_file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_validation_dependency_file_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name="fk_validation_dependency_tenant_id_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_validation_dependency"),
        sa.UniqueConstraint(
            "validation_run_id",
            "depended_file_version_id",
            name="uq_validation_dependency_run_file",
        ),
    )
    op.create_index(
        "ix_validation_dependency_tenant_id",
        "validation_dependency",
        ["tenant_id"],
    )
    op.create_index(
        "ix_validation_dependency_depended_file_version_id",
        "validation_dependency",
        ["depended_file_version_id"],
    )

    op.add_column("finding", sa.Column("validation_run_id", sa.Uuid(), nullable=True))
    op.add_column(
        "finding",
        sa.Column(
            "rule_kind",
            sa.Enum(
                "limit",
                "invoice_type",
                "timeliness",
                "invoice_title",
                "invoice_duplicate",
                name="rule_kind_enum",
                native_enum=False,
            ),
            nullable=True,
        ),
    )
    op.add_column("finding", sa.Column("rule_config_id", sa.Uuid(), nullable=True))
    op.add_column(
        "finding",
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_finding_rule_kind_values"),
        "finding",
        "rule_kind IS NULL OR rule_kind IN "
        "('limit', 'invoice_type', 'timeliness', 'invoice_title', 'invoice_duplicate')",
    )
    op.create_foreign_key(
        "fk_finding_validation_run_tenant",
        "finding",
        "validation_run",
        ["validation_run_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_finding_rule_config_tenant",
        "finding",
        "rule_config",
        ["rule_config_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_finding_validation_run_id", "finding", ["validation_run_id"])
    op.create_index("ix_finding_rule_config_id", "finding", ["rule_config_id"])
    op.create_index(
        "uq_finding_deterministic_rule",
        "finding",
        ["validation_run_id", "row_no", "rule_id", "rule_kind"],
        unique=True,
        postgresql_where=sa.text("validation_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Return to 0003 only when no derived revision would be destroyed."""
    bind = op.get_bind()
    has_derived_revision = bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM file_version WHERE revision_no > 1)")
    )
    if has_derived_revision:
        raise RuntimeError(
            "cannot downgrade 0004 while derived file revisions exist; "
            "restore the verified pre-0004 backup into an isolated database"
        )

    op.drop_index("uq_finding_deterministic_rule", table_name="finding")
    op.drop_index("ix_finding_rule_config_id", table_name="finding")
    op.drop_index("ix_finding_validation_run_id", table_name="finding")
    op.drop_constraint("fk_finding_rule_config_tenant", "finding", type_="foreignkey")
    op.drop_constraint("fk_finding_validation_run_tenant", "finding", type_="foreignkey")
    op.drop_constraint(op.f("ck_finding_rule_kind_values"), "finding", type_="check")
    op.drop_column("finding", "evidence_json")
    op.drop_column("finding", "rule_config_id")
    op.drop_column("finding", "rule_kind")
    op.drop_column("finding", "validation_run_id")

    op.drop_index(
        "ix_validation_dependency_depended_file_version_id",
        table_name="validation_dependency",
    )
    op.drop_index("ix_validation_dependency_tenant_id", table_name="validation_dependency")
    op.drop_table("validation_dependency")
    op.drop_index("ix_validation_run_mapping_version_id", table_name="validation_run")
    op.drop_index("ix_validation_run_tenant_id", table_name="validation_run")
    op.drop_table("validation_run")

    op.drop_constraint("fk_rule_config_created_by_tenant", "rule_config", type_="foreignkey")
    op.drop_constraint("uq_rule_config_id_tenant_id", "rule_config", type_="unique")
    op.drop_column("rule_config", "backfilled_legacy")
    op.drop_column("rule_config", "created_by")
    op.drop_column("rule_config", "config_fingerprint")

    op.drop_index("ix_file_version_root_file_version_id", table_name="file_version")
    op.drop_index("ix_file_version_source_file_version_id", table_name="file_version")
    op.drop_index("uq_file_version_source_request_key", table_name="file_version")
    op.drop_index("uq_file_version_revision_one_content_hash", table_name="file_version")
    op.drop_constraint(
        "fk_file_version_root_file_version_tenant",
        "file_version",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_file_version_source_file_version_tenant",
        "file_version",
        type_="foreignkey",
    )
    op.drop_constraint(op.f("ck_file_version_revision_lineage"), "file_version", type_="check")
    op.drop_constraint(
        op.f("ck_file_version_revision_reason_values"), "file_version", type_="check"
    )
    op.drop_constraint(op.f("ck_file_version_revision_no_positive"), "file_version", type_="check")
    op.drop_constraint(
        "uq_file_version_tenant_id_content_hash_revision_no",
        "file_version",
        type_="unique",
    )
    op.drop_column("file_version", "revision_request_fingerprint")
    op.drop_column("file_version", "revision_request_key_hash")
    op.drop_column("file_version", "revision_reason")
    op.drop_column("file_version", "root_file_version_id")
    op.drop_column("file_version", "source_file_version_id")
    op.drop_column("file_version", "revision_no")
    op.create_unique_constraint(
        "uq_file_version_tenant_id_content_hash",
        "file_version",
        ["tenant_id", "content_hash"],
    )
    op.drop_constraint("uq_app_user_id_tenant_id", "app_user", type_="unique")
