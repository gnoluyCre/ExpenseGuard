"""报销批次:文件版本、原始行、行级结果、字段可用性。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, file_version_fk, str_enum, uuid_pk


class FieldStatus(StrEnum):
    """字段可用性探测结果（三级降级的第一环）。

    不同企业的导出字段不同，因此可用性必须在解析时**自动探测**，
    而非人工逐租户配置——这是「接入新企业不改代码」在解析层的落地。
    """

    AVAILABLE = "available"  # 直接命中：存在明确列且非空率达标
    INFERRED = "inferred"  # 可推断：如从商户名称提取地名，准确性有限
    MISSING = "missing"  # 缺失：以上均不满足


class ParseStatus(StrEnum):
    """批次结构化解析状态；并发处理中状态由数据库行锁表达。"""

    UNPARSED = "unparsed"
    PARSED = "parsed"
    PARSED_WITH_ERRORS = "parsed_with_errors"
    FAILED = "failed"


class RevisionReason(StrEnum):
    """创建派生文件版本的显式原因。"""

    RULESET_CHANGE = "ruleset_change"
    MAPPING_CHANGE = "mapping_change"
    POLICY_CHANGE = "policy_change"


class FileVersion(Base, TenantScopedMixin, TimestampMixin):
    """一次 Excel 导入。

    `unique(tenant_id, content_hash)` 使同一文件重复上传复用既有批次，
    而不是产生第二份平行数据。
    """

    __tablename__ = "file_version"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "content_hash",
            "revision_no",
            name="uq_file_version_tenant_id_content_hash_revision_no",
        ),
        Index(
            "uq_file_version_revision_one_content_hash",
            "tenant_id",
            "content_hash",
            unique=True,
            postgresql_where=text("revision_no = 1"),
        ),
        Index(
            "uq_file_version_source_request_key",
            "tenant_id",
            "source_file_version_id",
            "revision_request_key_hash",
            unique=True,
            postgresql_where=text("revision_request_key_hash IS NOT NULL"),
        ),
        # 冗余唯一约束：供子表的复合外键 (file_version_id, tenant_id) 引用。
        # PostgreSQL 要求外键目标列组合上存在唯一约束。
        UniqueConstraint("id", "tenant_id", name="uq_file_version_id_tenant_id"),
        ForeignKeyConstraint(
            ["mapping_version_id", "tenant_id"],
            ["schema_mapping_version.id", "schema_mapping_version.tenant_id"],
            name="fk_file_version_mapping_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_file_version_source_file_version_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["root_file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name="fk_file_version_root_file_version_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "parse_status IN ('unparsed', 'parsed', 'parsed_with_errors', 'failed')",
            name="parse_status_values",
        ),
        CheckConstraint("revision_no > 0", name="revision_no_positive"),
        CheckConstraint(
            "revision_reason IS NULL OR revision_reason IN "
            "('ruleset_change', 'mapping_change', 'policy_change')",
            name="revision_reason_values",
        ),
        CheckConstraint(
            "(revision_no = 1 AND source_file_version_id IS NULL "
            "AND root_file_version_id IS NULL AND revision_reason IS NULL "
            "AND revision_request_key_hash IS NULL "
            "AND revision_request_fingerprint IS NULL) OR "
            "(revision_no > 1 AND source_file_version_id IS NOT NULL "
            "AND root_file_version_id IS NOT NULL AND revision_reason IS NOT NULL "
            "AND revision_request_key_hash IS NOT NULL "
            "AND revision_request_fingerprint IS NOT NULL)",
            name="revision_lineage",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    #: 文件内容的 SHA-256 十六进制串
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    mapping_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    parse_status: Mapped[ParseStatus] = mapped_column(
        str_enum(ParseStatus, "parse_status_enum"),
        nullable=False,
        default=ParseStatus.UNPARSED,
        server_default=ParseStatus.UNPARSED.value,
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    source_file_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    root_file_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    revision_reason: Mapped[RevisionReason | None] = mapped_column(
        str_enum(RevisionReason, "revision_reason_enum"), nullable=True
    )
    revision_request_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision_request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ExpenseRow(Base, TenantScopedMixin, TimestampMixin):
    """解析后的单行报销记录。

    Phase 1 只落 `raw_json`。类型化列（金额 / 日期 / 发票号 / 商户……）
    由 F2 决定后用加列迁移补——那时才知道要哪些字段。
    这是「骨架 vs 细节」分层的样板：结构先立住，字段随需求长。
    """

    __tablename__ = "expense_row"
    __table_args__ = (
        UniqueConstraint("file_version_id", "row_no"),
        UniqueConstraint(
            "file_version_id",
            "row_no",
            "tenant_id",
            name="uq_expense_row_file_row_tenant",
        ),
        file_version_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    #: Excel 中的行号（1-based），是行级幂等的业务键
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 原始行内容，永远保留——解析规则变更后可重放
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    normalized_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: 解析失败的原因。非空即表示该行进了错误清单。
    #: PRD 硬性要求：解析失败的行不得静默丢弃。
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class RowResult(Base, TenantScopedMixin):
    """行级处理结果 —— **幂等核心表**。

    唯一约束是 `(file_version_id, row_no)` 而非自增 id。这样即使
    LangGraph 节点重放、并发写入、或进程崩溃后重启，同一行的副作用
    最多发生一次。

    恢复语义::

        workflow thread_id = file_version_id
        每行处理前:SELECT row_result WHERE (file_version_id, row_no)
          命中 → 直接返回已有结果，不重复执行任何副作用
          未命中 → 执行 → INSERT ... ON CONFLICT DO NOTHING

    ⚠️ 本表的唯一约束属于 AGENTS.md 受保护区域，不得弱化或删除。
    """

    __tablename__ = "row_result"
    __table_args__ = (
        # ↓↓↓ 项目最高优先级约束 ↓↓↓
        UniqueConstraint("file_version_id", "row_no"),
        UniqueConstraint(
            "file_version_id",
            "row_no",
            "tenant_id",
            name="uq_row_result_file_row_tenant",
        ),
        file_version_fk(),
        Index("ix_row_result_file_version_id", "file_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 判定结论。取值域由 F3 定义，Phase 1 不约束。
    verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 产生该结果的规则版本 —— 可复现性的锚点：
    #: 相同输入 + 相同规则版本 → 相同输出
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FieldAvailability(Base, TenantScopedMixin, TimestampMixin):
    """字段可用性探测结果。

    下游各 detector 查询本表判断自身依赖是否满足，据此产出
    enabled / degraded / unavailable 三种状态之一。
    """

    __tablename__ = "field_availability"
    __table_args__ = (
        UniqueConstraint("file_version_id", "field_name"),
        file_version_fk(),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[FieldStatus] = mapped_column(
        str_enum(FieldStatus, "field_status_enum"), nullable=False
    )
    #: 探测依据（非空率、提取成功率、样本等），供人工复核判断
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
