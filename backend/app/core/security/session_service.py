"""服务端会话。

## 为什么存 PostgreSQL 而不是 Redis

1. TechDesign 明确「MVP 单机单租户，不引入额外组件」——Redis 意味着
   一个新的 compose 服务、新的故障域、新的备份口径，而在**用户数个位数**
   的场景下换来的性能收益为零。
2. **session 本身是审计对象。** 谁在什么时间登录、何时失效，必须和
   `audit_log` 同库同事务写入，才谈得上可查询、可备份、可回滚。
   Redis 做不到这点，除非双写——那更糟。

## token 只存哈希

cookie 里放 `secrets.token_urlsafe(32)` 的**明文**，数据库里只存它的
SHA-256 摘要。这样即使数据库被读走（备份泄漏、SQL 注入、离职员工带走
dump），也无法伪造出任何一个可用会话。

这里用 SHA-256 而非 argon2 是刻意的:token 是 32 字节的高熵随机值，
不存在字典攻击的空间，慢哈希只会给每个请求平白加上几十毫秒。
密码才需要慢哈希，因为密码是低熵的。

## 双重过期

- `last_seen_at` + 闲置超时 → 离开工位一段时间后自动失效
- `expires_at` 绝对上限 → 即便一直在操作，也强制重新登录

取两者中更早发生的那个。

## 写节流

每个请求都 `UPDATE last_seen_at` 会把 session 表变成写热点。
仅当距上次更新超过阈值（默认 60 秒）才真正写库，可以平摊掉
绝大部分写入，而对「闲置 8 小时」这个粒度的判定毫无影响。
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError
from app.core.tenancy.scope import skip_tenant_filter_options
from app.db.models.tenancy import AppUser, Role, UserSession
from app.settings import Settings

#: cookie 里 token 的字节数。32 字节 ≈ 256 位熵，远超暴力破解可行范围。
_TOKEN_BYTES = 32

#: 会话 cookie 的名字。
#:
#: 定义成模块常量而非配置项，是因为 FastAPI 的 `Cookie(alias=...)`
#: 在**导入时**求值，拿不到运行时配置。若做成配置项，就会出现
#: 「写 cookie 用配置值、读 cookie 用硬编码值」的错位——改了配置之后
#: 登录会成功但立刻就是未登录状态，且没有任何报错。
SESSION_COOKIE_NAME = "eg_session"


def _hash_token(token: str) -> bytes:
    """token → SHA-256 摘要（32 字节）。"""
    return hashlib.sha256(token.encode("utf-8")).digest()


@dataclass(frozen=True)
class AuthenticatedSession:
    """已解析并校验通过的会话。"""

    session_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: Role


class SessionExpiredError(AuthenticationError):
    """会话已过期或已被撤销。"""

    def __init__(self) -> None:
        super().__init__(code="SESSION_EXPIRED", message="会话已过期，请重新登录")


class SessionNotFoundError(AuthenticationError):
    """token 无效。"""

    def __init__(self) -> None:
        super().__init__(code="SESSION_INVALID", message="登录状态无效，请重新登录")


async def create_session(
    db: AsyncSession,
    *,
    user: AppUser,
    settings: Settings,
) -> tuple[str, UserSession]:
    """为用户创建会话。

    Returns:
        `(明文 token, 会话记录)`。**明文 token 只在这里出现一次**——
        调用方应立即把它写进 Set-Cookie，之后再也无法从数据库还原。
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    now = datetime.now(UTC)

    record = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=_hash_token(token),
        expires_at=now + timedelta(seconds=settings.session_absolute_timeout_seconds),
        last_seen_at=now,
    )
    db.add(record)
    await db.flush()
    return token, record


async def resolve_session(
    db: AsyncSession,
    *,
    token: str,
    settings: Settings,
) -> AuthenticatedSession:
    """校验 token 并返回会话上下文。

    Raises:
        SessionNotFoundError: token 不存在，或对应用户已停用。
        SessionExpiredError: 已撤销 / 已过绝对期限 / 闲置超时。
    """
    stmt = (
        select(UserSession, AppUser)
        .join(AppUser, AppUser.id == UserSession.user_id)
        .where(UserSession.token_hash == _hash_token(token))
    )
    # 鸡生蛋问题:必须先查到会话才知道它属于哪个租户，而租户守卫
    # 要求查询时已有租户上下文。这里显式绕过——查询条件是 token 的
    # SHA-256 精确匹配，本身就唯一确定了一行，不存在越权范围。
    #
    # 这是全系统仅有的两处绕过之一（另一处是登录时按用户名查用户）。
    row = (await db.execute(stmt, execution_options=skip_tenant_filter_options())).one_or_none()
    if row is None:
        raise SessionNotFoundError

    record, user = row
    if not user.is_active:
        raise SessionNotFoundError

    now = datetime.now(UTC)
    if record.revoked_at is not None:
        raise SessionExpiredError
    if record.expires_at <= now:
        raise SessionExpiredError

    idle_deadline = record.last_seen_at + timedelta(seconds=settings.session_idle_timeout_seconds)
    if idle_deadline <= now:
        raise SessionExpiredError

    await _touch(db, record=record, now=now, settings=settings)

    return AuthenticatedSession(
        session_id=record.id,
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role,
    )


async def _touch(
    db: AsyncSession,
    *,
    record: UserSession,
    now: datetime,
    settings: Settings,
) -> None:
    """带节流地刷新 `last_seen_at`。

    只有距上次刷新超过阈值才真正写库 —— 否则 session 表会变成写热点。
    """
    elapsed = (now - record.last_seen_at).total_seconds()
    if elapsed < settings.session_touch_throttle_seconds:
        return
    record.last_seen_at = now
    await db.flush()


async def revoke_session(db: AsyncSession, *, session_id: uuid.UUID) -> None:
    """撤销会话（登出）。

    用 UPDATE 标记 `revoked_at` 而**不是** DELETE 整行 —— 会话是审计对象，
    「这个会话何时被主动登出」本身就是要留痕的信息。
    """
    record = await db.get(UserSession, session_id)
    if record is None or record.revoked_at is not None:
        return
    record.revoked_at = datetime.now(UTC)
    await db.flush()
