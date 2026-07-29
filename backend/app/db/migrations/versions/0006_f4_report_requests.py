"""Add the CP-F4.3 report request ledger and policy-change revisions.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    """Install the CP-F4.3 persistence additions without rewriting old migrations."""
    op.drop_constraint(
        op.f("ck_file_version_revision_reason_values"),
        "file_version",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_file_version_revision_reason_values"),
        "file_version",
        "revision_reason IS NULL OR revision_reason IN "
        "('ruleset_change', 'mapping_change', 'policy_change')",
    )

    op.create_table(
        "report_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("report_run_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_report_request_file_version_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_run_id", "tenant_id", "file_version_id"],
            ["report_run.id", "report_run.tenant_id", "report_run.file_version_id"],
            name="fk_report_request_report_tenant_file",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_report_request_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_request")),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name=op.f("uq_report_request_tenant_id_idempotency_key_hash"),
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key_hash) = 64",
            name=op.f("ck_report_request_idempotency_hash_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name=op.f("ck_report_request_request_fingerprint_length"),
        ),
    )
    op.create_index(op.f("ix_report_request_tenant_id"), "report_request", ["tenant_id"])
    op.create_index(
        op.f("ix_report_request_file_version_id"),
        "report_request",
        ["file_version_id"],
    )
    op.create_index(
        op.f("ix_report_request_report_run_id"),
        "report_request",
        ["report_run_id"],
    )
    op.execute(
        """
        CREATE TRIGGER trg_report_request_immutable
        BEFORE UPDATE OR DELETE ON report_request
        FOR EACH ROW EXECUTE FUNCTION f4_reject_update_delete();
        """
    )


def _guard_downgrade() -> None:
    protected_data = op.get_bind().scalar(
        sa.text(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM file_version
                    WHERE revision_reason = 'policy_change'
                )
                OR EXISTS (SELECT 1 FROM report_request)
            """
        )
    )
    if protected_data:
        raise RuntimeError(
            "cannot downgrade 0006 while policy_change revisions or report request "
            "evidence exists; restore the verified pre-0006 backup into an isolated database"
        )


def downgrade() -> None:
    """Return to 0005 only when no CP-F4.3 delivery evidence would be destroyed."""
    _guard_downgrade()

    op.execute("DROP TRIGGER IF EXISTS trg_report_request_immutable ON report_request")
    op.drop_table("report_request")

    op.drop_constraint(
        op.f("ck_file_version_revision_reason_values"),
        "file_version",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_file_version_revision_reason_values"),
        "file_version",
        "revision_reason IS NULL OR revision_reason IN ('ruleset_change', 'mapping_change')",
    )
