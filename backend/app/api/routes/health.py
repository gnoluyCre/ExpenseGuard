"""健康检查。

分两个端点，语义不同:

- `/api/health`       存活探针 —— 进程还在跑吗？不碰任何外部依赖
- `/api/health/ready` 就绪探针 —— 依赖都可用吗？逐个探测并如实汇报

分开的理由:存活探针失败应该导致**重启**，就绪探针失败只应该
**摘出流量**。把两者混在一起，会让一次数据库抖动引发容器无谓重启。

Qdrant 的健康检查也在这里做,而不是在 docker-compose 里——
官方镜像不含 curl/wget,compose 的 `CMD curl` 形式会直接失败。
"""

import asyncio
from enum import StrEnum

import httpx
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import SettingsDep
from app.settings import Settings

router = APIRouter(prefix="/api/health", tags=["health"])

#: 单个依赖的探测超时。宁可快速报「不可用」，也不要挂住整个探针。
_PROBE_TIMEOUT_SECONDS = 3.0


class DependencyStatus(StrEnum):
    """依赖状态。"""

    UP = "up"
    DOWN = "down"


class DependencyHealth(BaseModel):
    """单个依赖的健康状况。"""

    name: str
    status: DependencyStatus
    #: 不可用时的简短原因，供运维排查。不含凭据。
    detail: str | None = None


class LivenessResponse(BaseModel):
    """存活响应。"""

    status: str


class ReadinessResponse(BaseModel):
    """就绪响应。"""

    ready: bool
    dependencies: list[DependencyHealth]


@router.get("", response_model=LivenessResponse, name="liveness")
async def liveness() -> LivenessResponse:
    """存活探针。刻意不碰任何外部依赖。"""
    return LivenessResponse(status="ok")


async def _probe_postgres(request: Request) -> DependencyHealth:
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return DependencyHealth(
            name="postgres", status=DependencyStatus.DOWN, detail="session_factory 未装配"
        )
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS), factory() as session:
            await session.execute(text("SELECT 1"))
        return DependencyHealth(name="postgres", status=DependencyStatus.UP)
    except Exception as exc:
        return DependencyHealth(
            name="postgres", status=DependencyStatus.DOWN, detail=type(exc).__name__
        )


async def _probe_qdrant(settings: Settings) -> DependencyHealth:
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{settings.qdrant_url.rstrip('/')}/readyz")
        if resp.status_code == 200:
            return DependencyHealth(name="qdrant", status=DependencyStatus.UP)
        return DependencyHealth(
            name="qdrant", status=DependencyStatus.DOWN, detail=f"HTTP {resp.status_code}"
        )
    except Exception as exc:
        return DependencyHealth(
            name="qdrant", status=DependencyStatus.DOWN, detail=type(exc).__name__
        )


@router.get("/ready", response_model=ReadinessResponse, name="readiness")
async def readiness(
    request: Request,
    response: Response,
    settings: SettingsDep,
) -> ReadinessResponse:
    """就绪探针。并发探测各依赖，任一不可用即整体未就绪。"""
    checks = await asyncio.gather(
        _probe_postgres(request),
        _probe_qdrant(settings),
    )
    ready = all(c.status is DependencyStatus.UP for c in checks)
    # 未就绪返回 503，使负载均衡器/编排器能据状态码摘流量
    response.status_code = 200 if ready else 503
    return ReadinessResponse(ready=ready, dependencies=list(checks))
