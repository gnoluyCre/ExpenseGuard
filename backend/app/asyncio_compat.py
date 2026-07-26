"""Windows 事件循环兼容。

## 问题

psycopg 的异步模式**无法在 `ProactorEventLoop` 上运行**——连接时直接抛
`InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。

而 `ProactorEventLoop` 正是 Windows 上 asyncio 的**默认**事件循环
（Python 3.8 起）。也就是说:在 Windows 上什么都不做，整个异步数据库层
根本连不上。

## 处理

进程启动时把事件循环策略切成 `SelectorEventLoop`。必须在任何事件循环
被创建**之前**调用，因此 `app.main` 在模块导入时就调用它。

Linux / macOS 无此问题，函数在那里是空操作。

## 代价

`SelectorEventLoop` 在 Windows 上有 512 个文件描述符的上限
（`select()` 的限制）。对本项目——单机、个位数用户、单租户——毫无影响。
若将来 Windows 侧需要高并发，正确的做法是换 Linux 部署，
而不是换回 ProactorEventLoop（那样数据库层直接不可用）。
"""

import asyncio
import sys


def configure_event_loop_policy() -> None:
    """在 Windows 上切换到 psycopg 可用的事件循环策略。"""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
