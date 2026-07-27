"""F2 schema mapping versions and structured parsing storage.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-27

The migration is deliberately additive. In particular, it preserves every
UNIQUE/CHECK constraint created by 0001 and 0002, including the legacy
schema_mapping uniqueness constraint.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import RowMapping

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_THRESHOLDS = {
    "available_min_non_null_rate": "0.8000",
    "inferred_min_success_rate": "0.8000",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _header_signature(source_columns: list[str]) -> str:
    # F1 already strips and deduplicates headers. Sorting makes the signature
    # independent of JSONB key order and original Excel column order.
    return _sha256_json(sorted(source_columns))


def _legacy_fingerprint(mappings: list[dict[str, str]]) -> str:
    return _sha256_json(
        {
            "availability_thresholds": _DEFAULT_THRESHOLDS,
            "currency_aliases": {},
            "inference_config": {"rules": []},
            "mappings": sorted(
                mappings,
                key=lambda item: (item["source_column"], item["target_field"]),
            ),
        }
    )


def _backfill_legacy_mappings() -> None:
    """Create one immutable parent version for each legacy tenant/version group."""
    bind = op.get_bind()

    duplicate_target = (
        bind.execute(
            sa.text(
                """
            SELECT tenant_id, version, target_field, count(*) AS row_count
            FROM schema_mapping
            GROUP BY tenant_id, version, target_field
            HAVING count(*) > 1
            LIMIT 1
            """
            )
        )
        .mappings()
        .one_or_none()
    )
    if duplicate_target is not None:
        raise RuntimeError(
            "legacy schema_mapping contains duplicate target fields for "
            f"tenant={duplicate_target['tenant_id']} version={duplicate_target['version']} "
            f"target={duplicate_target['target_field']}"
        )

    rows: Sequence[RowMapping] = (
        bind.execute(
            sa.text(
                """
            SELECT tenant_id, version, source_column, target_field, created_at
            FROM schema_mapping
            ORDER BY tenant_id, version, source_column
            """
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[tuple[uuid.UUID, int], list[RowMapping]] = defaultdict(list)
    for row in rows:
        grouped[(row["tenant_id"], row["version"])].append(row)

    version_table = sa.table(
        "schema_mapping_version",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("header_signature", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("config_fingerprint", sa.String()),
        sa.column("availability_thresholds", postgresql.JSONB()),
        sa.column("currency_aliases", postgresql.JSONB()),
        sa.column("inference_config", postgresql.JSONB()),
        sa.column("backfilled_legacy", sa.Boolean()),
        sa.column("created_by", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    for (tenant_id, version), group_rows in grouped.items():
        mappings = [
            {
                "source_column": str(row["source_column"]),
                "target_field": str(row["target_field"]),
            }
            for row in group_rows
        ]
        mapping_version_id = uuid.uuid4()
        op.bulk_insert(
            version_table,
            [
                {
                    "id": mapping_version_id,
                    "tenant_id": tenant_id,
                    "header_signature": _header_signature(
                        [item["source_column"] for item in mappings]
                    ),
                    "version": version,
                    "config_fingerprint": _legacy_fingerprint(mappings),
                    "availability_thresholds": _DEFAULT_THRESHOLDS,
                    "currency_aliases": {},
                    "inference_config": {"rules": []},
                    "backfilled_legacy": True,
                    "created_by": None,
                    "created_at": min(row["created_at"] for row in group_rows),
                }
            ],
        )
        bind.execute(
            sa.text(
                """
                UPDATE schema_mapping
                SET mapping_version_id = :mapping_version_id
                WHERE tenant_id = :tenant_id AND version = :version
                """
            ),
            {
                "mapping_version_id": mapping_version_id,
                "tenant_id": tenant_id,
                "version": version,
            },
        )


def upgrade() -> None:
    """Add F2 versioned mappings and structured parsing columns."""
    op.create_table(
        "schema_mapping_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("header_signature", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "availability_thresholds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("currency_aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("inference_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("backfilled_legacy", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_schema_mapping_version_created_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_schema_mapping_version_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schema_mapping_version")),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name=op.f("uq_schema_mapping_version_tenant_id_version"),
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            name="uq_schema_mapping_version_id_tenant_id",
        ),
    )
    op.create_index(
        op.f("ix_schema_mapping_version_tenant_id"),
        "schema_mapping_version",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_schema_mapping_version_tenant_id_header_signature",
        "schema_mapping_version",
        ["tenant_id", "header_signature"],
        unique=False,
    )

    op.add_column("schema_mapping", sa.Column("mapping_version_id", sa.Uuid(), nullable=True))
    _backfill_legacy_mappings()
    op.alter_column("schema_mapping", "mapping_version_id", nullable=False)
    op.create_foreign_key(
        "fk_schema_mapping_mapping_version_tenant",
        "schema_mapping",
        "schema_mapping_version",
        ["mapping_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        op.f("uq_schema_mapping_mapping_version_id_source_column"),
        "schema_mapping",
        ["mapping_version_id", "source_column"],
    )
    op.create_unique_constraint(
        op.f("uq_schema_mapping_mapping_version_id_target_field"),
        "schema_mapping",
        ["mapping_version_id", "target_field"],
    )
    op.create_index(
        op.f("ix_schema_mapping_mapping_version_id"),
        "schema_mapping",
        ["mapping_version_id"],
        unique=False,
    )

    op.add_column("file_version", sa.Column("mapping_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "file_version",
        sa.Column(
            "parse_status",
            sa.Enum(
                "unparsed",
                "parsed",
                "parsed_with_errors",
                "failed",
                name="parse_status_enum",
                native_enum=False,
            ),
            server_default="unparsed",
            nullable=False,
        ),
    )
    op.add_column("file_version", sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_file_version_mapping_version_tenant",
        "file_version",
        "schema_mapping_version",
        ["mapping_version_id", "tenant_id"],
        ["id", "tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_file_version_parse_status_values"),
        "file_version",
        "parse_status IN ('unparsed', 'parsed', 'parsed_with_errors', 'failed')",
    )
    op.create_index(
        op.f("ix_file_version_mapping_version_id"),
        "file_version",
        ["mapping_version_id"],
        unique=False,
    )

    op.add_column(
        "expense_row",
        sa.Column("normalized_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("expense_row", sa.Column("parse_error_code", sa.String(length=64), nullable=True))
    op.add_column(
        "expense_row",
        sa.Column("parse_error_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Return to the 0002 schema; F2 materialized data is intentionally discarded."""
    op.drop_column("expense_row", "parse_error_detail")
    op.drop_column("expense_row", "parse_error_code")
    op.drop_column("expense_row", "normalized_json")

    op.drop_index(op.f("ix_file_version_mapping_version_id"), table_name="file_version")
    op.drop_constraint(
        op.f("ck_file_version_parse_status_values"),
        "file_version",
        type_="check",
    )
    op.drop_constraint(
        "fk_file_version_mapping_version_tenant",
        "file_version",
        type_="foreignkey",
    )
    op.drop_column("file_version", "parsed_at")
    op.drop_column("file_version", "parse_status")
    op.drop_column("file_version", "mapping_version_id")

    op.drop_index(op.f("ix_schema_mapping_mapping_version_id"), table_name="schema_mapping")
    op.drop_constraint(
        op.f("uq_schema_mapping_mapping_version_id_target_field"),
        "schema_mapping",
        type_="unique",
    )
    op.drop_constraint(
        op.f("uq_schema_mapping_mapping_version_id_source_column"),
        "schema_mapping",
        type_="unique",
    )
    op.drop_constraint(
        "fk_schema_mapping_mapping_version_tenant",
        "schema_mapping",
        type_="foreignkey",
    )
    op.drop_column("schema_mapping", "mapping_version_id")

    op.drop_index(
        "ix_schema_mapping_version_tenant_id_header_signature",
        table_name="schema_mapping_version",
    )
    op.drop_index(
        op.f("ix_schema_mapping_version_tenant_id"),
        table_name="schema_mapping_version",
    )
    op.drop_table("schema_mapping_version")
