"""认证、RBAC 与租户隔离的集成测试。

全部走真实的 ASGI 请求（httpx + ASGITransport）与真实数据库，
不 mock 任何一层 —— 这些测试要验证的正是「各层拼起来之后行为是否正确」，
mock 掉任何一层都会让结论失去意义。
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import TenantScopeMissingError
from app.core.security.password import hash_password
from app.core.security.session_service import SESSION_COOKIE_NAME
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import FileVersion
from app.db.models.tenancy import AppUser, Role, Tenant, UserSession
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """接到测试库的应用实例。

    直接注入 session_factory 而不跑 lifespan —— lifespan 会按 .env
    连开发库，那样测试就会污染开发数据。
    """
    instance = create_app()
    instance.state.session_factory = session_factory
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI 客户端。`follow_redirects` 关闭，避免掩盖非预期的跳转。"""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        yield ac


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
    roles: tuple[Role, ...] = (Role.AUDITOR,),
) -> tuple[uuid.UUID, dict[Role, uuid.UUID]]:
    """建一个租户，并为每个角色建一个用户名为角色名的用户。"""
    tenant = Tenant(slug=slug, name=f"租户 {slug}")
    session.add(tenant)
    await session.flush()

    users: dict[Role, uuid.UUID] = {}
    for role in roles:
        user = AppUser(
            tenant_id=tenant.id,
            username=role.value,
            password_hash=hash_password(PASSWORD),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        users[role] = user.id
    await session.commit()
    return tenant.id, users


async def _login(client: AsyncClient, *, slug: str, username: str, password: str = PASSWORD):
    return await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": password},
    )


# ======================================================================
# 未认证
# ======================================================================
async def test_未登录访问受保护端点返回_401(client: AsyncClient) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_INVALID"


async def test_伪造的_token_返回_401(client: AsyncClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "totally-made-up-token")
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


