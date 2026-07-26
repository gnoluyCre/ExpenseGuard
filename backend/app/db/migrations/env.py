"""Alembic 迁移环境。

## 与 LangGraph checkpoint 表的隔离（三层防御）

`PostgresSaver.setup()` 会自行创建 checkpoints / checkpoint_blobs /
checkpoint_writes / checkpoint_migrations 四张表。它们不归 Alembic 管，
但如果 Alembic 能「看见」它们，autogenerate 就会生成 `DROP TABLE checkpoints`
—— 一次 upgrade 就能抹掉全部断点数据。

三层防御:
  1. checkpointer 的连接串强制 `search_path=langgraph`，
     使那四张表落在独立 schema（见 `Settings.checkpoint_url`）
  2. 本文件的 engine 强制 `search_path=public`，反射不到 langgraph schema
  3. `include_object` 黑名单兜底，防止有人误改上面两项

机械化验证:集成测试 `test_alembic_unaffected_by_checkpoint_tables`
会在 `setup()` 之后跑 `alembic check`，要求输出「无待生成操作」。
"""

from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool

import app.db.models  # noqa: F401  导入全部模型以填充 metadata
from app.db.base import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: LangGraph 自行管理的表。即便前两层防御失效，也不允许 Alembic 碰它们。
CHECKPOINT_TABLES = frozenset(
    {
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
        "checkpoint_migrations",
    }
)

#: Alembic 自己的版本追踪表。
#:
#: 正常情况下 Alembic 会自动把它排除在 autogenerate 之外，但那套逻辑按
#: (schema, name) 比对:我们用 connect_args 把 search_path 固定成 public 后，
#: 反射回来的 schema 是 None 而非 "public"，比对不上 → 它会被当成
#: 「数据库里有、模型里没有」的多余表，生成 `op.drop_table('alembic_version')`。
#: 那条语句一旦执行，迁移追踪就彻底失效。这里显式排除，不依赖那套比对。
INTERNAL_TABLES = frozenset({"alembic_version"})


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """第三层防御:排除 checkpoint 表、Alembic 版本表与非 public schema。"""
    if type_ == "table":
        schema = getattr(obj, "schema", None)
        if schema not in (None, "public"):
            return False
        if name in CHECKPOINT_TABLES or name in INTERNAL_TABLES:
            return False
    return True


def _database_url() -> str:
    """解析数据库连接串。

    优先级:
      1. 调用方显式注入的 `sqlalchemy.url`（测试用 —— testcontainers
         起的临时库地址在运行时才知道，只能这样传进来）
      2. 应用配置（正常路径）

    连接串**不写在 alembic.ini 里**:凭据只有环境变量一个来源，
    alembic.ini 因此可以安全地进仓库。
    """
    override = config.get_main_option("sqlalchemy.url", None)
    if override:
        return override
    return get_settings().sync_database_url


def run_migrations_offline() -> None:
    """离线模式:只生成 SQL，不连数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        # 不设 version_table_schema：连接的 search_path 已固定为 public，
        # 显式指定反而会与反射结果（schema=None）不匹配。
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        # 检测列类型变更。默认关闭，会漏掉 VARCHAR(64) → VARCHAR(128) 这类改动。
        compare_type=True,
        # 不设 version_table_schema：连接的 search_path 已固定为 public，
        # 显式指定反而会与反射结果（schema=None）不匹配。
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式:连库执行迁移。

    用**同步** engine —— psycopg3 的同一个 dialect 名同时支持同步与异步，
    所以 Alembic 不必套 async 模板，少一层复杂度。
    """
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # 第二层防御:Alembic 的连接只看 public schema
        connect_args={"options": "-csearch_path=public"},
    )

    with connectable.connect() as connection:
        _do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
