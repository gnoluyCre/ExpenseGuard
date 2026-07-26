"""把 FastAPI 的 OpenAPI 文档导出成仓库里的 `openapi.json`。

用法::

    cd backend && uv run python scripts/export_openapi.py          # 写入
    cd backend && uv run python scripts/export_openapi.py --check  # 只校验，不写

## 为什么要把生成物提交进仓库

`openapi.json` 是前后端之间的**契约**。提交它之后，CI 可以重新导出再
`git diff --exit-code`：后端改了 schema 却没同步前端类型，流水线立刻变红，
而不是等到前端运行时才发现字段对不上。

## 稳定输出是这套门禁成立的前提

`sort_keys=True` 不是审美选择。FastAPI 构造 schema 时 dict 的插入顺序会随
路由注册顺序、Pydantic 内部实现变化而抖动；不排序的话，一次无害的重构就能
制造一份满屏乱序 diff，门禁随即被当成噪音关掉。同理固定 `indent`、
`ensure_ascii=False`（中文 description 保持可读）与末尾换行。

## 导入不会连数据库

引擎在 `lifespan` 里创建而非模块导入时，所以这个脚本不需要 postgres 在跑——
CI 的 contract job 因此可以不起任何服务容器。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# 脚本从 backend/ 运行，需要把 backend/ 放进 sys.path 才能 import app。
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.main import create_app  # noqa: E402  (必须在 sys.path 调整之后)

#: 契约文件放仓库根 —— 它同时属于前后端，不偏袒任何一侧。
OPENAPI_PATH = _BACKEND_DIR.parent / "openapi.json"


def build_openapi() -> dict[str, Any]:
    """构造 OpenAPI 文档。"""
    return create_app().openapi()


def render(schema: dict[str, Any]) -> str:
    """序列化成稳定文本。改这里的任何参数都会让全仓库 diff 一次性爆炸。"""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="导出 OpenAPI 契约")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对不写入；有漂移则以非零退出码结束（CI 用）",
    )
    args = parser.parse_args()

    content = render(build_openapi())

    if args.check:
        if not OPENAPI_PATH.exists():
            sys.stderr.write(f"{OPENAPI_PATH} 不存在 —— 请先运行不带 --check 的导出\n")
            return 1
        if OPENAPI_PATH.read_text(encoding="utf-8") != content:
            sys.stderr.write(
                f"{OPENAPI_PATH} 与当前代码不一致 —— "
                "请运行 `uv run python scripts/export_openapi.py` 并提交结果\n"
            )
            return 1
        sys.stderr.write("OpenAPI 契约无漂移\n")
        return 0

    OPENAPI_PATH.write_text(content, encoding="utf-8")
    sys.stderr.write(f"已写入 {OPENAPI_PATH}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