# ======================================================================
# 登录
# ======================================================================
async def test_登录成功并下发安全_cookie(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        await _seed_tenant(s, slug="acme")

    resp = await _login(client, slug="acme", username="auditor")
    assert resp.status_code == 200

    raw = resp.headers["set-cookie"]
    assert "HttpOnly" in raw, "cookie 必须是 HttpOnly —— 否则 XSS 可直接偷走会话"
    assert "SameSite=lax" in raw.lower().replace("samesite=lax", "SameSite=lax")
    assert "Path=/" in raw

    body = resp.json()
    assert body["role"] == "auditor"
    assert "review:submit" in body["permissions"]
    assert "config:write" not in body["permissions"]


async def test_密码错误返回_401_且写审计日志(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        tenant_id, _ = await _seed_tenant(s, slug="acme")

    resp = await _login(client, slug="acme", username="auditor", password="wrong")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async with session_factory() as s:
        bind_tenant(s.sync_session, tenant_id)
        count = await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.login_failed")
        )
    assert count == 1, "失败的登录尝试必须留痕"


async def test_不存在的用户与密码错误返回相同响应(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """防用户名枚举。

    若两者返回不同的 code 或不同的 message，攻击者据此就能判断
    哪些用户名是有效的 —— 在企业内部系统里，有效用户名本身就是情报。
    （时序上的等价由 `verify_dummy()` 保证，此处只断言响应等价。）
    """
    async with session_factory() as s:
        await _seed_tenant(s, slug="acme")

    wrong_password = await _login(client, slug="acme", username="auditor", password="nope")
    no_such_user = await _login(client, slug="acme", username="ghost", password="nope")

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json() == no_such_user.json()


async def test_停用用户无法登录(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        tenant_id, users = await _seed_tenant(s, slug="acme")
        bind_tenant(s.sync_session, tenant_id)
        user = await s.get(AppUser, users[Role.AUDITOR])
        assert user is not None
        user.is_active = False
        await s.commit()

    resp = await _login(client, slug="acme", username="auditor")
    assert resp.status_code == 401


# ======================================================================
# 会话生命周期
# ======================================================================
async def test_闲置超时后会话失效(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        tenant_id, _ = await _seed_tenant(s, slug="acme")

    await _login(client, slug="acme", username="auditor")
    assert (await client.get("/api/auth/me")).status_code == 200

    # 把 last_seen_at 推回 9 小时前（闲置上限 8 小时）
    async with session_factory() as s:
        bind_tenant(s.sync_session, tenant_id)
        record = await s.scalar(select(UserSession))
        assert record is not None
        record.last_seen_at = datetime.now(UTC) - timedelta(hours=9)
        await s.commit()

    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_EXPIRED"


async def test_绝对超时后会话失效(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """即便一直在活动，超过绝对上限也必须重新登录。"""
    async with session_factory() as s:
        tenant_id, _ = await _seed_tenant(s, slug="acme")

    await _login(client, slug="acme", username="auditor")

    async with session_factory() as s:
        bind_tenant(s.sync_session, tenant_id)
        record = await s.scalar(select(UserSession))
        assert record is not None
        # last_seen_at 保持最新（模拟持续活动），只让绝对期限过去
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await s.commit()

    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SESSION_EXPIRED"


async def test_登出后会话被撤销且留痕(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as s:
        tenant_id, _ = await _seed_tenant(s, slug="acme")

    await _login(client, slug="acme", username="auditor")
    assert (await client.post("/api/auth/logout")).status_code == 204

    async with session_factory() as s:
        bind_tenant(s.sync_session, tenant_id)
        record = await s.scalar(select(UserSession))
        assert record is not None
        assert record.revoked_at is not None, "登出应标记 revoked_at 而非删除整行"

        logged = await s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == "auth.logout")
        )
        assert logged == 1


# ======================================================================
# RBAC 权限矩阵
# ======================================================================
@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (Role.AUDITOR, {"batch:import", "review:submit", "config:read"}),
        (Role.CONFIGURATOR, {"batch:import", "review:submit", "config:write"}),
        (Role.VIEWER, {"batch:read", "report:read", "report:export"}),
    ],
)
async def test_角色权限矩阵(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    role: Role,
    expected: set[str],
) -> None:
    async with session_factory() as s:
        await _seed_tenant(s, slug="acme", roles=(role,))

    resp = await _login(client, slug="acme", username=role.value)
    assert resp.status_code == 200
    granted = set(resp.json()["permissions"])
    assert expected <= granted, f"{role} 缺少权限: {expected - granted}"


async def test_只读角色不能提交复核(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """viewer 刻意不给 review:submit。

    复核标记会作为真实标签进入回流评测集，让只读角色能写标签
    会污染评测数据的来源。
    """
    async with session_factory() as s:
        await _seed_tenant(s, slug="acme", roles=(Role.VIEWER,))

    resp = await _login(client, slug="acme", username="viewer")
    assert "review:submit" not in resp.json()["permissions"]


# ======================================================================
# 租户隔离 —— CP3 的核心
# ======================================================================
async def test_跨租户查询返回空而非他人数据(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """租户 A 的上下文查不到租户 B 的数据。

    ⚠️ 反向验证:临时注释掉 `core/tenancy/scope.py` 里
    `install_tenant_guard()` 的事件监听器，本测试必须变红。
    若它仍然通过，说明测的不是过滤机制。
    """
    async with session_factory() as s:
        tenant_a, users_a = await _seed_tenant(s, slug="a")
        tenant_b, users_b = await _seed_tenant(s, slug="b")

        # 各自建一个批次
        for tid, uid, name in (
            (tenant_a, users_a[Role.AUDITOR], "a.xlsx"),
            (tenant_b, users_b[Role.AUDITOR], "b.xlsx"),
        ):
            s.add(
                FileVersion(
                    tenant_id=tid,
                    filename=name,
                    content_hash=uuid.uuid4().hex * 2,
                    uploaded_by=uid,
                )
            )
        await s.commit()

    async with session_factory() as s:
        bind_tenant(s.sync_session, tenant_a)
        rows = (await s.scalars(select(FileVersion))).all()

    names = {r.filename for r in rows}
    assert names == {"a.xlsx"}, f"租户 A 看到了不属于自己的数据: {names}"


async def test_未绑定租户时查询直接报错(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """fail-closed:忘了注入租户必须炸，绝不能静默返回全租户数据。

    这条断言的价值在于把「遗漏型」失败转成「显式型」失败:
    500 错误会被立刻发现，而静默返回全部数据不会。
    """
    async with session_factory() as s:
        await _seed_tenant(s, slug="acme")

    async with session_factory() as s:
        # 刻意不调用 bind_tenant
        with pytest.raises(TenantScopeMissingError):
            await s.scalars(select(FileVersion))
