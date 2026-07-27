"""FastAPI 依赖注入。

## 依赖链

    get_settings
        ↓
    get_db          裸会话，**未绑定租户** —— 只有登录路径能用
        ↓
    get_auth        从 cookie 解析会话，得到 TenantContext
        ↓
    get_tenant_db   已绑定租户的会话 —— 业务路由一律用这个

## 关键设计:租户过滤由依赖注入强制，不由业务代码负责

`get_tenant_db` 产出的会话**天生带租户过滤**。业务代码拿到它之后，
即便想写跨租户查询也写不出来 —— 过滤器在 ORM 层自动注入，
不需要（也不允许）手写 `WHERE tenant_id = ...`。

这与 AGENTS.md 的架构主权条款一致:手写过滤是**遗漏型**失败，
漏一处就静默泄漏；默认过滤则是**加入型**——想绕过必须显式声明。

本模块只做适配:把领域异常转成 HTTP 语义，不含任何业务逻辑。
"""

from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

from fastapi import Cookie, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import PermissionDeniedError
from app.core.security.permissions import Permission, has_permission
from app.core.security.session_service import (
    SESSION_COOKIE_NAME,
    SessionNotFoundError,
    resolve_session,
)
from app.core.tenancy.context import TenantContext
from app.core.tenancy.scope import bind_tenant
from app.settings import Settings, get_settings

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """从应用状态取会话工厂（由 lifespan 装配）。"""
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:  # pragma: no cover
        raise RuntimeError("session_factory 未装配 —— 检查 lifespan")
    return factory  # type: ignore[no-any-return]


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """裸数据库会话，**未绑定租户**。

    ⚠️ 只有登录路径应该用它。业务路由一律用 `get_tenant_db`，
    否则任何 ORM 查询都会因缺少租户上下文而抛 `TenantScopeMissingError`
    （这是 fail-closed 设计的预期行为，不是 bug）。
    """
    async with get_session_factory(request)() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbDep = Annotated[AsyncSession, Depends(get_db)]


async def get_auth(
    db: DbDep,
    settings: SettingsDep,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
) -> TenantContext:
    """从 cookie 解析当前身份。

    未携带 cookie 与 token 无效返回同样的错误 —— 不给探测者额外信息。
    """
    if not session_token:
        raise SessionNotFoundError

    authenticated = await resolve_session(db, token=session_token, settings=settings)

    # 一旦知道租户是谁，立刻绑到会话上。
    # 这样后续所有查询（包括登出时撤销会话）都自动受租户过滤保护，
    # 不需要再开任何绕过口子。
    bind_tenant(db.sync_session, authenticated.tenant_id)

    return TenantContext(
        tenant_id=authenticated.tenant_id,
        user_id=authenticated.user_id,
        role=authenticated.role,
        session_id=authenticated.session_id,
    )


AuthDep = Annotated[TenantContext, Depends(get_auth)]


async def get_tenant_db(db: DbDep, auth: AuthDep) -> AsyncSession:
    """已绑定租户的会话 —— 业务路由的标准依赖。

    绑定之后，该会话上所有带 `TenantScopedMixin` 的模型查询
    都会自动附加 `tenant_id = :current` 过滤。
    """
    bind_tenant(db.sync_session, auth.tenant_id)
    return db


TenantDbDep = Annotated[AsyncSession, Depends(get_tenant_db)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def require_permission(
    permission: Permission,
) -> Callable[[TenantContext], Coroutine[Any, Any, TenantContext]]:
    """依赖工厂:要求当前角色具备指定权限。

    用法::

        @router.post("/rules", dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))])
    """

    async def _check(auth: AuthDep) -> TenantContext:
        if not has_permission(auth.role, permission):
            raise PermissionDeniedError(
                code="PERMISSION_DENIED",
                message=f"当前角色无权执行该操作（需要 {permission.value}）",
            )
        return auth

    return _check
