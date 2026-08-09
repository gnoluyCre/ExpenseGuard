"""Add CP-F6.1 immutable cross-row detection persistence.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DETECTOR_VERSION_CHECK = """
    (detector = 'split_invoice' AND detector_version = 'split-window-v1') OR
    (detector = 'sequential_invoice' AND detector_version = 'invoice-sequence-v1') OR
    (detector = 'frequency_anomaly' AND detector_version = 'frequency-mad-v1') OR
    (detector = 'spatiotemporal_tier0'
        AND detector_version = 'spatiotemporal-pair-v1')
"""


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _preflight_empty_legacy_f6_tables() -> None:
    """Refuse to invent run/config provenance for legacy skeleton rows."""
    legacy_rows = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT
                    (SELECT count(*) FROM capability_declaration) AS capability_count,
                    (SELECT count(*) FROM correlation_finding) AS finding_count
                """
            )
        )
        .one()
    )
    if legacy_rows.capability_count or legacy_rows.finding_count:
        raise RuntimeError(
            "cannot upgrade to 0008 while legacy capability_declaration or "
            "correlation_finding rows exist; provide an explicit provenance mapping "
            "before retrying"
        )


def _create_detection_config() -> None:
    op.create_table(
        "detection_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_canonical", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("algorithm_bundle_version", sa.String(32), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_detection_config_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_detection_config_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detection_config")),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_detection_config_tenant_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_detection_config_tenant_idempotency_key",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "version",
            "config_fingerprint",
            "algorithm_bundle_version",
            name="uq_detection_config_snapshot",
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_detection_config_version_positive")),
        sa.CheckConstraint(
            "schema_version = 1",
            name=op.f("ck_detection_config_schema_version_value"),
        ),
        sa.CheckConstraint(
            "algorithm_bundle_version = 'correlation-v1'",
            name=op.f("ck_detection_config_algorithm_bundle_version_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(definition) = 'object'",
            name=op.f("ck_detection_config_definition_object"),
        ),
        sa.CheckConstraint(
            "definition_canonical::jsonb = definition",
            name=op.f("ck_detection_config_definition_canonical_matches"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(definition_canonical, 'UTF8')) <= 262144",
            name=op.f("ck_detection_config_definition_canonical_size"),
        ),
        sa.CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_config_config_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_config_idempotency_key_hash_format"),
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_config_request_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "char_length(change_reason) BETWEEN 1 AND 500 AND change_reason !~ '[[:cntrl:]]'",
            name=op.f("ck_detection_config_change_reason_valid"),
        ),
    )
    op.create_index(
        op.f("ix_detection_config_tenant_id"),
        "detection_config",
        ["tenant_id"],
    )


def _create_detection_run() -> None:
    op.create_table(
        "detection_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("detection_config_id", sa.Uuid(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("algorithm_bundle_version", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("run_fingerprint", sa.String(64), nullable=False),
        sa.Column("source_row_count", sa.Integer(), nullable=False),
        sa.Column("parsed_row_count", sa.Integer(), nullable=False),
        sa.Column("error_row_count", sa.Integer(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_detection_run_file_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_detection_run_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_detection_run_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detection_run")),
        sa.UniqueConstraint(
            "file_version_id",
            "config_fingerprint",
            name="uq_detection_run_file_config_fingerprint",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_detection_run_id_tenant_file",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "config_fingerprint",
            name="uq_detection_run_identity",
        ),
        sa.CheckConstraint(
            "config_version > 0",
            name=op.f("ck_detection_run_config_version_positive"),
        ),
        sa.CheckConstraint(
            "algorithm_bundle_version = 'correlation-v1'",
            name=op.f("ck_detection_run_algorithm_bundle_version_value"),
        ),
        sa.CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_run_config_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_run_input_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "run_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_run_run_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "source_row_count >= 0 AND parsed_row_count >= 0 "
            "AND error_row_count >= 0 AND finding_count >= 0",
            name=op.f("ck_detection_run_counts_non_negative"),
        ),
        sa.CheckConstraint(
            "source_row_count = parsed_row_count + error_row_count",
            name=op.f("ck_detection_run_source_count_consistent"),
        ),
        sa.CheckConstraint(
            "completed_at >= created_at",
            name=op.f("ck_detection_run_completed_after_created"),
        ),
    )
    op.create_index(
        op.f("ix_detection_run_detection_config_id"),
        "detection_run",
        ["detection_config_id"],
    )
    op.create_index(
        "ix_detection_run_tenant_file_created",
        "detection_run",
        ["tenant_id", "file_version_id", "created_at"],
    )
    op.create_index(
        op.f("ix_detection_run_tenant_id"),
        "detection_run",
        ["tenant_id"],
    )


def _create_detection_request() -> None:
    op.create_table(
        "detection_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["detection_run_id", "tenant_id", "file_version_id"],
            ["detection_run.id", "detection_run.tenant_id", "detection_run.file_version_id"],
            name="fk_detection_request_run_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_detection_request_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_detection_request")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_detection_request_tenant_idempotency_key",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_request_idempotency_key_hash_format"),
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_detection_request_request_fingerprint_format"),
        ),
    )
    op.create_index(
        "ix_detection_request_run_id",
        "detection_request",
        ["detection_run_id"],
    )
    op.create_index(
        op.f("ix_detection_request_tenant_id"),
        "detection_request",
        ["tenant_id"],
    )


