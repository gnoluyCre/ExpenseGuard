"""租户过滤的强制注入机制。

## 为什么用 ORM 事件而不是手写 WHERE

AGENTS.md 的架构主权条款要求「多租户过滤通过依赖注入强制注入，不手写 WHERE」。
理由很直接:手写过滤是**遗漏型**失败——写漏一处，那个查询就静默返回全租户数据，
不报错、不告警、代码评审也极易放过。

改成 ORM 事件后，过滤是**默认行为**:任何带 `TenantScopedMixin` 的模型查询
都自动带上 `tenant_id = :current`，业务代码想漏都漏不掉。

## fail-closed 是关键设计

会话上没有租户上下文时，这里**抛异常而不是放行**。
一个「忘了注入租户」的 bug 必须表现为 500 错误，
绝不能静默返回全租户数据——在财务审计系统里，后者是数据泄漏。

## 已知边界

`with_loader_criteria` 只作用于 ORM 查询。绕过 ORM 的手写
`session.execute(text("SELECT ..."))` 不受此保护。
若要覆盖那条路径，需要在数据库层加 PostgreSQL RLS
（代价是应用与迁移需分别用受限 / BYPASSRLS 两个角色）。
Phase 1 先靠 ORM 层，RLS 作为后续独立迁移可选加固。
"""

import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.core.errors import TenantScopeMissingError
from app.core.tenancy.context import SESSION_TENANT_KEY
from app.db.models.mixins import TenantScopedMixin


def bind_tenant(session: Session, tenant_id: uuid.UUID) -> None:
    """把租户上下文绑到会话上。

    此后该会话上的所有 ORM 查询都会自动带租户过滤。
    """
    session.info[SESSION_TENANT_KEY] = tenant_id


def current_tenant(session: Session) -> uuid.UUID | None:
    """读取会话上绑定的租户，未绑定则返回 None。"""
    value = session.info.get(SESSION_TENANT_KEY)
    return value if isinstance(value, uuid.UUID) else None


#: 显式跳过租户过滤的 execution option。
#:
#: 唯一的合法用途是**登录**:那时租户上下文尚未建立——它正是要靠
#: 那次查询来确定。除此之外任何用法都应被视为可疑。
#:
#: 做成显式选项而不是「某些情况下守卫自动放行」，是为了让绕过行为
#: 在代码里可搜索、在评审中可见。`grep -r skip_tenant_filter` 应该
#: 只有屈指可数的几处命中。
SKIP_TENANT_FILTER = "skip_tenant_filter"


def skip_tenant_filter_options() -> dict[str, Any]:
    """产出「跳过租户过滤」的 execution options。

    做成函数而不是让调用方写 `{SKIP_TENANT_FILTER: True}`，
    是因为 SQLAlchemy 的 `execution_options` 参数类型是 TypedDict，
    mypy 要求键必须是字面量 —— 用常量当键会报
    `Expected TypedDict key to be string literal`。

    在这里集中转一次类型，调用方就能既用常量、又过类型检查。
    """
    return {SKIP_TENANT_FILTER: True}


def _apply_tenant_filter(execute_state: ORMExecuteState) -> None:
    """在每次 ORM SELECT 前注入租户过滤。"""
    # 只管 SELECT。列级懒加载（is_column_load）已经限定在已过滤的父行上，
    # 再套一层过滤会导致关系加载异常。
    if not execute_state.is_select or execute_state.is_column_load:
        return

    # 关系加载同理:父行已经过滤过了。
    if execute_state.is_relationship_load:
        return

    # 显式声明的绕过（仅登录路径）
    if execute_state.execution_options.get(SKIP_TENANT_FILTER):
        return

    tenant_id = execute_state.session.info.get(SESSION_TENANT_KEY)
    if tenant_id is None:
        # fail-closed —— 宁可 500，也不泄漏跨租户数据
        raise TenantScopeMissingError

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


def install_tenant_guard(session_class: type[Session] = Session) -> None:
    """安装租户过滤监听器。

    应在应用启动时调用一次。重复调用是安全的（SQLAlchemy 会去重）。

    ⚠️ 反向验证:临时注释掉本函数的调用，
    `tests/integration/test_tenancy.py` 的跨租户测试必须变红。
    若它们仍然通过，说明测试测的不是这个机制。
    """
    if not event.contains(session_class, "do_orm_execute", _apply_tenant_filter):
        event.listen(session_class, "do_orm_execute", _apply_tenant_filter)


def uninstall_tenant_guard(session_class: type[Session] = Session) -> None:
    """卸载租户过滤监听器。仅供测试做反向验证使用。"""
    if event.contains(session_class, "do_orm_execute", _apply_tenant_filter):
        event.remove(session_class, "do_orm_execute", _apply_tenant_filter)
