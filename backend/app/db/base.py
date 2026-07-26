"""SQLAlchemy 声明式基类与命名约定。

**naming_convention 不是可选项。** 没有它，unique / check / foreign key 约束的名字
由 PostgreSQL 自动生成且不确定，会导致两个具体问题：
  1. `ON CONFLICT ON CONSTRAINT <name>` 无法稳定引用约束
  2. Alembic 的 downgrade 找不到要删的约束名

而 `row_result` 的唯一约束是本项目的幂等基石（AGENTS.md 受保护区域），
它的名字必须是确定的、可被测试直接断言的。
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 约束命名约定。%(column_0_N_name)s 会把复合约束的所有列名拼进去，
#: 于是 unique(file_version_id, row_no) → uq_row_result_file_version_id_row_no
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """当前 UTC 时间（带时区）。

    不用 `datetime.utcnow()`——它返回 naive datetime 且在 Python 3.12+ 已弃用。
    """
    return datetime.now(UTC)


class TimestampMixin:
    """带创建时间的表。时间列一律带时区，由数据库侧生成默认值。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
