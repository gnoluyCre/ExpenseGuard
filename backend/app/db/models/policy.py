"""企业制度文档与条款。

核心设计:制度检索的过滤条件是**费用发生日落在制度生效区间**，
而不是「取最新版本」——2 月发生的费用必须按 2 月生效的制度判定。
"""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, uuid_pk


class PolicyDocument(Base, TenantScopedMixin, TimestampMixin):
    """一份制度文档的某个版本。

    `effective_date` / `expiry_date` 同时作为 Qdrant 的 payload 索引，
    使向量检索能带时间维度的复合过滤::

        tenant_id == :tenant
        AND effective_date <= :expense_date
        AND (expiry_date IS NULL OR expiry_date > :expense_date)
    """

    __tablename__ = "policy_document"
    __table_args__ = (
        Index("ix_policy_document_tenant_id_effective_date", "tenant_id", "effective_date"),
        UniqueConstraint("tenant_id", "title", "version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: 为空表示当前仍然生效
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)


class PolicyClause(Base, TenantScopedMixin, TimestampMixin):
    """按条款边界切分出的单条制度条款。

    **`text` 存在 PostgreSQL 而不只在向量库里，是引用校验的前提。**
    LLM 产出的逐字引用必须与这里的原文做机械式字符串比对，
    不通过则拒绝呈现该引用——这是证据链可信度的最后一道防线，
    不是 F4 的实现细节，而是地基。

    为何不可省略:RAG 场景中存在大量「先凭参数记忆生成结论、再补一个
    表面匹配的来源」的 post-rationalized 引用。引用**正确**（来源确实支撑陈述）
    与引用**忠实**（来源确实影响了生成）是两回事，机械比对是工程上
    唯一低成本的防线。
    """

    __tablename__ = "policy_clause"
    __table_args__ = (
        Index("ix_policy_clause_document_id", "document_id"),
        UniqueConstraint("document_id", "clause_no"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("policy_document.id", ondelete="CASCADE"), nullable=False
    )
    #: 条款编号，如 "3.2.1"
    clause_no: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 层级路径，如 "第三章 > 差旅费 > 市内交通"
    hierarchy_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: 条款原文 —— 逐字引用校验的比对源，不可为空
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
