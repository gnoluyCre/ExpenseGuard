"""Add CP-F7.1 immutable anomaly-investigation persistence.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def _preflight_empty_evidence_step() -> None:
    """Refuse to guess which F6 candidate a legacy skeleton row belongs to."""

    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM evidence_step)")):
        raise RuntimeError(
            "cannot upgrade to 0009 while legacy evidence_step rows exist; "
            "provide an explicit F6 investigation provenance mapping before retrying"
        )


def _run_identity_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
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


def _create_investigation_run() -> None:
    op.create_table(
        "investigation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("correlation_finding_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version", sa.String(64), nullable=False),
        sa.Column("action_schema_version", sa.Integer(), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("provider_model", sa.String(255), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("redaction_version", sa.String(16), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_investigation_run_actor_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_investigation_run_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_run")),
        sa.UniqueConstraint(
            "id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            name="uq_investigation_run_identity",
        ),
        sa.CheckConstraint(
            "agent_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name=op.f("ck_investigation_run_agent_version_format"),
        ),
        sa.CheckConstraint(
            "action_schema_version = 1",
            name=op.f("ck_investigation_run_action_schema_version_value"),
        ),
        sa.CheckConstraint(
            "prompt_template_version ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name=op.f("ck_investigation_run_prompt_template_version_format"),
        ),
        sa.CheckConstraint(
            "provider_kind IN ('disabled', 'openai_compatible')",
            name=op.f("ck_investigation_run_provider_kind_values"),
        ),
        sa.CheckConstraint(
            "char_length(provider_model) BETWEEN 1 AND 255 AND provider_model !~ '[[:cntrl:]]'",
            name=op.f("ck_investigation_run_provider_model_valid"),
        ),
        sa.CheckConstraint(
            "max_steps BETWEEN 1 AND 12",
            name=op.f("ck_investigation_run_max_steps_range"),
        ),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 120",
            name=op.f("ck_investigation_run_timeout_seconds_range"),
        ),
        sa.CheckConstraint(
            "redaction_version ~ '^v[1-9][0-9]{0,8}$'",
            name=op.f("ck_investigation_run_redaction_version_format"),
        ),
        sa.CheckConstraint(
            "input_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_investigation_run_input_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "config_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_investigation_run_config_fingerprint_format"),
        ),
    )
    op.create_index(
        "ix_investigation_run_candidate_created",
        "investigation_run",
        ["tenant_id", "detection_run_id", "correlation_finding_id", "created_at"],
    )
    op.create_index(op.f("ix_investigation_run_tenant_id"), "investigation_run", ["tenant_id"])


def _create_investigation_request() -> None:
    op.create_table(
        "investigation_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_run_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_finding_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _run_identity_fk("fk_investigation_request_run_identity"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_investigation_request_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_request")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_investigation_request_tenant_idempotency_key",
        ),
        sa.CheckConstraint(
            "idempotency_key_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_investigation_request_idempotency_key_hash_format"),
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_investigation_request_request_fingerprint_format"),
        ),
    )
    op.create_index(
        "ix_investigation_request_run_id",
        "investigation_request",
        ["investigation_run_id"],
    )
    op.create_index(
        op.f("ix_investigation_request_tenant_id"),
        "investigation_request",
        ["tenant_id"],
    )


def _enhance_evidence_step() -> None:
    op.drop_index("ix_evidence_step_finding_id_step_no", table_name="evidence_step")
    op.drop_constraint(
        op.f("uq_evidence_step_finding_id_step_no"),
        "evidence_step",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_evidence_step_finding_id_finding"),
        "evidence_step",
        type_="foreignkey",
    )
    op.drop_column("evidence_step", "finding_id")

    for column in (
        sa.Column("investigation_run_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_finding_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("action_kind", sa.String(32), nullable=False),
        sa.Column("action_schema_version", sa.Integer(), nullable=False),
        sa.Column("model_action", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("decision_summary", sa.Text(), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
    ):
        op.add_column("evidence_step", column)
    op.alter_column("evidence_step", "tool_name", nullable=True)

    op.create_unique_constraint(
        "uq_evidence_step_run_step_no",
        "evidence_step",
        ["investigation_run_id", "step_no"],
    )
    op.create_foreign_key(
        "fk_evidence_step_run_identity",
        "evidence_step",
        "investigation_run",
        [
            "investigation_run_id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
        ],
        [
            "id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
        ],
        ondelete="RESTRICT",
    )
    for constraint_name, sqltext in (
        ("step_no_positive", "step_no > 0"),
        ("action_kind_values", "action_kind IN ('tool_call', 'terminate')"),
        ("action_schema_version_value", "action_schema_version = 1"),
        ("model_action_object", "jsonb_typeof(model_action) = 'object'"),
        (
            "model_action_size",
            "octet_length(convert_to(model_action::text, 'UTF8')) <= 65536",
        ),
        (
            "action_tool_consistent",
            "(action_kind = 'tool_call' AND tool_name IN "
            "('get_correlation_rows', 'get_employee_history', "
            "'get_supplier_history', 'search_policy_clauses') "
            "AND tool_input IS NOT NULL AND tool_output IS NOT NULL) OR "
            "(action_kind = 'terminate' AND tool_name IS NULL "
            "AND tool_input IS NULL AND tool_output IS NULL)",
        ),
        ("tool_input_object", "tool_input IS NULL OR jsonb_typeof(tool_input) = 'object'"),
        ("tool_output_object", "tool_output IS NULL OR jsonb_typeof(tool_output) = 'object'"),
        (
            "tool_input_size",
            "tool_input IS NULL OR octet_length(convert_to(tool_input::text, 'UTF8')) <= 32768",
        ),
        (
            "tool_output_size",
            "tool_output IS NULL OR octet_length(convert_to(tool_output::text, 'UTF8')) <= 262144",
        ),
        (
            "decision_summary_valid",
            "char_length(decision_summary) BETWEEN 1 AND 1000 "
            "AND decision_summary !~ '[[:cntrl:]]'",
        ),
        ("payload_fingerprint_format", "payload_fingerprint ~ '^[0-9a-f]{64}$'"),
    ):
        op.create_check_constraint(
            op.f(f"ck_evidence_step_{constraint_name}"),
            "evidence_step",
            sqltext,
        )
    op.create_index(
        "ix_evidence_step_run_step_no",
        "evidence_step",
        ["investigation_run_id", "step_no"],
    )


def _create_investigation_result() -> None:
    op.create_table(
        "investigation_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("investigation_run_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_finding_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("evidence_sufficient", sa.Boolean(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("citations_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_fingerprint", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _run_identity_fk("fk_investigation_result_run_identity"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_investigation_result_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_result")),
        sa.UniqueConstraint(
            "investigation_run_id",
            name="uq_investigation_result_run_id",
        ),
        sa.CheckConstraint(
            "outcome IN ('sufficient', 'insufficient', 'unavailable', 'max_steps', 'failed')",
            name=op.f("ck_investigation_result_outcome_values"),
        ),
        sa.CheckConstraint(
            "(outcome = 'sufficient' AND evidence_sufficient IS TRUE) OR "
            "(outcome = 'insufficient' AND evidence_sufficient IS FALSE) OR "
            "(outcome IN ('unavailable', 'max_steps', 'failed') "
            "AND evidence_sufficient IS NULL)",
            name=op.f("ck_investigation_result_outcome_sufficiency_consistent"),
        ),
        sa.CheckConstraint(
            "char_length(summary) BETWEEN 1 AND 2000 AND summary !~ '[[:cntrl:]]'",
            name=op.f("ck_investigation_result_summary_valid"),
        ),
        sa.CheckConstraint(
            "reason_code ~ '^[A-Z][A-Z0-9_]{0,63}$'",
            name=op.f("ck_investigation_result_reason_code_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(citations_json) = 'array'",
            name=op.f("ck_investigation_result_citations_array"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(citations_json::text, 'UTF8')) <= 262144",
            name=op.f("ck_investigation_result_citations_size"),
        ),
        sa.CheckConstraint(
            "result_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_investigation_result_result_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "completed_at >= created_at",
            name=op.f("ck_investigation_result_completed_after_created"),
        ),
    )
    op.create_index(
        "ix_investigation_result_candidate",
        "investigation_result",
        ["correlation_finding_id"],
    )
    op.create_index(
        op.f("ix_investigation_result_tenant_id"),
        "investigation_result",
        ["tenant_id"],
    )


def _create_pii_token() -> None:
    op.create_table(
        "pii_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_version", sa.String(16), nullable=False),
        sa.Column("pii_kind", sa.String(32), nullable=False),
        sa.Column("source_hmac", sa.String(64), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_pii_token_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pii_token")),
        sa.UniqueConstraint(
            "tenant_id",
            "token_version",
            "pii_kind",
            "source_hmac",
            name="uq_pii_token_source_identity",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "token",
            name="uq_pii_token_tenant_token",
        ),
        sa.CheckConstraint(
            "token_version ~ '^v[1-9][0-9]{0,8}$'",
            name=op.f("ck_pii_token_token_version_format"),
        ),
        sa.CheckConstraint(
            "pii_kind IN ('employee_name', 'employee_id', 'national_id', 'phone', "
            "'bank_account', 'supplier', 'merchant')",
            name=op.f("ck_pii_token_pii_kind_values"),
        ),
        sa.CheckConstraint(
            "source_hmac ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_pii_token_source_hmac_format"),
        ),
        sa.CheckConstraint(
            "token ~ '^[A-Z][A-Z0-9_]{1,31}_v[1-9][0-9]{0,8}_[0-9a-f]{16}$'",
            name=op.f("ck_pii_token_token_format"),
        ),
    )
    op.create_index(op.f("ix_pii_token_tenant_id"), "pii_token", ["tenant_id"])


def _create_immutability_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION f7_reject_update_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable: % is not permitted', TG_TABLE_NAME, TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name in (
        "investigation_run",
        "investigation_request",
        "evidence_step",
        "investigation_result",
        "pii_token",
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION f7_reject_update_delete();
            """
        )


