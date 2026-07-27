"""应用配置的单一入口。

所有配置从环境变量 / .env 读取，绝不硬编码凭据。
`extra="forbid"` 是刻意为之：环境变量名拼错会立刻报错，
而不是静默回退到默认值——后者在生产环境中极难排查。
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: backend/app/settings.py → backend/app → backend → 仓库根
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent


class Settings(BaseSettings):
    """从环境变量装配的应用配置。"""

    model_config = SettingsConfigDict(
        # 用绝对路径而非 ".env"：后端命令从 backend/ 运行，
        # 相对路径会找不到位于仓库根的 .env。
        # 元组中靠后的文件优先级更高，因此 backend/.env 可覆盖根级配置。
        env_file=(_REPO_ROOT / ".env", _BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    # —— 应用 ——
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # —— 数据库 ——
    # 业务表 + 审计日志走 public schema；LangGraph checkpoint 走 langgraph schema。
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://postgres:postgres@127.0.0.1:5432/expenseguard"),
    )

    # —— 会话 ——
    # session 存 PostgreSQL 表，不引入 Redis（TechDesign 明确：MVP 单机单租户不加组件）。
    # session 同时是审计对象，与 audit_log 同库同事务才能保证登录留痕可查、可备份。
    # cookie 名字**刻意不做成配置项** —— FastAPI 的 Cookie(alias=...)
    # 在导入时求值，拿不到运行时配置。见 core/security/session_service.py
    # 的 SESSION_COOKIE_NAME。
    session_cookie_secure: bool = False  # dev 为 false；prod 必须 true
    session_idle_timeout_seconds: int = 8 * 60 * 60  # 闲置 8 小时
    session_absolute_timeout_seconds: int = 12 * 60 * 60  # 绝对上限 12 小时
    session_touch_throttle_seconds: int = 60  # last_seen_at 写节流，避免 session 表成写热点

    # —— 多租户 ——
    # MVP 单租户运行，但所有业务表带 tenant_id，隔离方案在架构层内建。
    default_tenant_slug: str = "default"

    # —— CORS ——
    # 开发走 Vite proxy 同源，此项供非 proxy 场景使用。绝不使用通配符（因为要带 cookie）。
    #
    # NoDecode 关掉 pydantic-settings 对 list 字段的自动 JSON 解码。
    # 不加它的话，环境变量必须写成 '["http://a","http://b"]' 这种 JSON——
    # 对 .env 文件很不友好。关掉之后由下面的 before-validator 接管，
    # 支持更自然的逗号分隔写法。
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # —— 向量库 ——
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "policy_clauses"

    # —— 可观测 ——
    # Phase 1 默认关闭：此阶段无 LLM 调用，trace 消费者为 0。
    # 关闭时不得产生任何网络调用。
    tracing_enabled: bool = False
    otel_service_name: str = "expenseguard-api"
    otel_exporter_otlp_endpoint: str = ""

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """支持用逗号分隔的字符串配置 CORS 源（环境变量天然是字符串）。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def checkpoint_url(self) -> str:
        """LangGraph checkpoint 专用连接串。

        强制 search_path=langgraph，使 PostgresSaver.setup() 建的
        checkpoints / checkpoint_blobs / checkpoint_writes / checkpoint_migrations
        四张表落在独立 schema 中——这是 Alembic 与 checkpoint 表隔离的第一层防御，
        避免 autogenerate 生成 `DROP TABLE checkpoints`。
        """
        raw = str(self.database_url).replace("postgresql+psycopg://", "postgresql://", 1)
        sep = "&" if "?" in raw else "?"
        return f"{raw}{sep}options=-csearch_path%3Dlanggraph"

    @property
    def sync_database_url(self) -> str:
        """Alembic 用的同步连接串。

        psycopg3 的同一个 dialect 名同时支持同步与异步，
        因此 Alembic 不必用 async 模板，少一层复杂度。
        """
        return str(self.database_url)

    @property
    def upload_root(self) -> Path:
        """原始上传文件留存目录。该目录位于 gitignore 的 data/private 下。"""
        return _REPO_ROOT / "data" / "private" / "uploads"


@lru_cache
def get_settings() -> Settings:
    """返回进程内缓存的配置实例。"""
    return Settings()
