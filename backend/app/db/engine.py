"""数据库引擎与会话工厂。

应用侧走异步（FastAPI + LangGraph 都是 async），Alembic 走同步——
psycopg3 的同一个 dialect 名同时支持两者，所以只有一个驱动、一套连接串。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import Settings


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """按配置创建异步引擎。

    `pool_pre_ping` 让连接在使用前先探活，避免数据库重启后
    整个连接池里都是死连接（Docker Compose 开发时很常见）。
    """
    return create_async_engine(
        str(settings.database_url),
        pool_pre_ping=True,
        echo=False,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建会话工厂。

    `expire_on_commit=False`：commit 之后对象属性仍可读，
    否则每次读属性都会触发一次隐式 SELECT——在 async 上下文里会直接抛错。
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def dispose_engine(engine: AsyncEngine) -> None:
    """关闭引擎并释放连接池。用于应用优雅退出。"""
    await engine.dispose()


async def iter_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """产出一个会话并保证关闭。

    注意：这里**不设置**租户上下文。带租户过滤的会话由
    `app.api.deps.get_tenant_db` 提供——租户过滤必须经依赖注入强制注入，
    不允许业务代码手写 WHERE tenant_id（AGENTS.md 架构主权条款）。
    """
    async with session_factory() as session:
        yield session