def _enhance_capability_declaration() -> None:
    op.drop_constraint(
        op.f("fk_capability_declaration_file_version_id_tenant_id_file_version"),
        "capability_declaration",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_capability_declaration_file_version_id_detector"),
        "capability_declaration",
        type_="unique",
    )
    op.add_column(
        "capability_declaration",
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
    )
    op.add_column(
        "capability_declaration",
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
    )
    op.add_column(
        "capability_declaration",
        sa.Column("detector_version", sa.String(32), nullable=False),
    )
    op.add_column(
        "capability_declaration",
        sa.Column("reason_code", sa.String(64), nullable=False),
    )
    op.add_column(
        "capability_declaration",
        sa.Column("details_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )
    op.add_column(
        "capability_declaration",
        sa.Column("finding_count", sa.Integer(), nullable=False),
    )
    op.alter_column("capability_declaration", "reason", nullable=False)
    op.create_unique_constraint(
        "uq_capability_declaration_run_detector",
        "capability_declaration",
        ["detection_run_id", "detector"],
    )
    op.create_foreign_key(
        "fk_capability_declaration_run_identity",
        "capability_declaration",
        "detection_run",
        ["detection_run_id", "tenant_id", "file_version_id", "config_fingerprint"],
        ["id", "tenant_id", "file_version_id", "config_fingerprint"],
        ondelete="RESTRICT",
    )
    for constraint_name, sqltext in (
        ("detector_version_pair", DETECTOR_VERSION_CHECK),
        ("config_fingerprint_format", "config_fingerprint ~ '^[0-9a-f]{64}$'"),
        ("status_values", "status IN ('enabled', 'degraded', 'unavailable')"),
        ("reason_code_format", "reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'"),
        (
            "reason_valid",
            "char_length(reason) BETWEEN 1 AND 500 AND reason !~ '[[:cntrl:]]'",
        ),
        ("details_object", "jsonb_typeof(details_json) = 'object'"),
        ("finding_count_non_negative", "finding_count >= 0"),
    ):
        op.create_check_constraint(
            op.f(f"ck_capability_declaration_{constraint_name}"),
            "capability_declaration",
            sqltext,
        )
    op.create_index(
        op.f("ix_capability_declaration_detection_run_id"),
        "capability_declaration",
        ["detection_run_id"],
    )


def _enhance_correlation_finding() -> None:
    op.drop_constraint(
        op.f("fk_correlation_finding_file_version_id_tenant_id_file_version"),
        "correlation_finding",
        type_="foreignkey",
    )
    op.add_column(
        "correlation_finding",
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
    )
    op.add_column(
        "correlation_finding",
        sa.Column("detector_version", sa.String(32), nullable=False),
    )
    op.add_column(
        "correlation_finding",
        sa.Column("finding_key", sa.String(64), nullable=False),
    )
    op.add_column(
        "correlation_finding",
        sa.Column("evidence_schema_version", sa.Integer(), nullable=False),
    )
    op.add_column(
        "correlation_finding",
        sa.Column("reasoning_snapshot", sa.Text(), nullable=False),
    )
    op.drop_column("correlation_finding", "participating_row_nos")
    op.create_unique_constraint(
        "uq_correlation_finding_run_detector_key",
        "correlation_finding",
        ["detection_run_id", "detector", "finding_key"],
    )
    op.create_unique_constraint(
        "uq_correlation_finding_identity",
        "correlation_finding",
        ["id", "detection_run_id", "file_version_id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_correlation_finding_run_identity",
        "correlation_finding",
        "detection_run",
        ["detection_run_id", "tenant_id", "file_version_id"],
        ["id", "tenant_id", "file_version_id"],
        ondelete="RESTRICT",
    )
    for constraint_name, sqltext in (
        ("detector_version_pair", DETECTOR_VERSION_CHECK),
        ("finding_key_format", "finding_key ~ '^[0-9a-f]{64}$'"),
        ("evidence_schema_version_value", "evidence_schema_version = 1"),
        ("evidence_object", "jsonb_typeof(evidence_json) = 'object'"),
        (
            "reasoning_snapshot_non_empty",
            "char_length(reasoning_snapshot) > 0",
        ),
        (
            "severity_ungraded",
            "severity_impact = 0 AND severity_confidence = 0",
        ),
    ):
        op.create_check_constraint(
            op.f(f"ck_correlation_finding_{constraint_name}"),
            "correlation_finding",
            sqltext,
        )
    op.create_index(
        op.f("ix_correlation_finding_detection_run_id"),
        "correlation_finding",
        ["detection_run_id"],
    )


def _create_correlation_finding_row() -> None:
    op.create_table(
        "correlation_finding_row",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["file_version_id", "row_no", "tenant_id"],
            ["expense_row.file_version_id", "expense_row.row_no", "expense_row.tenant_id"],
            name="fk_correlation_finding_row_expense_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_correlation_finding_row_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_correlation_finding_row")),
        sa.UniqueConstraint(
            "finding_id",
            "row_no",
            name="uq_correlation_finding_row_finding_row",
        ),
        sa.UniqueConstraint(
            "finding_id",
            "ordinal",
            name="uq_correlation_finding_row_finding_ordinal",
        ),
        sa.CheckConstraint(
            "row_no > 0",
            name=op.f("ck_correlation_finding_row_row_no_positive"),
        ),
        sa.CheckConstraint(
            "ordinal > 0",
            name=op.f("ck_correlation_finding_row_ordinal_positive"),
        ),
    )
    op.create_index(
        "ix_correlation_finding_row_expense_identity",
        "correlation_finding_row",
        ["file_version_id", "row_no", "tenant_id"],
    )
    op.create_index(
        op.f("ix_correlation_finding_row_tenant_id"),
        "correlation_finding_row",
        ["tenant_id"],
    )


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION f6_reject_update_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in (
        "detection_config",
        "detection_run",
        "detection_request",
        "capability_declaration",
        "correlation_finding",
        "correlation_finding_row",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION f6_reject_update_delete();
            """
        )


def upgrade() -> None:
    """Install F6 persistence only after both legacy skeletons are proven empty."""
    _preflight_empty_legacy_f6_tables()
    _create_detection_config()
    _create_detection_run()
    _create_detection_request()
    _enhance_capability_declaration()
    _enhance_correlation_finding()
    _create_correlation_finding_row()
    _create_immutability_triggers()


def _guard_downgrade() -> None:
    protected_data = op.get_bind().scalar(
        sa.text(
            """
            SELECT
                EXISTS (SELECT 1 FROM detection_config)
                OR EXISTS (SELECT 1 FROM detection_run)
                OR EXISTS (SELECT 1 FROM detection_request)
                OR EXISTS (SELECT 1 FROM capability_declaration)
                OR EXISTS (SELECT 1 FROM correlation_finding)
                OR EXISTS (SELECT 1 FROM correlation_finding_row)
            """
        )
    )
    if protected_data:
        raise RuntimeError(
            "cannot downgrade 0008 while F6 config, run, request, declaration, "
            "finding, or participating-row evidence exists; restore the verified "
            "pre-0008 backup into an isolated database"
        )


def _drop_immutability_triggers() -> None:
    for table_name in (
        "correlation_finding_row",
        "correlation_finding",
        "capability_declaration",
        "detection_request",
        "detection_run",
        "detection_config",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")


def _restore_correlation_finding_skeleton() -> None:
    op.drop_index(
        op.f("ix_correlation_finding_detection_run_id"),
        table_name="correlation_finding",
    )
    for constraint_name, constraint_type in (
        ("ck_correlation_finding_severity_ungraded", "check"),
        ("ck_correlation_finding_reasoning_snapshot_non_empty", "check"),
        ("ck_correlation_finding_evidence_object", "check"),
        ("ck_correlation_finding_evidence_schema_version_value", "check"),
        ("ck_correlation_finding_finding_key_format", "check"),
        ("ck_correlation_finding_detector_version_pair", "check"),
        ("fk_correlation_finding_run_identity", "foreignkey"),
        ("uq_correlation_finding_identity", "unique"),
        ("uq_correlation_finding_run_detector_key", "unique"),
    ):
        op.drop_constraint(
            op.f(constraint_name),
            "correlation_finding",
            type_=constraint_type,
        )
    op.add_column(
        "correlation_finding",
        sa.Column("participating_row_nos", postgresql.ARRAY(sa.Integer()), nullable=False),
    )
    for column_name in (
        "reasoning_snapshot",
        "evidence_schema_version",
        "finding_key",
        "detector_version",
        "detection_run_id",
    ):
        op.drop_column("correlation_finding", column_name)
    op.create_foreign_key(
        op.f("fk_correlation_finding_file_version_id_tenant_id_file_version"),
        "correlation_finding",
        "file_version",
        ["file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )


def _restore_capability_declaration_skeleton() -> None:
    op.drop_index(
        op.f("ix_capability_declaration_detection_run_id"),
        table_name="capability_declaration",
    )
    for constraint_name, constraint_type in (
        ("ck_capability_declaration_finding_count_non_negative", "check"),
        ("ck_capability_declaration_details_object", "check"),
        ("ck_capability_declaration_reason_valid", "check"),
        ("ck_capability_declaration_reason_code_format", "check"),
        ("ck_capability_declaration_status_values", "check"),
        ("ck_capability_declaration_config_fingerprint_format", "check"),
        ("ck_capability_declaration_detector_version_pair", "check"),
        ("fk_capability_declaration_run_identity", "foreignkey"),
        ("uq_capability_declaration_run_detector", "unique"),
    ):
        op.drop_constraint(
            op.f(constraint_name),
            "capability_declaration",
            type_=constraint_type,
        )
    op.alter_column("capability_declaration", "reason", nullable=True)
    for column_name in (
        "finding_count",
        "details_json",
        "reason_code",
        "detector_version",
        "config_fingerprint",
        "detection_run_id",
    ):
        op.drop_column("capability_declaration", column_name)
    op.create_foreign_key(
        op.f("fk_capability_declaration_file_version_id_tenant_id_file_version"),
        "capability_declaration",
        "file_version",
        ["file_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        op.f("uq_capability_declaration_file_version_id_detector"),
        "capability_declaration",
        ["file_version_id", "detector"],
    )


def downgrade() -> None:
    """Return to the exact 0007 skeleton only when no F6 evidence exists."""
    _guard_downgrade()
    _drop_immutability_triggers()
    op.drop_table("correlation_finding_row")
    _restore_correlation_finding_skeleton()
    _restore_capability_declaration_skeleton()
    op.drop_table("detection_request")
    op.drop_table("detection_run")
    op.drop_table("detection_config")
    op.execute("DROP FUNCTION f6_reject_update_delete()")
