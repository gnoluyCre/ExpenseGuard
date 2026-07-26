"""Windows 事件循环兼容。

## 问题

psycopg 的异步模式**无法在 `ProactorEventLoop` 上运行**。而 uvicorn 在
Windows 上会**硬编码**使用它（`uvicorn/loops/asyncio.py`）::

    def asyncio_loop_factory(use_subprocess: bool = False):
        if sys.platform == "win32" and not use_subprocess:
            return asyncio.ProactorEventLoop
        return asyncio.SelectorEventLoop

两个非显然的后果:

1. **`asyncio.set_event_loop_policy()` 对 uvicorn 无效。** uvicorn 0.36+
   改用 `asyncio.run(..., loop_factory=...)`，该路径完全绕过事件循环策略。
   在应用模块顶部设策略看似合理，实际一点用都没有。

2. **这是个「开发能跑、生产挂死」的陷阱。** `--reload` 走子进程模式
   （`use_subprocess=True`）→ 拿到 SelectorEventLoop → 正常；
   生产模式不带 reload → ProactorEventLoop → 所有数据库调用挂起至超时。

   实测:健康检查的 postgres 探针 3 秒超时，而同一条 `SELECT 1`
   在 Selector 循环上只要 0.03 秒。

## 正确做法

把循环工厂**显式传给 uvicorn**，不依赖策略、也不依赖它的平台判断。
见 `app/__main__.py`。

## 代价

`SelectorEventLoop` 在 Windows 上受 `select()` 的 512 个文件描述符限制。
对本项目——单机、个位数用户、单租户——毫无影响。若将来 Windows 侧
需要高并发，正确做法是换 Linux 部署，而不是换回 Proactor
（那样数据库层直接不可用）。

生产部署是 Docker/Linux，本模块在那里是空操作。
"""

import asyncio
import sys
from collections.abc import Callable


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """返回本平台应使用的事件循环工厂。

    Returns:
        Windows 上返回 `SelectorEventLoop`（psycopg 唯一可用的循环）；
        其它平台返回 None，表示沿用默认。
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop
    return None


def configure_event_loop_policy() -> None:
    """为**非 uvicorn**的入口设置事件循环策略。

    适用场景:脚本、`asyncio.run()` 直接调用、Alembic 的异步分支等——
    这些路径会走事件循环策略。

    ⚠️ **对 uvicorn 无效**，原因见模块 docstring。uvicorn 必须用
    `loop_factory()` 显式传入。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
