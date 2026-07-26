"""LangGraph checkpoint 存储。

## 与业务表的隔离

checkpoint 表（checkpoints / checkpoint_blobs / checkpoint_writes /
checkpoint_migrations）由 `AsyncPostgresSaver.setup()` 自行创建，
**不归 Alembic 管**。它们与业务表同库、但在独立的 `langgraph` schema 里。

隔离靠连接串上的 `search_path=langgraph`（见 `Settings.checkpoint_url`）。
这是三层防御的第一层，另两层在 `app/db/migrations/env.py`。

隔离的意义:若 checkpoint 表落在 public，Alembic 的 autogenerate 会认为
它们是「数据库有、模型没有」的多余表，从而生成 `DROP TABLE checkpoints`
—— 一次 upgrade 就抹掉全部断点数据。

## 连接池的三个必需参数

`autocommit=True`
    `setup()` 内部执行 DDL 且自行管理事务边界，连接处于隐式事务中会报错。

`row_factory=dict_row`
    LangGraph 的 checkpoint 读取代码按字段名取值，默认的 tuple 行不兼容。

`prepare_threshold=0`
    关闭 psycopg 的自动 prepared statement。经过连接池复用时，
    prepared statement 会与 `search_path` 等会话状态产生难以排查的耦合。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.settings import Settings


def build_checkpoint_pool(settings: Settings) -> AsyncConnectionPool:
    """创建 checkpoint 专用连接池（尚未打开）。

    `open=False` 让调用方决定打开时机 —— 在 FastAPI 里应该由 lifespan
    管理，而不是在模块导入时就建立数据库连接。
    """
    return AsyncConnectionPool(
        conninfo=settings.checkpoint_url,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )


@asynccontextmanager
async def checkpointer(settings: Settings) -> AsyncIterator[AsyncPostgresSaver]:
    """产出一个已完成建表的 checkpointer，退出时关闭连接池。

    `setup()` 是幂等的:它内部有自己的迁移表，重复调用只会跳过。
    """
    pool = build_checkpoint_pool(settings)
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
    finally:
        await pool.close()
