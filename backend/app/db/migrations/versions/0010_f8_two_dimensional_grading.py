"""Add CP-F8.1 immutable two-dimensional grading snapshots.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenant.id"],
        name=op.f(f"fk_{table}_tenant_id_tenant"),
        ondelete="RESTRICT",
    )


def _hash_check(table: str, column: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} ~ '^[0-9a-f]{{64}}$'", name=op.f(f"ck_{table}_{column}_format")
    )


def _create_reason_validator() -> None:
    op.execute(
        """
        CREATE FUNCTION f8_reason_codes_valid(codes jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        AS $$
          WITH allowed(code, rank) AS (
            VALUES
              ('IMPACT_RULE_MAPPING', 1),
              ('IMPACT_DETECTOR_MAPPING', 2),
              ('CONFIDENCE_F3_FLAGGED', 3),
              ('CONFIDENCE_F3_UNAVAILABLE', 4),
              ('CONFIDENCE_F7_SUFFICIENT', 5),
              ('CONFIDENCE_F7_INSUFFICIENT', 6),
              ('CONFIDENCE_F7_UNAVAILABLE', 7),
              ('CONFIDENCE_F7_MAX_STEPS', 8),
              ('CONFIDENCE_F7_FAILED', 9),
              ('CONFIDENCE_F7_NOT_RUN', 10),
              ('CAPABILITY_ENABLED', 11),
              ('CAPABILITY_DEGRADED', 12),
              ('CAPABILITY_UNAVAILABLE', 13),
              ('CONFIDENCE_CAP_APPLIED', 14),
              ('COST_MATRIX_SELECTED', 15),
              ('OVERRIDE_IMPACT_3', 16),
              ('OVERRIDE_CONFIDENCE_0', 17),
              ('OVERRIDE_F3_UNAVAILABLE', 18),
              ('OVERRIDE_F7_NON_SUFFICIENT', 19),
              ('OVERRIDE_CAPABILITY_UNAVAILABLE', 20),
              ('FINAL_HIGH_ATTENTION', 21),
              ('FINAL_MANUAL_ATTENTION', 22),
              ('FINAL_CLEARED', 23)
          ), expanded AS (
            SELECT item.value AS code, item.ordinality, allowed.rank
            FROM jsonb_array_elements_text(codes) WITH ORDINALITY AS item(value, ordinality)
            LEFT JOIN allowed ON allowed.code = item.value
          )
          SELECT jsonb_typeof(codes) = 'array'
            AND jsonb_array_length(codes) BETWEEN 1 AND 16
            AND (SELECT count(*) FROM expanded) = jsonb_array_length(codes)
            AND NOT EXISTS (SELECT 1 FROM expanded WHERE rank IS NULL)
            AND (SELECT count(DISTINCT code) FROM expanded) = jsonb_array_length(codes)
            AND NOT EXISTS (
              SELECT 1 FROM expanded left_item
              JOIN expanded right_item ON left_item.ordinality < right_item.ordinality
              WHERE left_item.rank >= right_item.rank
            );
        $$
        """
    )


def _add_source_snapshot_constraints() -> None:
    op.create_unique_constraint(
        "uq_finding_f8_source_identity",
        "finding",
        ["id", "validation_run_id", "file_version_id", "tenant_id", "rule_kind"],
    )
    op.create_unique_constraint(
        "uq_correlation_finding_f8_snapshot",
        "correlation_finding",
        [
            "id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            "detector",
            "detector_version",
            "finding_key",
        ],
    )
    op.create_unique_constraint(
        "uq_capability_declaration_f8_snapshot",
        "capability_declaration",
        [
            "id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            "config_fingerprint",
            "detector",
            "detector_version",
            "status",
        ],
    )
    op.create_unique_constraint(
        "uq_investigation_result_f8_snapshot",
        "investigation_result",
        [
            "id",
            "investigation_run_id",
            "correlation_finding_id",
            "detection_run_id",
            "file_version_id",
            "tenant_id",
            "outcome",
            "result_fingerprint",
        ],
    )


def _create_config() -> None:
    op.create_table(
        "grading_config",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("definition_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("canonical_definition", sa.Text(), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _tenant_fk("grading_config"),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_grading_config_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grading_config")),
        sa.UniqueConstraint("tenant_id", "version", name="uq_grading_config_tenant_version"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_grading_config_tenant_idempotency_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "config_fingerprint",
            name="uq_grading_config_tenant_fingerprint",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "version",
            "schema_version",
            "config_fingerprint",
            "algorithm_version",
            name="uq_grading_config_snapshot",
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_grading_config_version_positive")),
        sa.CheckConstraint(
            "schema_version = 1", name=op.f("ck_grading_config_schema_version_value")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(definition_json) = 'object'",
            name=op.f("ck_grading_config_definition_object"),
        ),
        sa.CheckConstraint(
            "canonical_definition::jsonb = definition_json",
            name=op.f("ck_grading_config_canonical_matches_definition"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(canonical_definition, 'UTF8')) <= 262144",
            name=op.f("ck_grading_config_canonical_size"),
        ),
        sa.CheckConstraint(
            "algorithm_version = 'cost-matrix-v1'",
            name=op.f("ck_grading_config_algorithm_version_value"),
        ),
        _hash_check("grading_config", "config_fingerprint"),
        _hash_check("grading_config", "idempotency_key_hash"),
        _hash_check("grading_config", "request_fingerprint"),
        sa.CheckConstraint(
            "char_length(change_reason) BETWEEN 1 AND 500 AND change_reason !~ '[[:cntrl:]]'",
            name=op.f("ck_grading_config_change_reason_valid"),
        ),
    )
    op.create_index("ix_grading_config_tenant_id", "grading_config", ["tenant_id"])


def _create_run() -> None:
    op.create_table(
        "grading_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("grading_config_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("config_schema_version", sa.Integer(), nullable=False),
        sa.Column("config_fingerprint", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("f3_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("f3_manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("f6_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("f6_manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("f7_manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("f7_manifest_fingerprint", sa.String(64), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("deterministic_item_count", sa.Integer(), nullable=False),
        sa.Column("correlation_item_count", sa.Integer(), nullable=False),
        sa.Column("high_attention_count", sa.Integer(), nullable=False),
        sa.Column("manual_attention_count", sa.Integer(), nullable=False),
        sa.Column("cleared_count", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _tenant_fk("grading_run"),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_grading_run_file_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id", "tenant_id", "file_version_id"],
            ["validation_run.id", "validation_run.tenant_id", "validation_run.file_version_id"],
            name="fk_grading_run_validation_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["detection_run_id", "tenant_id", "file_version_id"],
            ["detection_run.id", "detection_run.tenant_id", "detection_run.file_version_id"],
            name="fk_grading_run_detection_identity",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_grading_run_creator_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grading_run")),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "validation_run_id",
            "detection_run_id",
            name="uq_grading_run_identity",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            "input_fingerprint",
            name="uq_grading_run_request_identity",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "file_version_id",
            "validation_run_id",
            "detection_run_id",
            "grading_config_id",
            "input_fingerprint",
            name="uq_grading_run_business_identity",
        ),
        sa.CheckConstraint(
            "config_version > 0", name=op.f("ck_grading_run_config_version_positive")
        ),
        sa.CheckConstraint(
            "config_schema_version = 1",
            name=op.f("ck_grading_run_config_schema_version_value"),
        ),
        sa.CheckConstraint(
            "algorithm_version = 'cost-matrix-v1'",
            name=op.f("ck_grading_run_algorithm_version_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(f3_manifest_json) = 'object'",
            name=op.f("ck_grading_run_f3_manifest_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(f6_manifest_json) = 'object'",
            name=op.f("ck_grading_run_f6_manifest_object"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(f7_manifest_json) = 'object'",
            name=op.f("ck_grading_run_f7_manifest_object"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(f3_manifest_json::text, 'UTF8')) <= 8388608",
            name=op.f("ck_grading_run_f3_manifest_size"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(f6_manifest_json::text, 'UTF8')) <= 16777216",
            name=op.f("ck_grading_run_f6_manifest_size"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(f7_manifest_json::text, 'UTF8')) <= 33554432",
            name=op.f("ck_grading_run_f7_manifest_size"),
        ),
        _hash_check("grading_run", "config_fingerprint"),
        _hash_check("grading_run", "f3_manifest_fingerprint"),
        _hash_check("grading_run", "f6_manifest_fingerprint"),
        _hash_check("grading_run", "f7_manifest_fingerprint"),
        _hash_check("grading_run", "input_fingerprint"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name=op.f("ck_grading_run_status_values"),
        ),
        sa.CheckConstraint(
            "deterministic_item_count >= 0 AND correlation_item_count >= 0 "
            "AND high_attention_count >= 0 AND manual_attention_count >= 0 "
            "AND cleared_count >= 0",
            name=op.f("ck_grading_run_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "deterministic_item_count + correlation_item_count = "
            "high_attention_count + manual_attention_count + cleared_count",
            name=op.f("ck_grading_run_counts_arithmetic_consistent"),
        ),
        sa.CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name=op.f("ck_grading_run_completion_consistent"),
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name=op.f("ck_grading_run_completed_after_created"),
        ),
    )
    op.create_index(
        "ix_grading_run_tenant_file_created",
        "grading_run",
        ["tenant_id", "file_version_id", "created_at"],
    )
    op.create_index("ix_grading_run_tenant_id", "grading_run", ["tenant_id"])


def _create_request() -> None:
    op.create_table(
        "grading_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grading_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _tenant_fk("grading_request"),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grading_request")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_grading_request_tenant_idempotency_key",
        ),
        _hash_check("grading_request", "input_fingerprint"),
        _hash_check("grading_request", "idempotency_key_hash"),
        _hash_check("grading_request", "request_fingerprint"),
    )
    op.create_index("ix_grading_request_run_id", "grading_request", ["grading_run_id"])
    op.create_index("ix_grading_request_tenant_id", "grading_request", ["tenant_id"])


def _run_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
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


def _create_item() -> None:
    op.create_table(
        "grading_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grading_run_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=True),
        sa.Column("correlation_finding_id", sa.Uuid(), nullable=True),
        sa.Column("capability_declaration_id", sa.Uuid(), nullable=True),
        sa.Column("investigation_run_id", sa.Uuid(), nullable=True),
        sa.Column("investigation_result_id", sa.Uuid(), nullable=True),
        sa.Column("rule_kind", sa.String(64), nullable=True),
        sa.Column("f3_outcome", sa.String(32), nullable=True),
        sa.Column("f3_evidence_fingerprint", sa.String(64), nullable=True),
        sa.Column("detector", sa.String(64), nullable=True),
        sa.Column("f6_detector_version", sa.String(32), nullable=True),
        sa.Column("f6_finding_key", sa.String(64), nullable=True),
        sa.Column("f6_config_fingerprint", sa.String(64), nullable=True),
        sa.Column("f6_capability_status", sa.String(32), nullable=True),
        sa.Column("f6_evidence_fingerprint", sa.String(64), nullable=True),
        sa.Column("f7_outcome", sa.String(32), nullable=True),
        sa.Column("f7_evidence_sufficient", sa.Boolean(), nullable=True),
        sa.Column("f7_input_fingerprint", sa.String(64), nullable=True),
        sa.Column("f7_config_fingerprint", sa.String(64), nullable=True),
        sa.Column("f7_result_fingerprint", sa.String(64), nullable=True),
        sa.Column("f7_not_run_reason_code", sa.String(64), nullable=True),
        sa.Column("severity_impact", sa.Integer(), nullable=False),
        sa.Column("severity_confidence", sa.Integer(), nullable=False),
        sa.Column("disposition", sa.String(32), nullable=False),
        sa.Column("reason_codes_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("item_fingerprint", sa.String(64), nullable=False),
        sa.Column("first_row_no", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _tenant_fk("grading_item"),
        _run_fk("fk_grading_item_run_identity"),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grading_item")),
        sa.UniqueConstraint(
            "id",
            "grading_run_id",
            "file_version_id",
            "tenant_id",
            name="uq_grading_item_identity",
        ),
        sa.UniqueConstraint(
            "grading_run_id", "item_fingerprint", name="uq_grading_item_run_fingerprint"
        ),
        sa.CheckConstraint(
            "source_kind IN ('deterministic', 'correlation')",
            name=op.f("ck_grading_item_source_kind_values"),
        ),
        sa.CheckConstraint(
            "(source_kind = 'deterministic' AND finding_id IS NOT NULL "
            "AND correlation_finding_id IS NULL AND capability_declaration_id IS NULL "
            "AND investigation_run_id IS NULL AND investigation_result_id IS NULL "
            "AND rule_kind IS NOT NULL AND f3_outcome IN ('flagged', 'unavailable') "
            "AND f3_evidence_fingerprint IS NOT NULL AND detector IS NULL "
            "AND f6_detector_version IS NULL AND f6_finding_key IS NULL "
            "AND f6_config_fingerprint IS NULL AND f6_capability_status IS NULL "
            "AND f6_evidence_fingerprint IS NULL AND f7_outcome IS NULL "
            "AND f7_evidence_sufficient IS NULL AND f7_input_fingerprint IS NULL "
            "AND f7_config_fingerprint IS NULL AND f7_result_fingerprint IS NULL "
            "AND f7_not_run_reason_code IS NULL) OR "
            "(source_kind = 'correlation' AND finding_id IS NULL "
            "AND correlation_finding_id IS NOT NULL AND rule_kind IS NULL "
            "AND f3_outcome IS NULL AND f3_evidence_fingerprint IS NULL "
            "AND capability_declaration_id IS NOT NULL AND detector IS NOT NULL "
            "AND f6_detector_version IS NOT NULL AND f6_finding_key IS NOT NULL "
            "AND f6_config_fingerprint IS NOT NULL AND f6_capability_status IS NOT NULL "
            "AND f6_evidence_fingerprint IS NOT NULL AND f7_outcome IS NOT NULL)",
            name=op.f("ck_grading_item_source_fields_consistent"),
        ),
        sa.CheckConstraint(
            "rule_kind IS NULL OR rule_kind IN "
            "('limit', 'invoice_type', 'timeliness', 'invoice_title', 'invoice_duplicate')",
            name=op.f("ck_grading_item_rule_kind_values"),
        ),
        sa.CheckConstraint(
            "detector IS NULL OR detector IN "
            "('split_invoice', 'sequential_invoice', 'frequency_anomaly', "
            "'spatiotemporal_tier0')",
            name=op.f("ck_grading_item_detector_values"),
        ),
        sa.CheckConstraint(
            "f6_capability_status IS NULL OR "
            "f6_capability_status IN ('enabled', 'degraded', 'unavailable')",
            name=op.f("ck_grading_item_capability_values"),
        ),
        sa.CheckConstraint(
            "f7_outcome IS NULL OR f7_outcome IN "
            "('sufficient', 'insufficient', 'unavailable', 'max_steps', 'failed', 'not_run')",
            name=op.f("ck_grading_item_f7_outcome_values"),
        ),
        sa.CheckConstraint(
            "f7_outcome IS NULL OR "
            "(f7_outcome = 'sufficient' AND f7_evidence_sufficient IS TRUE "
            "AND investigation_run_id IS NOT NULL AND investigation_result_id IS NOT NULL "
            "AND f7_input_fingerprint IS NOT NULL AND f7_config_fingerprint IS NOT NULL "
            "AND f7_result_fingerprint IS NOT NULL AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome = 'insufficient' AND f7_evidence_sufficient IS FALSE "
            "AND investigation_run_id IS NOT NULL AND investigation_result_id IS NOT NULL "
            "AND f7_input_fingerprint IS NOT NULL AND f7_config_fingerprint IS NOT NULL "
            "AND f7_result_fingerprint IS NOT NULL AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome IN ('unavailable', 'max_steps', 'failed') "
            "AND f7_evidence_sufficient IS NULL AND investigation_run_id IS NOT NULL "
            "AND investigation_result_id IS NOT NULL AND f7_input_fingerprint IS NOT NULL "
            "AND f7_config_fingerprint IS NOT NULL AND f7_result_fingerprint IS NOT NULL "
            "AND f7_not_run_reason_code IS NULL) OR "
            "(f7_outcome = 'not_run' AND f7_evidence_sufficient IS NULL "
            "AND investigation_run_id IS NULL AND investigation_result_id IS NULL "
            "AND f7_input_fingerprint IS NULL AND f7_config_fingerprint IS NULL "
            "AND f7_result_fingerprint IS NULL "
            "AND f7_not_run_reason_code = 'INVESTIGATION_NOT_RUN')",
            name=op.f("ck_grading_item_f7_terminal_consistent"),
        ),
        sa.CheckConstraint(
            "severity_impact BETWEEN 0 AND 3", name=op.f("ck_grading_item_impact_range")
        ),
        sa.CheckConstraint(
            "severity_confidence BETWEEN 0 AND 3",
            name=op.f("ck_grading_item_confidence_range"),
        ),
        sa.CheckConstraint(
            "disposition IN ('high_attention', 'manual_attention', 'cleared')",
            name=op.f("ck_grading_item_disposition_values"),
        ),
        _hash_check("grading_item", "item_fingerprint"),
        sa.CheckConstraint(
            "f3_evidence_fingerprint IS NULL OR f3_evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f3_evidence_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "f6_finding_key IS NULL OR f6_finding_key ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f6_finding_key_format"),
        ),
        sa.CheckConstraint(
            "f6_config_fingerprint IS NULL OR f6_config_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f6_config_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "f6_evidence_fingerprint IS NULL OR f6_evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f6_evidence_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "f7_input_fingerprint IS NULL OR f7_input_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f7_input_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "f7_config_fingerprint IS NULL OR f7_config_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f7_config_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "f7_result_fingerprint IS NULL OR f7_result_fingerprint ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_grading_item_f7_result_fingerprint_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reason_codes_json) = 'array'",
            name=op.f("ck_grading_item_reason_codes_array"),
        ),
        sa.CheckConstraint(
            "jsonb_array_length(reason_codes_json) BETWEEN 1 AND 16",
            name=op.f("ck_grading_item_reason_codes_count"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(reason_codes_json::text, 'UTF8')) <= 8192",
            name=op.f("ck_grading_item_reason_codes_size"),
        ),
        sa.CheckConstraint(
            "f8_reason_codes_valid(reason_codes_json)",
            name=op.f("ck_grading_item_reason_codes_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_snapshot) = 'object'",
            name=op.f("ck_grading_item_evidence_object"),
        ),
        sa.CheckConstraint(
            "octet_length(convert_to(evidence_snapshot::text, 'UTF8')) <= 262144",
            name=op.f("ck_grading_item_evidence_size"),
        ),
        sa.CheckConstraint("first_row_no > 0", name=op.f("ck_grading_item_first_row_no_positive")),
    )
    op.create_index(
        "uq_grading_item_deterministic_source",
        "grading_item",
        ["grading_run_id", "finding_id"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'deterministic'"),
    )
    op.create_index(
        "uq_grading_item_correlation_source",
        "grading_item",
        ["grading_run_id", "correlation_finding_id"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'correlation'"),
    )
    op.create_index(
        "ix_grading_item_run_first_row", "grading_item", ["grading_run_id", "first_row_no"]
    )
    op.create_index("ix_grading_item_tenant_id", "grading_item", ["tenant_id"])


def _create_item_row() -> None:
    op.create_table(
        "grading_item_row",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grading_run_id", sa.Uuid(), nullable=False),
        sa.Column("grading_item_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_row_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        _tenant_fk("grading_item_row"),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["file_version_id", "row_no", "tenant_id"],
            ["expense_row.file_version_id", "expense_row.row_no", "expense_row.tenant_id"],
            name="fk_grading_item_row_expense_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grading_item_row")),
        sa.UniqueConstraint("grading_item_id", "ordinal", name="uq_grading_item_row_item_ordinal"),
        sa.UniqueConstraint("grading_item_id", "row_no", name="uq_grading_item_row_item_row"),
        sa.CheckConstraint("row_no > 0", name=op.f("ck_grading_item_row_row_no_positive")),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_grading_item_row_ordinal_positive")),
        _hash_check("grading_item_row", "source_row_fingerprint"),
    )
    op.create_index("ix_grading_item_row_run_row", "grading_item_row", ["grading_run_id", "row_no"])
    op.create_index("ix_grading_item_row_tenant_id", "grading_item_row", ["tenant_id"])


def _create_snapshot_validation() -> None:
    op.execute(
        """
        CREATE FUNCTION f8_validate_grading_snapshot(target_run uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
          run_row grading_run%ROWTYPE;
          actual_deterministic integer;
          actual_correlation integer;
          actual_high integer;
          actual_manual integer;
          actual_cleared integer;
        BEGIN
          SELECT * INTO run_row FROM grading_run WHERE id = target_run;
          IF NOT FOUND THEN RETURN; END IF;
          IF run_row.status <> 'completed' THEN
            RAISE EXCEPTION 'F8_SNAPSHOT_INCOMPLETE';
          END IF;

          SELECT
            count(*) FILTER (WHERE source_kind = 'deterministic'),
            count(*) FILTER (WHERE source_kind = 'correlation'),
            count(*) FILTER (WHERE disposition = 'high_attention'),
            count(*) FILTER (WHERE disposition = 'manual_attention'),
            count(*) FILTER (WHERE disposition = 'cleared')
          INTO actual_deterministic, actual_correlation, actual_high, actual_manual, actual_cleared
          FROM grading_item WHERE grading_run_id = target_run;

          IF (actual_deterministic, actual_correlation, actual_high, actual_manual, actual_cleared)
             IS DISTINCT FROM
             (run_row.deterministic_item_count, run_row.correlation_item_count,
              run_row.high_attention_count, run_row.manual_attention_count,
              run_row.cleared_count) THEN
            RAISE EXCEPTION 'F8_SNAPSHOT_COUNT_MISMATCH';
          END IF;

          IF EXISTS (
            (SELECT id FROM finding
             WHERE validation_run_id = run_row.validation_run_id
               AND evidence_json->>'outcome' IN ('flagged', 'unavailable')
             EXCEPT
             SELECT finding_id FROM grading_item
             WHERE grading_run_id = target_run AND source_kind = 'deterministic')
            UNION ALL
            (SELECT finding_id FROM grading_item
             WHERE grading_run_id = target_run AND source_kind = 'deterministic'
             EXCEPT
             SELECT id FROM finding
             WHERE validation_run_id = run_row.validation_run_id
               AND evidence_json->>'outcome' IN ('flagged', 'unavailable'))
          ) THEN RAISE EXCEPTION 'F8_F3_SOURCE_SET_MISMATCH'; END IF;

          IF EXISTS (
            (SELECT id FROM correlation_finding WHERE detection_run_id = run_row.detection_run_id
             EXCEPT
             SELECT correlation_finding_id FROM grading_item
             WHERE grading_run_id = target_run AND source_kind = 'correlation')
            UNION ALL
            (SELECT correlation_finding_id FROM grading_item
             WHERE grading_run_id = target_run AND source_kind = 'correlation'
             EXCEPT
             SELECT id FROM correlation_finding WHERE detection_run_id = run_row.detection_run_id)
          ) THEN RAISE EXCEPTION 'F8_F6_SOURCE_SET_MISMATCH'; END IF;

          IF EXISTS (
            SELECT 1 FROM grading_item item
            LEFT JOIN finding source ON source.id = item.finding_id
            WHERE item.grading_run_id = target_run AND item.source_kind = 'deterministic'
              AND (item.f3_outcome IS DISTINCT FROM source.evidence_json->>'outcome'
                   OR (SELECT count(*) FROM grading_item_row row_item
                       WHERE row_item.grading_item_id = item.id) <> 1
                   OR NOT EXISTS (
                       SELECT 1 FROM grading_item_row row_item
                       WHERE row_item.grading_item_id = item.id
                         AND row_item.row_no = source.row_no))
          ) THEN RAISE EXCEPTION 'F8_F3_ROW_OR_OUTCOME_MISMATCH'; END IF;

          IF EXISTS (
            SELECT 1 FROM grading_item item
            WHERE item.grading_run_id = target_run AND item.source_kind = 'correlation'
              AND EXISTS (
                (SELECT row_no FROM correlation_finding_row
                 WHERE finding_id = item.correlation_finding_id
                 EXCEPT
                 SELECT row_no FROM grading_item_row WHERE grading_item_id = item.id)
                UNION ALL
                (SELECT row_no FROM grading_item_row WHERE grading_item_id = item.id
                 EXCEPT
                 SELECT row_no FROM correlation_finding_row
                 WHERE finding_id = item.correlation_finding_id))
          ) THEN RAISE EXCEPTION 'F8_F6_ROW_SET_MISMATCH'; END IF;

          IF EXISTS (
            SELECT 1 FROM grading_item item
            LEFT JOIN LATERAL (
              SELECT count(*) AS row_count, min(row_no) AS min_row,
                     min(ordinal) AS min_ordinal, max(ordinal) AS max_ordinal,
                     count(DISTINCT ordinal) AS distinct_ordinal
              FROM grading_item_row WHERE grading_item_id = item.id
            ) rows ON true
            WHERE item.grading_run_id = target_run
              AND (rows.row_count = 0 OR item.first_row_no <> rows.min_row
                   OR rows.min_ordinal <> 1 OR rows.max_ordinal <> rows.row_count
                   OR rows.distinct_ordinal <> rows.row_count)
          ) THEN RAISE EXCEPTION 'F8_ITEM_ROW_ORDINAL_MISMATCH'; END IF;

          IF EXISTS (
            SELECT 1 FROM grading_item item
            JOIN capability_declaration capability ON capability.id = item.capability_declaration_id
            WHERE item.grading_run_id = target_run AND item.source_kind = 'correlation'
              AND (item.f6_capability_status IS DISTINCT FROM capability.status
                   OR item.f6_config_fingerprint IS DISTINCT FROM capability.config_fingerprint
                   OR item.f6_detector_version IS DISTINCT FROM capability.detector_version)
          ) THEN RAISE EXCEPTION 'F8_CAPABILITY_SNAPSHOT_MISMATCH'; END IF;

          IF EXISTS (
            SELECT 1 FROM grading_item item
            JOIN investigation_run investigation ON investigation.id = item.investigation_run_id
            JOIN investigation_result result ON result.id = item.investigation_result_id
            WHERE item.grading_run_id = target_run AND item.f7_outcome <> 'not_run'
              AND (item.f7_outcome IS DISTINCT FROM result.outcome
                   OR item.f7_evidence_sufficient IS DISTINCT FROM result.evidence_sufficient
                   OR item.f7_result_fingerprint IS DISTINCT FROM result.result_fingerprint
                   OR item.f7_input_fingerprint IS DISTINCT FROM investigation.input_fingerprint
                   OR item.f7_config_fingerprint IS DISTINCT FROM investigation.config_fingerprint)
          ) THEN RAISE EXCEPTION 'F8_F7_SNAPSHOT_MISMATCH'; END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_prepare_validation_cache()
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
          CREATE TEMP TABLE IF NOT EXISTS f8_validated_grading_run(
            grading_run_id uuid PRIMARY KEY
          ) ON COMMIT DELETE ROWS;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_invalidate_grading_snapshot(target_run uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_prepare_validation_cache();
          DELETE FROM pg_temp.f8_validated_grading_run WHERE grading_run_id = target_run;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_validate_grading_snapshot_once(target_run uuid)
        RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_prepare_validation_cache();
          IF NOT EXISTS (
            SELECT 1 FROM pg_temp.f8_validated_grading_run WHERE grading_run_id = target_run
          ) THEN
            PERFORM f8_validate_grading_snapshot(target_run);
            INSERT INTO pg_temp.f8_validated_grading_run(grading_run_id)
            VALUES (target_run) ON CONFLICT DO NOTHING;
          END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_invalidate_grading_snapshot_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_invalidate_grading_snapshot(NEW.grading_run_id);
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_invalidate_grading_run_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_invalidate_grading_snapshot(NEW.id);
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_validate_grading_snapshot_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_validate_grading_snapshot_once(NEW.grading_run_id);
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION f8_validate_grading_run_trigger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          PERFORM f8_validate_grading_snapshot_once(NEW.id);
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_grading_run_snapshot_invalidate AFTER INSERT ON grading_run "
        "FOR EACH ROW EXECUTE FUNCTION f8_invalidate_grading_run_trigger()"
    )
    op.execute(
        "CREATE TRIGGER trg_grading_item_snapshot_invalidate AFTER INSERT ON grading_item "
        "FOR EACH ROW EXECUTE FUNCTION f8_invalidate_grading_snapshot_trigger()"
    )
    op.execute(
        "CREATE TRIGGER trg_grading_item_row_snapshot_invalidate AFTER INSERT ON grading_item_row "
        "FOR EACH ROW EXECUTE FUNCTION f8_invalidate_grading_snapshot_trigger()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_grading_run_snapshot_consistent "
        "AFTER INSERT ON grading_run DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION f8_validate_grading_run_trigger()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_grading_item_snapshot_consistent "
        "AFTER INSERT ON grading_item DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION f8_validate_grading_snapshot_trigger()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_grading_item_row_snapshot_consistent "
        "AFTER INSERT ON grading_item_row DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION f8_validate_grading_snapshot_trigger()"
    )


def _create_immutable_triggers() -> None:
    op.execute(
        """
        CREATE FUNCTION f8_reject_update_delete()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'F8_IMMUTABLE_FACT';
        END;
        $$
        """
    )
    for table in (
        "grading_config",
        "grading_run",
        "grading_request",
        "grading_item",
        "grading_item_row",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION f8_reject_update_delete()"
        )


def upgrade() -> None:
    _create_reason_validator()
    _add_source_snapshot_constraints()
    _create_config()
    _create_run()
    _create_request()
    _create_item()
    _create_item_row()
    _create_snapshot_validation()
    _create_immutable_triggers()


def _guard_downgrade() -> None:
    protected = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM grading_config) "
            "OR EXISTS (SELECT 1 FROM grading_run) "
            "OR EXISTS (SELECT 1 FROM grading_request) "
            "OR EXISTS (SELECT 1 FROM grading_item) "
            "OR EXISTS (SELECT 1 FROM grading_item_row)"
        )
    )
    if protected:
        raise RuntimeError(
            "cannot downgrade 0010 while F8 config, run, request, item, or row facts exist; "
            "restore the verified pre-0010 backup into an isolated database"
        )


