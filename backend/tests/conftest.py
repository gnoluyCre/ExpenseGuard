"""测试夹具。

## 两套会话夹具，严格分工

| fixture      | 语义                      | 用于                        |
|--------------|---------------------------|-----------------------------|
| `db_session` | SAVEPOINT + 结束 rollback | 纯逻辑、权限矩阵、schema 校验 |
| `clean_db`   | **真提交** + 测试前 TRUNCATE | **幂等 / 并发 / 恢复**       |

⚠️ **为什么不能用 rollback 夹具跑幂等测试**

常见做法是「每个测试包在一个最外层事务里，结束时 rollback」。这套做法
对幂等测试是**灾难性**的:

  1. rollback 掉的数据从未提交过，而幂等要验证的恰恰是
     「跨事务、跨进程的**已提交**副作用最多发生一次」——
     被测的性质在这种夹具下根本不存在。
  2. 并发测试需要两条独立连接抢同一个键。包在单一事务里做不到。
  3. 最坏的情况是**静默通过**:上一轮残留的行会让
     `ON CONFLICT DO NOTHING` 恰好表现得「正确」，
     于是一个本该失败的测试变绿。

因此凡是断言副作用次数的测试，一律用 `clean_db`。

## schema 用迁移建，不用 metadata.create_all()

`create_all()` 会绕过迁移文件直接按模型建表。那样一来，
「唯一约束到底有没有写进迁移」这件事永远得不到验证——
而那个约束正是 AGENTS.md 受保护区域的核心资产。
"""

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.tenancy.scope import install_tenant_guard

BACKEND_DIR = Path(__file__).resolve().parents[1]


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> dict[str, type[asyncio.AbstractEventLoop]] | None:
    """Windows 上强制使用 SelectorEventLoop。

    psycopg 的异步模式**无法在 ProactorEventLoop 上工作**（连接时直接抛
    `InterfaceError`），而 ProactorEventLoop 恰恰是 Windows 上 asyncio 的默认。

    这不是测试专属问题——生产环境跑 uvicorn 同样会撞上，
    对应的处理见 `app/asyncio_compat.py`。

    用这个 hook 而非老的 `event_loop_policy` fixture:后者在
    pytest-asyncio 1.x 已弃用，且事件循环策略机制本身在 Python 3.14 也在退场。
    """
    if sys.platform != "win32":
        return None
    return {"selector": asyncio.SelectorEventLoop}


#: 需要在 clean_db 里清空的业务表。
#:
#: 用 TRUNCATE 而非 DELETE 有一个非显然的原因:audit_log 上的追加写触发器
#: 是 `BEFORE UPDATE OR DELETE`，DELETE 会被它拒绝。TRUNCATE 触发的是
#: TRUNCATE 触发器（我们没建），因此可以清空。
#:
#: 顺带说明这一保证的真实边界:TRUNCATE 确实能绕过追加写触发器，
#: 但它需要表 owner 权限且是全表级操作——无法选择性篡改某几条记录，
#: 与「悄悄改掉一条审计记录」的威胁模型不是一回事。
_TRUNCATE_SQL = text(
    """
    TRUNCATE TABLE
        audit_log, review_plan_request, sampling_review, sampling_audit,
        review, review_sampling_plan, review_sampling_config,
        capability_declaration, evidence_step,
        correlation_finding, finding, field_availability, row_result, expense_row,
        file_version, policy_clause, policy_document, rule_config, schema_mapping,
        schema_mapping_version,
        user_session, app_user, tenant
    RESTART IDENTITY CASCADE
    """
)


#: docker-compose.yml 里由 init 脚本预建的测试库
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/expenseguard_test"
)


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    """测试数据库地址。

    解析顺序:

      1. `TEST_DATABASE_URL` —— 显式指定（CI 用）
      2. `USE_TESTCONTAINERS=1` —— 起一次性容器，隔离性最好
      3. 默认 —— 连 docker-compose 预建的 `expenseguard_test` 库

    **为什么默认不是 testcontainers。** 本项目的开发流程本来就要求
    `docker compose up`（应用自己要连 postgres 和 qdrant），所以
    「先起 compose」并不是额外负担；而 testcontainers 在本项目的
    Windows + Docker Desktop 环境下实测会挂死——容器正常启动、迁移
    也跑完了，但 pytest 之后既不再连库也不产出任何输出（详见
    `specs/001-phase1-foundation.md` 的已知问题）。

    保留 `USE_TESTCONTAINERS=1` 这条路径，是因为它在 Linux CI 上工作正常，
    且能提供更强的隔离（每次全新实例，不受上一轮残留影响）。
    """
    override = os.getenv("TEST_DATABASE_URL")
    if override:
        yield override
        return

    if os.getenv("USE_TESTCONTAINERS") == "1":
        from testcontainers.postgres import PostgresContainer

        # 镜像与 docker-compose.yml 保持一致，
        # 避免「本地 PG16 过、生产 PG17 挂」这类版本漂移
        with PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as container:
            yield container.get_connection_url()
        return

    yield DEFAULT_TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated_db(db_url: str) -> str:
    """把 schema 迁移到最新版本。

    走 `alembic upgrade head` 而非 `metadata.create_all()`——
    见模块 docstring 的说明。
    """
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    return db_url


@pytest.fixture(scope="session")
def alembic_config(migrated_db: str) -> Config:
    """已指向测试库的 Alembic 配置，供 `alembic check` 类断言使用。"""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", migrated_db)
    return cfg


@pytest.fixture(scope="session")
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    """会话级异步引擎。"""
    eng = create_async_engine(migrated_db, pool_pre_ping=True)
    # 与生产一致地装上租户过滤守卫——否则测试环境比生产宽松，
    # 「忘了注入租户」的 bug 就测不出来。
    install_tenant_guard()
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """会话工厂。"""
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> AsyncIterator[None]:
    """把库清空，随后测试中的写入**真实提交**。

    幂等 / 并发 / 崩溃恢复类测试必须用这个夹具:它们断言的是
    已提交副作用的次数，而 rollback 夹具下那些副作用根本不存在。
    """
    async with engine.begin() as conn:
        await conn.execute(_TRUNCATE_SQL)
    yield


@pytest.fixture
async def db_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """带自动回滚的会话，快。

    适合纯逻辑、权限矩阵、schema 断言这类**不关心提交语义**的测试。
    ⚠️ 不要用它测幂等。
    """
    async with session_factory() as session:
        yield session
        await session.rollback()
