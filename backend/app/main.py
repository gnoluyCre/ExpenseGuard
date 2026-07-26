"""FastAPI 应用装配。

⚠️ Windows 上必须用 `python -m app` 启动，不能直接 `uvicorn app.main:app`——
uvicorn 在 Windows 硬编码 ProactorEventLoop，而 psycopg 异步模式无法在其上
运行。原因与修法见 `app/asyncio_compat.py` 与 `app/__main__.py`。

这里刻意**不**设置事件循环策略:uvicorn 0.36+ 走
`asyncio.run(..., loop_factory=...)`，该路径完全绕过策略，
在这里设置只会制造「看起来处理过了」的假象。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from app.api.errors import register_error_handlers
from app.api.routes import auth, health
from app.core.tenancy.scope import install_tenant_guard
from app.db.engine import create_engine_from_settings, create_session_factory
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def _operation_id(route: APIRoute) -> str:
    """为 OpenAPI 生成简洁的 operationId。

    默认规则会产出 `login_api_auth_login_post` 这种把路径和方法都拼进去
    的名字，前端生成的客户端方法名会非常难看。改成 `auth_login` 形式。
    """
    tag = route.tags[0] if route.tags else "default"
    return f"{tag}_{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期:装配资源，退出时释放。

    数据库引擎在这里创建而非模块导入时——否则仅仅 import 这个模块
    （比如导出 OpenAPI 的脚本）就会尝试连数据库。
    """
    settings = get_settings()

    # 装上租户过滤守卫。必须在任何查询发生之前。
    install_tenant_guard()

    engine = create_engine_from_settings(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    logger.info("应用启动完成 env=%s", settings.app_env)

    try:
        yield
    finally:
        # 优雅退出:释放连接池
        await engine.dispose()
        logger.info("应用已退出")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用实例。"""
    settings = settings or get_settings()

    app = FastAPI(
        title="ExpenseGuard API",
        description="面向内部财务团队的费用报销预审系统",
        version="0.1.0",
        lifespan=lifespan,
        # 关掉输入/输出 schema 分离。开着的话同一个 Pydantic 模型会生成
        # FooModel-Input / FooModel-Output 两份，前端类型凭空多一倍。
        separate_input_output_schemas=False,
        generate_unique_id_function=_operation_id,
    )

    app.add_middleware(
        CORSMiddleware,
        # 必须是显式白名单:带 cookie 的跨域请求下通配符 "*" 无效，
        # 浏览器会直接拒绝。
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app


app = create_app()
