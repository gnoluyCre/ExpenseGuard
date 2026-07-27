"""模型 Mixin 与共用列类型。"""

import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column


def uuid_pk() -> Mapped[uuid.UUID]:
    """标准主键列：客户端生成的 UUID。"""
    return mapped_column(primary_key=True, default=uuid.uuid4)


def str_enum(enum_cls: type, name: str) -> SAEnum:
    """把 Python StrEnum 映射为 VARCHAR + CHECK 约束。

    **刻意不用 PostgreSQL 原生 ENUM。** 原生 ENUM 增删值要 `ALTER TYPE`，
    在迁移与回滚里极难写对（尤其 downgrade 无法删除单个值）。
    VARCHAR + CHECK 的表达力相同，但迁移就是普通的约束增删。
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        values_callable=lambda e: [member.value for member in e],
    )


class TenantScopedMixin:
    """带 tenant_id 的业务表。

    **所有业务表都冗余 tenant_id**，即使它能通过 file_version_id 间接推出。
    原因：`app.core.tenancy.scope` 靠 `with_loader_criteria` 在 ORM 层统一注入
    租户过滤，而这需要每个模型上真实存在 tenant_id 列——没有列就挂不上过滤器。

    冗余带来的漂移风险由**复合外键**在数据库层消除，见 `file_version_fk()`。
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


def file_version_fk() -> ForeignKeyConstraint:
    """指向 file_version 的复合外键 `(file_version_id, tenant_id)`。

    这条约束把「冗余的 tenant_id 可能与 file_version 的 tenant_id 不一致」
    从**代码纪律**变成**数据库不变式**：子行的 tenant_id 在物理上
    不可能偏离其所属 file_version 的 tenant_id。

    依赖 file_version 上的冗余唯一约束 `unique(id, tenant_id)`——
    PostgreSQL 要求外键的目标列组合上存在唯一约束。
    """
    return ForeignKeyConstraint(
        ["file_version_id", "tenant_id"],
        ["file_version.id", "file_version.tenant_id"],
        ondelete="CASCADE",
    )
