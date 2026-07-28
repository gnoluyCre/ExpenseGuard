"""F3 确定性校验快照与冻结依赖。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, str_enum, uuid_pk


class ValidationRunStatus(StrEnum):
    """持久化校验批次的最小状态域。"""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class ValidationRun(Base, TenantScopedMixin, TimestampMixin):
    """首次成功校验冻结的映射与规则集快照。"""

    __tablename__ = "validation_run"
    __table_args__ = (
        UniqueConstraint("file_version_id"),
        UniqueConstraint("id", "tenant_id", name="uq_validation_run_id_tenant_id"),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_validation_run_id_tenant_id_file_version_id",
        ),
        ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_validation_run_file_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_validation_run_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["triggered_by", "tenant_id"],
            ["app_user.id", "app_user.tenant_id"],
            name="fk_validation_run_triggered_by_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("status IN ('in_progress', 'completed')", name="status_values"),
        CheckConstraint(
            "total_row_count >= 0 AND evaluated_row_count >= 0 "
            "AND passed_count >= 0 AND flagged_count >= 0 "
            "AND manual_review_count >= 0 AND parse_failed_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "total_row_count = evaluated_row_count + parse_failed_count",
            name="total_count_consistent",
        ),
        CheckConstraint(
            "evaluated_row_count = passed_count + flagged_count + manual_review_count",
            name="evaluated_count_consistent",
        ),
        CheckConstraint(
            "(status = 'in_progress' AND completed_at IS NULL) OR "
            "(status = 'completed' AND completed_at IS NOT NULL)",
            name="completion_consistent",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    ruleset_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ruleset_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[ValidationRunStatus] = mapped_column(
        str_enum(ValidationRunStatus, "validation_run_status_enum"), nullable=False
    )
    total_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluated_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    passed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flagged_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manual_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parse_failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dependencies: Mapped[list["ValidationDependency"]] = relationship(
        back_populates="validation_run",
        foreign_keys="[ValidationDependency.validation_run_id, ValidationDependency.tenant_id]",
    )


class ValidationDependency(Base, TenantScopedMixin, TimestampMixin):
    """一次校验冻结的租户历史查重输入。"""

    __tablename__ = "validation_dependency"
    __table_args__ = (
        UniqueConstraint(
            "validation_run_id",
            "depended_file_version_id",
            name="uq_validation_dependency_run_file",
        ),
        ForeignKeyConstraint(
            ["validation_run_id", "tenant_id"],
            ["validation_run.id", "validation_run.tenant_id"],
            name="fk_validation_dependency_validation_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["depended_file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_validation_dependency_file_version_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_validation_dependency_depended_file_version_id", "depended_file_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    validation_run_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    depended_file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    validation_run: Mapped[ValidationRun] = relationship(
        back_populates="dependencies",
        foreign_keys="[ValidationDependency.validation_run_id, ValidationDependency.tenant_id]",
    )
