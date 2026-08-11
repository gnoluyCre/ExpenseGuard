"""Cross-cutting HTTP safety, correlation, drain and abuse-protection middleware."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.errors import ErrorDetail, ErrorResponse
from app.core.observability.logging import bind_request_id, reset_request_id
from app.core.runtime import DrainController
from app.settings import Settings

logger = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class SlidingWindowLimiter:
    """Bounded in-memory limiter for the documented single-process MVP deployment."""

    def __init__(self, *, max_keys: int = 10_000) -> None:
        self._entries: OrderedDict[str, deque[float]] = OrderedDict()
        self._max_keys = max_keys
        self._lock = asyncio.Lock()

    async def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            samples = self._entries.pop(key, deque())
            while samples and samples[0] <= cutoff:
                samples.popleft()
            allowed = len(samples) < limit
            if allowed:
                samples.append(now)
            self._entries[key] = samples
            while len(self._entries) > self._max_keys:
                self._entries.popitem(last=False)
            return allowed


class RuntimeBoundaryMiddleware(BaseHTTPMiddleware):
    """Apply safe headers, stable request IDs, drain semantics and bounded rate limits."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: Settings,
        drain: DrainController,
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._drain = drain
        self._limiter = limiter or SlidingWindowLimiter()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = self._request_id(request)
        token = bind_request_id(request_id)
        started = time.perf_counter()
        try:
            blocked = await self._blocked_response(request)
            response = blocked if blocked is not None else await call_next(request)
            self._secure(response, request_id=request_id)
            logger.info(
                "http.request",
                extra={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "http_method": request.method,
                    "http_status": response.status_code,
                    "path": request.url.path,
                },
            )
            return response
        finally:
            reset_request_id(token)

    async def _blocked_response(self, request: Request) -> Response | None:
        if request.method not in _SAFE_METHODS and self._drain.is_draining:
            return _error(503, "SERVICE_DRAINING", "服务正在安全退出，请稍后重试")
        if not self._settings.rate_limit_enabled:
            return None
        key, limit = await self._rate_limit_identity(request)
        if key is None:
            return None
        allowed = await self._limiter.allow(
            key,
            limit=limit,
            window_seconds=self._settings.rate_limit_window_seconds,
        )
        if allowed:
            return None
        return _error(429, "RATE_LIMITED", "请求过于频繁，请稍后重试", retry_after="60")

    async def _rate_limit_identity(self, request: Request) -> tuple[str | None, int]:
        client = request.client.host if request.client is not None else "unknown"
        if request.url.path == "/api/auth/login" and request.method == "POST":
            identity = "unknown"
            try:
                payload = await request.json()
                if isinstance(payload, dict):
                    tenant = payload.get("tenant_slug")
                    username = payload.get("username")
                    if isinstance(tenant, str) and isinstance(username, str):
                        identity = f"{tenant.casefold()}\0{username.casefold()}"
            except ValueError:
                pass
            digest = hashlib.sha256(identity.encode()).hexdigest()
            return f"login:{client}:{digest}", self._settings.login_rate_limit
        if request.method in _SAFE_METHODS:
            return None, 0
        session = request.cookies.get("eg_session", "")
        digest = hashlib.sha256(session.encode()).hexdigest() if session else client
        return f"write:{digest}", self._settings.write_rate_limit

    @staticmethod
    def _request_id(request: Request) -> str:
        candidate = request.headers.get("X-Request-ID", "")
        return candidate if _REQUEST_ID.fullmatch(candidate) else uuid.uuid4().hex

    @staticmethod
    def _secure(response: Response, *, request_id: str) -> None:
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after: str | None = None,
) -> JSONResponse:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(),
        headers=headers,
    )
