"""租户、用户与会话。"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.mixins import TenantScopedMixin, str_enum, uuid_pk


class Role(StrEnum):
    """RBAC 三角色。权限矩阵定义在 `app.core.security.permissions`。"""

    AUDITOR = "auditor"
    CONFIGURATOR = "configurator"
    VIEWER = "viewer"


class Tenant(Base, TimestampMixin):
    """租户。

    MVP 为单租户单机运行，但隔离方案在架构层内建——
    多租户是演进方向，不是当前形态。
    """

    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class AppUser(Base, TenantScopedMixin, TimestampMixin):
    """系统用户。

    表名用 `app_user` 而非 `user`：`user` 是 PostgreSQL 保留字，
    用它会导致每条手写 SQL 都必须加双引号，迁移里尤其容易出错。

    ⚠️ 必须继承 `TenantScopedMixin`（而不是自己写一个同样的 tenant_id 列）。
    租户过滤器用 `issubclass(Model, TenantScopedMixin)` 判断该给哪些模型
    挂过滤条件 —— 列写得再对，没继承 mixin 就匹配不上，那张表的查询
    会**静默地**不做租户过滤。
    """

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("tenant_id", "username"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    # Argon2 哈希，形如 $argon2id$v=19$...
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(str_enum(Role, "role_enum"), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)


class UserSession(Base, TenantScopedMixin, TimestampMixin):
    """服务端会话。

    **session 存 PostgreSQL 而非 Redis。** TechDesign 明确「MVP 单机单租户，
    不引入额外组件」；更重要的是 session 本身是审计对象——谁在何时登录、
    何时失效，必须与 audit_log 同库同事务，才谈得上可查询、可备份、可回滚。

    **库里只存 token 的 SHA-256，不存明文。** 明文 token 只出现在
    Set-Cookie 响应头里。这样即使数据库被读走，也无法伪造任何会话。
    """

    __tablename__ = "user_session"
    __table_args__ = (
        Index("ix_user_session_expires_at", "expires_at"),
        Index("ix_user_session_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    #: sha256(token) 的 32 字节摘要
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    #: 绝对过期时间上限
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 最后活跃时间，用于闲置过期判定。写入有节流，见 session_service
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: 登出是 UPDATE 此列而非 DELETE 整行——留痕要求
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
