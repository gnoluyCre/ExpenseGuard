"""服务器入口 —— `python -m app`。

## 为什么不直接 `uvicorn app.main:app`

uvicorn 在 Windows 上**硬编码**使用 `ProactorEventLoop`，而 psycopg 的
异步模式无法在其上运行（详见 `app/asyncio_compat.py`）。更糟的是
这个坑只在**不带 `--reload`** 时出现——reload 走子进程模式会拿到
SelectorEventLoop，于是「开发正常、生产挂死」。

本入口把循环工厂显式传给 uvicorn，两种模式行为一致。

Linux/macOS 上 `loop_factory()` 返回 None，沿用 uvicorn 默认，无任何影响。
"""

import asyncio

import uvicorn

from app.asyncio_compat import loop_factory
from app.settings import get_settings


def main() -> None:
    """启动服务器。"""
    settings = get_settings()
    reload_enabled = settings.app_env == "dev"

    config = uvicorn.Config(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=reload_enabled,
        log_level=settings.log_level.lower(),
    )

    if reload_enabled:
        # reload 模式需要 uvicorn 的 supervisor 管理子进程，无法自己驱动循环。
        # 好在子进程模式（use_subprocess=True）本身就会选 SelectorEventLoop，
        # 所以这条路径不需要额外处理。
        uvicorn.Server(config).run()
        return

    # 非 reload 模式:自己驱动事件循环，显式指定工厂。
    # 这是绕开 uvicorn 硬编码 ProactorEventLoop 的唯一可靠方式——
    # 事件循环策略在 asyncio.run(loop_factory=...) 路径下会被忽略。
    asyncio.run(uvicorn.Server(config).serve(), loop_factory=loop_factory())


if __name__ == "__main__":
    main()
