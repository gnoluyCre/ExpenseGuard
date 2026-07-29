"""Add CP-F5.1 immutable human-review persistence.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _preflight_empty_legacy_review_tables() -> None:
    """Refuse to invent F5 provenance for skeleton rows created before 0007."""
    legacy_rows = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT
                (SELECT count(*) FROM review) AS review_count,
                (SELECT count(*) FROM sampling_audit) AS sampling_count
            """
            )
        )
        .one()
    )
    if legacy_rows.review_count or legacy_rows.sampling_count:
        raise RuntimeError(
            "cannot upgrade to 0007 while legacy review or sampling_audit rows exist; "
            "provide an explicit provenance mapping before retrying"
        )


def _add_parent_identity_constraints() -> None:
    op.create_unique_constraint(
        "uq_report_item_review_identity",
        "report_item",
        ["id", "tenant_id", "report_run_id", "file_version_id", "finding_id"],
    )
    op.create_unique_constraint(
        "uq_expense_row_file_row_tenant",
        "expense_row",
        ["file_version_id", "row_no", "tenant_id"],
    )
    op.create_unique_constraint(
        "uq_row_result_file_row_tenant",
        "row_result",
        ["file_version_id", "row_no", "tenant_id"],
    )


def _create_sampling_config() -> None:
    op.create_table(
        "review_sampling_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("min_sample_size", sa.Integer(), nullable=False),
        sa.Column("max_sample_size", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_review_sampling_config_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_review_sampling_config_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_sampling_config")),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_review_sampling_config_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_review_sampling_config_idempotency_key",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_review_sampling_config_id_tenant",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "version",
            "config_fingerprint",
            "rate_bps",
            "min_sample_size",
            "max_sample_size",
            "algorithm_version",
            name="uq_review_sampling_config_snapshot",
        ),
        sa.CheckConstraint(
            "version > 0",
            name=op.f("ck_review_sampling_config_version_positive"),
        ),
        sa.CheckConstraint(
            "rate_bps BETWEEN 1 AND 10000",
            name=op.f("ck_review_sampling_config_rate_bps_range"),
        ),
        sa.CheckConstraint(
            "min_sample_size >= 1",
            name=op.f("ck_review_sampling_config_min_sample_size_positive"),
        ),
        sa.CheckConstraint(
            "max_sample_size >= min_sample_size",
            name=op.f("ck_review_sampling_config_sample_size_order"),
        ),
        sa.CheckConstraint(
            "algorithm_version = 'sha256-rank-v1'",
            name=op.f("ck_review_sampling_config_algorithm_version_value"),
        ),
        sa.CheckConstraint(
            "char_length(config_fingerprint) = 64",
            name=op.f("ck_review_sampling_config_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_review_sampling_config_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_review_sampling_config_request_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "char_length(change_reason) BETWEEN 1 AND 500 AND change_reason !~ '[[:cntrl:]]'",
            name=op.f("ck_review_sampling_config_change_reason_valid"),
        ),
    )
    op.create_index(
        op.f("ix_review_sampling_config_tenant_id"),
        "review_sampling_config",
        ["tenant_id"],
    )


def _create_sampling_plan() -> None:
    op.create_table(
        "review_sampling_plan",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("sampling_config_id", sa.Uuid(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("min_sample_size", sa.Integer(), nullable=False),
        sa.Column("max_sample_size", sa.Integer(), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("seed_hex", sa.String(64), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_review_sampling_plan_report_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_review_sampling_plan_file_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "sampling_config_id",
                "tenant_id",
                "config_version",
                "config_fingerprint",
                "rate_bps",
                "min_sample_size",
                "max_sample_size",
                "algorithm_version",
            ],
            [
                "review_sampling_config.id",
                "review_sampling_config.tenant_id",
                "review_sampling_config.version",
                "review_sampling_config.config_fingerprint",
                "review_sampling_config.rate_bps",
                "review_sampling_config.min_sample_size",
                "review_sampling_config.max_sample_size",
                "review_sampling_config.algorithm_version",
            ],
            name="fk_review_sampling_plan_config_snapshot",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_review_sampling_plan_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_review_sampling_plan_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_sampling_plan")),
        sa.UniqueConstraint(
            "report_run_id",
            name="uq_review_sampling_plan_report_run_id",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_review_sampling_plan_id_tenant",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "report_run_id",
            name="uq_review_sampling_plan_id_tenant_report",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "report_run_id",
            "file_version_id",
            name="uq_review_sampling_plan_identity",
        ),
        sa.CheckConstraint(
            "config_version > 0",
            name=op.f("ck_review_sampling_plan_config_version_positive"),
        ),
        sa.CheckConstraint(
            "rate_bps BETWEEN 1 AND 10000",
            name=op.f("ck_review_sampling_plan_rate_bps_range"),
        ),
        sa.CheckConstraint(
            "min_sample_size >= 1",
            name=op.f("ck_review_sampling_plan_min_sample_size_positive"),
        ),
        sa.CheckConstraint(
            "max_sample_size >= min_sample_size",
            name=op.f("ck_review_sampling_plan_sample_size_order"),
        ),
        sa.CheckConstraint(
            "algorithm_version = 'sha256-rank-v1'",
            name=op.f("ck_review_sampling_plan_algorithm_version_value"),
        ),
        sa.CheckConstraint(
            "char_length(config_fingerprint) = 64",
            name=op.f("ck_review_sampling_plan_fingerprint_length"),
        ),
        sa.CheckConstraint(
            "seed_hex ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_review_sampling_plan_seed_hex_format"),
        ),
        sa.CheckConstraint(
            "(eligible_count = 0 AND sample_size = 0) OR "
            "(eligible_count > 0 AND sample_size = LEAST("
            "eligible_count, max_sample_size, GREATEST("
            "min_sample_size, ((eligible_count::bigint * rate_bps + 9999) / 10000)::integer)))",
            name=op.f("ck_review_sampling_plan_counts_consistent"),
        ),
    )
    for column_name in (
        "tenant_id",
        "report_run_id",
        "file_version_id",
        "sampling_config_id",
    ):
        op.create_index(
            op.f(f"ix_review_sampling_plan_{column_name}"),
            "review_sampling_plan",
            [column_name],
        )


def _enhance_review() -> None:
    op.drop_constraint("fk_review_finding_id_finding", "review", type_="foreignkey")
    op.drop_constraint("fk_review_reviewer_id_app_user", "review", type_="foreignkey")
    for column in (
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("report_item_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
    ):
        op.add_column("review", column)
    op.create_unique_constraint("uq_review_report_item_id", "review", ["report_item_id"])
    op.create_unique_constraint(
        "uq_review_tenant_idempotency_key",
        "review",
        ["tenant_id", "idempotency_key_hash"],
    )
    op.create_foreign_key(
        "fk_review_report_item_identity",
        "review",
        "report_item",
        ["report_item_id", "tenant_id", "report_run_id", "file_version_id", "finding_id"],
        ["id", "tenant_id", "report_run_id", "file_version_id", "finding_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_report_tenant_file",
        "review",
        "report_run",
        ["report_run_id", "tenant_id", "file_version_id"],
        ["id", "tenant_id", "file_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_finding_tenant_file",
        "review",
        "finding",
        ["finding_id", "tenant_id", "file_version_id"],
        ["id", "tenant_id", "file_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_file_version_tenant",
        "review",
        "file_version",
        ["file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_review_reviewer_tenant",
        "review",
        "app_user",
        ["reviewer_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_review_decision_values"),
        "review",
        "decision IN ('confirmed', 'false_positive')",
    )
    op.create_check_constraint(
        op.f("ck_review_note_valid"),
        "review",
        "note IS NULL OR (char_length(note) BETWEEN 1 AND 2000 AND note !~ '[[:cntrl:]]')",
    )
    op.create_check_constraint(
        op.f("ck_review_false_positive_note_required"),
        "review",
        "decision <> 'false_positive' OR note IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_review_idempotency_hash_length"),
        "review",
        "char_length(idempotency_key_hash) = 64",
    )
    op.create_check_constraint(
        op.f("ck_review_request_fingerprint_length"),
        "review",
        "char_length(request_fingerprint) = 64",
    )
    op.create_index(op.f("ix_review_report_run_id"), "review", ["report_run_id"])
    op.create_index(op.f("ix_review_file_version_id"), "review", ["file_version_id"])


def _enhance_sampling_audit() -> None:
    op.drop_constraint(
        "fk_sampling_audit_file_version_id_tenant_id_file_version",
        "sampling_audit",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_sampling_audit_reviewer_id_app_user",
        "sampling_audit",
        type_="foreignkey",
    )
    for column in (
        sa.Column("sampling_plan_id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("selection_rank", sa.Integer(), nullable=False),
        sa.Column("selection_score_sha256", sa.String(64), nullable=False),
    ):
        op.add_column("sampling_audit", column)
    op.create_unique_constraint(
        "uq_sampling_audit_plan_rank",
        "sampling_audit",
        ["sampling_plan_id", "selection_rank"],
    )
    op.create_unique_constraint(
        "uq_sampling_audit_plan_row",
        "sampling_audit",
        ["sampling_plan_id", "row_no"],
    )
    op.create_unique_constraint(
        "uq_sampling_audit_review_identity",
        "sampling_audit",
        ["id", "tenant_id", "sampling_plan_id", "report_run_id", "file_version_id"],
    )
    op.create_foreign_key(
        "fk_sampling_audit_plan_identity",
        "sampling_audit",
        "review_sampling_plan",
        ["sampling_plan_id", "tenant_id", "report_run_id", "file_version_id"],
        ["id", "tenant_id", "report_run_id", "file_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sampling_audit_report_tenant_file",
        "sampling_audit",
        "report_run",
        ["report_run_id", "tenant_id", "file_version_id"],
        ["id", "tenant_id", "file_version_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sampling_audit_expense_row_identity",
        "sampling_audit",
        "expense_row",
        ["file_version_id", "row_no", "tenant_id"],
        ["file_version_id", "row_no", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sampling_audit_row_result_identity",
        "sampling_audit",
        "row_result",
        ["file_version_id", "row_no", "tenant_id"],
        ["file_version_id", "row_no", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_sampling_audit_legacy_reviewer_tenant",
        "sampling_audit",
        "app_user",
        ["reviewer_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_sampling_audit_selection_rank_positive"),
        "sampling_audit",
        "selection_rank > 0",
    )
    op.create_check_constraint(
        op.f("ck_sampling_audit_selection_score_length"),
        "sampling_audit",
        "char_length(selection_score_sha256) = 64",
    )
    op.create_check_constraint(
        op.f("ck_sampling_audit_legacy_review_fields_null"),
        "sampling_audit",
        "decision IS NULL AND reviewer_id IS NULL AND reviewed_at IS NULL",
    )
    op.create_index(
        op.f("ix_sampling_audit_sampling_plan_id"),
        "sampling_audit",
        ["sampling_plan_id"],
    )
    op.create_index(
        op.f("ix_sampling_audit_report_run_id"),
        "sampling_audit",
        ["report_run_id"],
    )


def _create_sampling_review() -> None:
    op.create_table(
        "sampling_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sampling_audit_id", sa.Uuid(), nullable=False),
        sa.Column("sampling_plan_id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(19), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            [
                "sampling_audit_id",
                "tenant_id",
                "sampling_plan_id",
                "report_run_id",
                "file_version_id",
            ],
            [
                "sampling_audit.id",
                "sampling_audit.tenant_id",
                "sampling_audit.sampling_plan_id",
                "sampling_audit.report_run_id",
                "sampling_audit.file_version_id",
            ],
            name="fk_sampling_review_sample_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sampling_plan_id", "tenant_id", "report_run_id", "file_version_id"],
            [
                "review_sampling_plan.id",
                "review_sampling_plan.tenant_id",
                "review_sampling_plan.report_run_id",
                "review_sampling_plan.file_version_id",
            ],
            name="fk_sampling_review_plan_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_sampling_review_report_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_sampling_review_file_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_sampling_review_reviewer_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_sampling_review_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sampling_review")),
        sa.UniqueConstraint(
            "sampling_audit_id",
            name="uq_sampling_review_sampling_audit_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_sampling_review_tenant_idempotency_key",
        ),
        sa.CheckConstraint(
            "decision IN ('clearance_confirmed', 'missed_issue')",
            name=op.f("ck_sampling_review_decision_values"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR (char_length(note) BETWEEN 1 AND 2000 AND note !~ '[[:cntrl:]]')",
            name=op.f("ck_sampling_review_note_valid"),
        ),
        sa.CheckConstraint(
            "decision <> 'missed_issue' OR note IS NOT NULL",
            name=op.f("ck_sampling_review_missed_issue_note_required"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_sampling_review_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_sampling_review_request_fingerprint_length"),
        ),
    )
    op.create_index(op.f("ix_sampling_review_tenant_id"), "sampling_review", ["tenant_id"])


def _create_plan_request() -> None:
    op.create_table(
        "review_plan_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("sampling_plan_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id"],
            ["report_run.id", "report_run.tenant_id"],
            name="fk_review_plan_request_report_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sampling_plan_id", "tenant_id", "report_run_id"],
            [
                "review_sampling_plan.id",
                "review_sampling_plan.tenant_id",
                "review_sampling_plan.report_run_id",
            ],
            name="fk_review_plan_request_plan_tenant_report",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_review_plan_request_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_plan_request")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_review_plan_request_tenant_idempotency_key",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_review_plan_request_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_review_plan_request_request_fingerprint_length"),
        ),
    )
    for column_name in ("tenant_id", "report_run_id", "sampling_plan_id"):
        op.create_index(
            op.f(f"ix_review_plan_request_{column_name}"),
            "review_plan_request",
            [column_name],
        )


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION f5_reject_update_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in (
        "review_sampling_config",
        "review_sampling_plan",
        "review",
        "sampling_audit",
        "sampling_review",
        "review_plan_request",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION f5_reject_update_delete();
            """
        )


def upgrade() -> None:
    """Install F5 persistence only after the legacy skeleton is proven empty."""
    _preflight_empty_legacy_review_tables()
    _add_parent_identity_constraints()
    _create_sampling_config()
    _create_sampling_plan()
    _enhance_review()
    _enhance_sampling_audit()
    _create_sampling_review()
    _create_plan_request()
    _create_immutability_triggers()


def _guard_downgrade() -> None:
    protected_data = op.get_bind().scalar(
        sa.text(
            """
            SELECT
                EXISTS (SELECT 1 FROM review_sampling_config)
                OR EXISTS (SELECT 1 FROM review_sampling_plan)
                OR EXISTS (SELECT 1 FROM review)
                OR EXISTS (SELECT 1 FROM sampling_audit)
                OR EXISTS (SELECT 1 FROM sampling_review)
                OR EXISTS (SELECT 1 FROM review_plan_request)
            """
        )
    )
    if protected_data:
        raise RuntimeError(
            "cannot downgrade 0007 while F5 config, plan, review, sample, or request "
            "evidence exists; restore the verified pre-0007 backup into an isolated database"
        )


def _drop_immutability_triggers() -> None:
    for table_name in (
        "review_plan_request",
        "sampling_review",
        "sampling_audit",
        "review",
        "review_sampling_plan",
        "review_sampling_config",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")


def _restore_sampling_audit_skeleton() -> None:
    op.drop_index(op.f("ix_sampling_audit_report_run_id"), table_name="sampling_audit")
    op.drop_index(op.f("ix_sampling_audit_sampling_plan_id"), table_name="sampling_audit")
    for constraint_name, constraint_type in (
        ("ck_sampling_audit_legacy_review_fields_null", "check"),
        ("ck_sampling_audit_selection_score_length", "check"),
        ("ck_sampling_audit_selection_rank_positive", "check"),
        ("fk_sampling_audit_legacy_reviewer_tenant", "foreignkey"),
        ("fk_sampling_audit_row_result_identity", "foreignkey"),
        ("fk_sampling_audit_expense_row_identity", "foreignkey"),
        ("fk_sampling_audit_report_tenant_file", "foreignkey"),
        ("fk_sampling_audit_plan_identity", "foreignkey"),
        ("uq_sampling_audit_review_identity", "unique"),
        ("uq_sampling_audit_plan_row", "unique"),
        ("uq_sampling_audit_plan_rank", "unique"),
    ):
        op.drop_constraint(op.f(constraint_name), "sampling_audit", type_=constraint_type)
    for column_name in (
        "selection_score_sha256",
        "selection_rank",
        "report_run_id",
        "sampling_plan_id",
    ):
        op.drop_column("sampling_audit", column_name)
    op.create_foreign_key(
        "fk_sampling_audit_file_version_id_tenant_id_file_version",
        "sampling_audit",
        "file_version",
        ["file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_sampling_audit_reviewer_id_app_user",
        "sampling_audit",
        "app_user",
        ["reviewer_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _restore_review_skeleton() -> None:
    op.drop_index(op.f("ix_review_file_version_id"), table_name="review")
    op.drop_index(op.f("ix_review_report_run_id"), table_name="review")
    for constraint_name, constraint_type in (
        ("ck_review_request_fingerprint_length", "check"),
        ("ck_review_idempotency_hash_length", "check"),
        ("ck_review_false_positive_note_required", "check"),
        ("ck_review_note_valid", "check"),
        ("ck_review_decision_values", "check"),
        ("fk_review_reviewer_tenant", "foreignkey"),
        ("fk_review_file_version_tenant", "foreignkey"),
        ("fk_review_finding_tenant_file", "foreignkey"),
        ("fk_review_report_tenant_file", "foreignkey"),
        ("fk_review_report_item_identity", "foreignkey"),
        ("uq_review_tenant_idempotency_key", "unique"),
        ("uq_review_report_item_id", "unique"),
    ):
        op.drop_constraint(op.f(constraint_name), "review", type_=constraint_type)
    for column_name in (
        "request_fingerprint",
        "idempotency_key_hash",
        "file_version_id",
        "report_item_id",
        "report_run_id",
    ):
        op.drop_column("review", column_name)
    op.create_foreign_key(
        "fk_review_finding_id_finding",
        "review",
        "finding",
        ["finding_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_review_reviewer_id_app_user",
        "review",
        "app_user",
        ["reviewer_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Return to the exact 0006 skeleton only when no F5 evidence exists."""
    _guard_downgrade()
    _drop_immutability_triggers()
    op.drop_table("review_plan_request")
    op.drop_table("sampling_review")
    _restore_sampling_audit_skeleton()
    _restore_review_skeleton()
    op.drop_table("review_sampling_plan")
    op.drop_table("review_sampling_config")
    op.drop_constraint("uq_row_result_file_row_tenant", "row_result", type_="unique")
    op.drop_constraint("uq_expense_row_file_row_tenant", "expense_row", type_="unique")
    op.drop_constraint("uq_report_item_review_identity", "report_item", type_="unique")
    op.execute("DROP FUNCTION f5_reject_update_delete()")