def downgrade() -> None:
    _guard_downgrade()
    op.execute("DROP TRIGGER trg_grading_item_row_snapshot_consistent ON grading_item_row")
    op.execute("DROP TRIGGER trg_grading_item_snapshot_consistent ON grading_item")
    op.execute("DROP TRIGGER trg_grading_run_snapshot_consistent ON grading_run")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_grading_item_row_snapshot_invalidate ON grading_item_row"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_grading_item_snapshot_invalidate ON grading_item")
    op.execute("DROP TRIGGER IF EXISTS trg_grading_run_snapshot_invalidate ON grading_run")
    for table in (
        "grading_item_row",
        "grading_item",
        "grading_request",
        "grading_run",
        "grading_config",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_immutable ON {table}")
    op.drop_table("grading_item_row")
    op.drop_table("grading_item")
    op.drop_table("grading_request")
    op.drop_table("grading_run")
    op.drop_table("grading_config")
    op.execute("DROP FUNCTION f8_validate_grading_snapshot_trigger()")
    op.execute("DROP FUNCTION f8_validate_grading_run_trigger()")
    op.execute("DROP FUNCTION IF EXISTS f8_invalidate_grading_snapshot_trigger()")
    op.execute("DROP FUNCTION IF EXISTS f8_invalidate_grading_run_trigger()")
    op.execute("DROP FUNCTION IF EXISTS f8_validate_grading_snapshot_once(uuid)")
    op.execute("DROP FUNCTION IF EXISTS f8_invalidate_grading_snapshot(uuid)")
    op.execute("DROP FUNCTION IF EXISTS f8_prepare_validation_cache()")
    op.execute("DROP FUNCTION f8_validate_grading_snapshot(uuid)")
    op.execute("DROP FUNCTION f8_reject_update_delete()")
    op.execute("DROP FUNCTION f8_reason_codes_valid(jsonb)")
    op.drop_constraint(
        "uq_investigation_result_f8_snapshot", "investigation_result", type_="unique"
    )
    op.drop_constraint(
        "uq_capability_declaration_f8_snapshot", "capability_declaration", type_="unique"
    )
    op.drop_constraint("uq_correlation_finding_f8_snapshot", "correlation_finding", type_="unique")
    op.drop_constraint("uq_finding_f8_source_identity", "finding", type_="unique")
