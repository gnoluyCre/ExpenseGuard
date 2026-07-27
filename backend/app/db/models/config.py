"""配置类实体:列名映射与规则配置。

设计原则:所有本可硬编码之处一律数据驱动——阈值与白名单是**配置**不是代码，
因为制度会变，且预期会有第二家企业。
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, uuid_pk


class SchemaMappingVersion(Base, TenantScopedMixin, TimestampMixin):
    """不可变的列映射配置版本。

    `version` 在租户内全局单调递增。这个作用域同时兼容 Phase 1
    `schema_mapping` 上受保护的 `(tenant_id, source_column, version)` 唯一约束。
    """

    __tablename__ = "schema_mapping_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version"),
        UniqueConstraint("id", "tenant_id", name="uq_schema_mapping_version_id_tenant_id"),
        Index(
            "ix_schema_mapping_version_tenant_id_header_signature",
            "tenant_id",
            "header_signature",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    header_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    availability_thresholds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    currency_aliases: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    inference_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    backfilled_legacy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )


class SchemaMapping(Base, TenantScopedMixin, TimestampMixin):
    """Excel 列名 → 内部统一字段的映射。

    一次性配置，之后每月导入直接复用。这是「接入新企业不改代码」的第一环。
    """

    __tablename__ = "schema_mapping"
    __table_args__ = (
        # 0002 创建的受保护约束，后续迁移不得删除或放宽。
        UniqueConstraint("tenant_id", "source_column", "version"),
        UniqueConstraint("mapping_version_id", "source_column"),
        UniqueConstraint("mapping_version_id", "target_field"),
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_schema_mapping_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    mapping_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    source_column: Mapped[str] = mapped_column(String(255), nullable=False)
    target_field: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 与父版本号保持一致；保留该列是为了维持 0002 的受保护唯一约束。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: AI 建议的置信度（人工确认后可为空）
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)


class RuleConfig(Base, TenantScopedMixin, TimestampMixin):
    """确定性校验规则 —— 阈值与白名单。

    **规则即数据。** definition 用 JSON Logic / 决策表表达，
    制度变更时改配置而非改代码，研发无需介入。

    `unique(tenant_id, rule_id, version)` 保证规则**版本化**：
    每条判定结果都引用具体的规则版本，这是「相同输入 + 相同规则版本
    → 相同输出」这一可复现性承诺的前提。
    """

    __tablename__ = "rule_config"
    __table_args__ = (UniqueConstraint("tenant_id", "rule_id", "version"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    #: 规则的稳定标识，如 "limit.taxi.per_trip"
    rule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: JSON Logic 表达式 / 决策表。具体 schema 由 F3 定义。
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
