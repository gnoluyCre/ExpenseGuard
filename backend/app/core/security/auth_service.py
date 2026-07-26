"""认证服务:登录、登出与审计留痕。

⚠️ 本模块的查询**刻意绕过租户过滤**。原因很直接:登录发生在租户上下文
建立**之前** —— 那时还不知道用户属于哪个租户，正是要靠这次查询确定。

这是整个系统里唯一允许绕过租户过滤的地方，因此:
  - 查询按 `(tenant_slug, username)` 精确定位，不做任何模糊匹配
  - 只查 `app_user` 一张表，不触碰任何业务数据
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError
from app.core.security.password import hash_password, needs_rehash, verify_dummy, verify_password
from app.core.tenancy.scope import skip_tenant_filter_options
from app.db.models.audit import AuditLog
from app.db.models.tenancy import AppUser, Tenant


class InvalidCredentialsError(AuthenticationError):
    """用户名或密码错误。

    ⚠️ 提示文案刻意**不区分**「用户不存在」与「密码错误」——
    区分开等于免费告诉攻击者哪些用户名是有效的。
    """

    def __init__(self) -> None:
        super().__init__(code="INVALID_CREDENTIALS", message="用户名或密码错误")


async def write_audit(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """追加一条审计日志。

    ⚠️ `payload` 中**绝不能**写入明文密码、session token 或未脱敏的 PII。
    审计日志是追加写的，写错了改不掉（数据库触发器会拒绝 UPDATE/DELETE）。
    """
    db.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload_json=payload,
        )
    )
    await db.flush()


async def authenticate(
    db: AsyncSession,
    *,
    tenant_slug: str,
    username: str,
    password: str,
) -> AppUser:
    """校验凭据并返回用户。

    时序安全:用户不存在时**同样**执行一次 argon2 校验（对哑哈希）。
    否则「用户不存在」会比「密码错误」快一个数量级，攻击者据此
    可以枚举出有效用户名。

    Raises:
        InvalidCredentialsError: 租户/用户不存在、已停用、或密码错误。
    """
    stmt = (
        select(AppUser, Tenant.id)
        .join(Tenant, Tenant.id == AppUser.tenant_id)
        .where(Tenant.slug == tenant_slug, AppUser.username == username)
    )
    # 绕过租户过滤:此刻还不知道租户是谁，正是要靠这次查询确定
    row = (await db.execute(stmt, execution_options=skip_tenant_filter_options())).one_or_none()

    if row is None:
        verify_dummy()
        raise InvalidCredentialsError

    # 显式标注:Row 的解包结果是 Any，mypy strict 会在 return 处报 no-any-return
    user: AppUser = row[0]
    tenant_id: uuid.UUID = row[1]

    async def _fail(reason: str) -> InvalidCredentialsError:
        """记录失败原因并**立即提交**，然后返回待抛出的异常。

        ⚠️ 这里的 commit 不是多余的。请求级依赖 `get_db` 在异常传播时会
        `rollback()`，若不提前提交，这条失败登录的审计记录会跟着异常
        一起被回滚掉 —— 结果就是「暴力破解尝试一条都没留痕」，
        而这恰恰是最需要留痕的场景。

        此刻事务里只有这一条审计记录（用户查询是只读的），
        因此单独提交它是安全的，不会把半截状态固化下来。
        """
        await write_audit(
            db,
            tenant_id=tenant_id,
            action="auth.login_failed",
            target_type="app_user",
            target_id=str(user.id),
            payload={"reason": reason},
        )
        await db.commit()
        return InvalidCredentialsError()

    if not user.is_active:
        verify_dummy()
        raise await _fail("inactive")

    if not verify_password(user.password_hash, password):
        raise await _fail("bad_password")

    # 参数升级后透明地重算哈希 —— 用户无感知
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    await write_audit(
        db,
        tenant_id=tenant_id,
        action="auth.login",
        actor_id=user.id,
        target_type="app_user",
        target_id=str(user.id),
    )
    return user