def upgrade() -> None:
    """Install F7 persistence only after the legacy evidence skeleton is empty."""

    _preflight_empty_evidence_step()
    _create_investigation_run()
    _create_investigation_request()
    _enhance_evidence_step()
    _create_investigation_result()
    _create_pii_token()
    _create_immutability_triggers()


def _guard_downgrade() -> None:
    protected_data = op.get_bind().scalar(
        sa.text(
            """
            SELECT
                EXISTS (SELECT 1 FROM investigation_run)
                OR EXISTS (SELECT 1 FROM investigation_request)
                OR EXISTS (SELECT 1 FROM evidence_step)
                OR EXISTS (SELECT 1 FROM investigation_result)
                OR EXISTS (SELECT 1 FROM pii_token)
            """
        )
    )
    if protected_data:
        raise RuntimeError(
            "cannot downgrade 0009 while F7 run, request, step, result, or token facts exist; "
            "restore the verified pre-0009 backup into an isolated database"
        )


def _drop_immutability_triggers() -> None:
    for table_name in (
        "pii_token",
        "investigation_result",
        "evidence_step",
        "investigation_request",
        "investigation_run",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")


def _restore_evidence_step_skeleton() -> None:
    op.drop_index("ix_evidence_step_run_step_no", table_name="evidence_step")
    for constraint_name, constraint_type in (
        ("ck_evidence_step_payload_fingerprint_format", "check"),
        ("ck_evidence_step_decision_summary_valid", "check"),
        ("ck_evidence_step_tool_output_size", "check"),
        ("ck_evidence_step_tool_input_size", "check"),
        ("ck_evidence_step_tool_output_object", "check"),
        ("ck_evidence_step_tool_input_object", "check"),
        ("ck_evidence_step_action_tool_consistent", "check"),
        ("ck_evidence_step_model_action_size", "check"),
        ("ck_evidence_step_model_action_object", "check"),
        ("ck_evidence_step_action_schema_version_value", "check"),
        ("ck_evidence_step_action_kind_values", "check"),
        ("ck_evidence_step_step_no_positive", "check"),
        ("fk_evidence_step_run_identity", "foreignkey"),
        ("uq_evidence_step_run_step_no", "unique"),
    ):
        op.drop_constraint(op.f(constraint_name), "evidence_step", type_=constraint_type)
    op.alter_column("evidence_step", "tool_name", nullable=False)
    for column_name in (
        "payload_fingerprint",
        "decision_summary",
        "model_action",
        "action_schema_version",
        "action_kind",
        "file_version_id",
        "detection_run_id",
        "correlation_finding_id",
        "investigation_run_id",
    ):
        op.drop_column("evidence_step", column_name)
    op.add_column("evidence_step", sa.Column("finding_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        op.f("fk_evidence_step_finding_id_finding"),
        "evidence_step",
        "finding",
        ["finding_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        op.f("uq_evidence_step_finding_id_step_no"),
        "evidence_step",
        ["finding_id", "step_no"],
    )
    op.create_index(
        "ix_evidence_step_finding_id_step_no",
        "evidence_step",
        ["finding_id", "step_no"],
    )


def downgrade() -> None:
    """Return to the exact 0008 skeleton only when no F7 facts exist."""

    _guard_downgrade()
    _drop_immutability_triggers()
    op.drop_table("pii_token")
    op.drop_table("investigation_result")
    _restore_evidence_step_skeleton()
    op.drop_table("investigation_request")
    op.drop_table("investigation_run")
    op.execute("DROP FUNCTION f7_reject_update_delete()")
