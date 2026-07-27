"""审计日志 —— 追加写，不可静默修改。"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.mixins import TenantScopedMixin, uuid_pk


class AuditLog(Base, TenantScopedMixin):
    """审计日志。

    规则版本、判定依据、复核动作、配置变更、登录登出全部留痕。

    ⚠️ **追加写语义由数据库触发器强制**（见迁移 0001）:
    对本表的 UPDATE 与 DELETE 会直接抛异常。

    把「追加写」做成数据库不变式而非代码纪律，有两个好处:
      1. 绕过 ORM 的手写 SQL 同样受约束
      2. 它可以被测试直接断言——`test_audit_log_is_append_only`
    """

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_tenant_id_at", "tenant_id", "at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    #: 动作发起人。系统自动动作时为空。
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True
    )
    #: 动作名，如 "auth.login" / "rule_config.update" / "review.submit"
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 动作细节。⚠️ 不得写入明文密码、token 或未脱敏的 PII。
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
