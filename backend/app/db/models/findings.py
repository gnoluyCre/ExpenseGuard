"""判定结果:单行判定、跨行关联异常、证据链、复核、抽检、能力声明。"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ARRAY,
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


class ReviewDecision(StrEnum):
    """复核结论。这是回流评测集**唯一的真实标签来源**。"""

    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"


class CapabilityStatus(StrEnum):
    """检测器的能力状态。

    **三种状态都是可接受的产出。** 系统的义务不是「一定能检测」，
    而是准确知道并如实声明自己检测了什么、没检测什么。
    `degraded` 尤其重要——它既保留检测价值，又不把推断结果伪装成确定结论。
    """

    ENABLED = "enabled"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class RuleKind(StrEnum):
    """F3 的五类确定性规则。"""

    LIMIT = "limit"
    INVOICE_TYPE = "invoice_type"
    TIMELINESS = "timeliness"
    INVOICE_TITLE = "invoice_title"
    INVOICE_DUPLICATE = "invoice_duplicate"


class Finding(Base, TenantScopedMixin, TimestampMixin):
    """单行判定结果。

    **二维分级的两个维度分列存储**，不合成单一 severity。
    这是规格明写的要求，必须在地基阶段就分开——事后拆列要迁移数据，
    而合成规则（命中确定性规则直接定级 / 仅统计信号需取证后按代价敏感阈值合成）
    由 F8 实现，但存储结构现在就得对。
    """

    __tablename__ = "finding"
    __table_args__ = (
        file_version_fk(),
        UniqueConstraint(
            "id",
            "tenant_id",
            "file_version_id",
            name="uq_finding_id_tenant_id_file_version_id",
        ),
        ForeignKeyConstraint(
            ["validation_run_id", "tenant_id"],
            ["validation_run.id", "validation_run.tenant_id"],
            name="fk_finding_validation_run_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["rule_config_id", "tenant_id"],
            ["rule_config.id", "rule_config.tenant_id"],
            name="fk_finding_rule_config_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["clause_id", "tenant_id"],
            ["policy_clause.id", "policy_clause.tenant_id"],
            name="fk_finding_clause_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_finding_file_version_id_severity", "file_version_id", "severity_impact"),
        Index("ix_finding_validation_run_id", "validation_run_id"),
        Index("ix_finding_rule_config_id", "rule_config_id"),
        Index(
            "uq_finding_deterministic_rule",
            "validation_run_id",
            "row_no",
            "rule_id",
            "rule_kind",
            unique=True,
            postgresql_where=text("validation_run_id IS NOT NULL"),
        ),
        CheckConstraint(
            "rule_kind IS NULL OR rule_kind IN "
            "('limit', 'invoice_type', 'timeliness', 'invoice_title', 'invoice_duplicate')",
            name="rule_kind_values",
        ),
        CheckConstraint(
            "severity_impact >= 0 AND severity_impact <= 3",
            name="severity_impact_range",
        ),
        CheckConstraint(
            "severity_confidence >= 0 AND severity_confidence <= 3",
            name="severity_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 关联的原始数据行号 —— 证据链的锚点之一
    row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 判定类型，如 "limit_exceeded" / "invoice_duplicate"
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 影响维度:违规造成的后果有多严重
    severity_impact: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 置信维度:系统对这条判定有多确定
    severity_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 命中的规则 ID（确定性校验路径）
    rule_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rule_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 引用的制度条款（LLM 判定路径）
    clause_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    #: 条款逐字引用。**只有通过机械式逐字校验的引用才允许写入这里。**
    quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rule_kind: Mapped[RuleKind | None] = mapped_column(
        str_enum(RuleKind, "rule_kind_enum"), nullable=True
    )
    rule_config_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class CorrelationFinding(Base, TenantScopedMixin, TimestampMixin):
    """跨行关联异常（拆单 / 连号 / 频次异常 / 时空冲突）。

    这类异常只在跨行关联中显现，单行审核看不出来——是本项目的核心差异化能力。
    必须记录**全部**参与行号，否则证据链不成立。
    """

    __tablename__ = "correlation_finding"
    __table_args__ = (
        file_version_fk(),
        Index("ix_correlation_finding_file_version_id", "file_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: 检测器名，如 "split_invoice" / "sequential_invoice_no"
    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 参与该关联的**全部**行号
    participating_row_nos: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)
    #: 判定依据，如 {"threshold": 5000, "sum": 14700, "employee": "...", "date": "..."}
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    severity_impact: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity_confidence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EvidenceStep(Base, TenantScopedMixin, TimestampMixin):
    """ReAct 取证 agent 的单步记录。

    每一步的工具选择、输入、输出全部落库。这既是审计要求，
    也是教学项目的可视化数据源。

    `unique(finding_id, step_no)`:ReAct 循环同样可能被重放，
    步骤记录也需要幂等。
    """

    __tablename__ = "evidence_step"
    __table_args__ = (
        UniqueConstraint("finding_id", "step_no"),
        Index("ix_evidence_step_finding_id_step_no", "finding_id", "step_no"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("finding.id", ondelete="CASCADE"), nullable=False
    )
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    tool_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class Review(Base, TenantScopedMixin, TimestampMixin):
    """人工复核结论。

    `unique(finding_id)` 防止同一判定被重复复核 —— 一条判定只能有一个结论。
    复核结论是回流评测集的唯一真实标签来源，重复会直接污染评测。
    """

    __tablename__ = "review"
    __table_args__ = (UniqueConstraint("finding_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("finding.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[ReviewDecision] = mapped_column(
        str_enum(ReviewDecision, "review_decision_enum"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class SamplingAudit(Base, TenantScopedMixin, TimestampMixin):
    """被放行样本的随机抽检记录。

    ⚠️ **这张表是漏放率可测量的唯一来源。**

    若只标注被拦截的样本，漏放率在数学上不可测量——这是信用评分领域的
    reject inference / selection bias 问题。因此从**第一个批次**起就必须
    对被放行行随机抽检，否则「召回 ≥95%」这一核心指标根本无法计算。

    该机制不是后续优化项，是第一天就要上线的东西。
    """

    __tablename__ = "sampling_audit"
    __table_args__ = (
        UniqueConstraint("file_version_id", "row_no"),
        file_version_fk(),
        Index("ix_sampling_audit_file_version_id", "file_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 抽检结论。为空表示尚未复核。
    decision: Mapped[ReviewDecision | None] = mapped_column(
        str_enum(ReviewDecision, "review_decision_enum"), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CapabilityDeclaration(Base, TenantScopedMixin, TimestampMixin):
    """本批次各检测器的能力声明，由 `field_availability` 推导得出。

    报告中必须显式呈现，如「本批次未启用时空冲突检测:数据源缺少地点字段」。
    这直接服务于「不同企业数据完整度不同是常态，系统必须知道自己能查什么」
    这一产品定位。
    """

    __tablename__ = "capability_declaration"
    __table_args__ = (
        UniqueConstraint("file_version_id", "detector"),
        file_version_fk(),
        Index("ix_capability_declaration_file_version_id", "file_version_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    file_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[CapabilityStatus] = mapped_column(
        str_enum(CapabilityStatus, "capability_status_enum"), nullable=False
    )
    #: 状态成因的人类可读说明，直接进报告
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
