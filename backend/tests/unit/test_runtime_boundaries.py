from __future__ import annotations

import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api.errors import register_error_handlers
from app.api.middleware import RuntimeBoundaryMiddleware
from app.core.observability.logging import JsonFormatter, bind_request_id, reset_request_id
from app.core.runtime import DrainController
from app.settings import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _app(settings: Settings, drain: DrainController) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RuntimeBoundaryMiddleware, settings=settings, drain=drain)

    @app.get("/read")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/write")
    async def write() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/auth/login")
    async def login() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("private database detail")

    register_error_handlers(app)
    return app


def test_drain_controller_is_monotonic() -> None:
    drain = DrainController()
    assert not drain.is_draining
    assert drain.request_drain()
    first = drain.requested_at
    assert drain.is_draining
    assert not drain.request_drain()
    assert drain.requested_at == first


def test_production_security_and_tracing_configuration_fail_closed() -> None:
    with pytest.raises(ValidationError, match="Secure session cookie"):
        _settings(app_env="prod", policy_embedding_provider="http")
    with pytest.raises(ValidationError, match="OTLP endpoint"):
        _settings(tracing_enabled=True)
    settings = _settings(
        app_env="prod",
        policy_embedding_provider="http",
        session_cookie_secure=True,
        llm_input_price_per_million_usd="",
        llm_output_price_per_million_usd="",
    )
    assert settings.llm_input_price_per_million_usd is None


def test_json_formatter_whitelists_context_and_omits_arbitrary_extras() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "safe.event", (), None)
    record.secret = "must-not-appear"
    token = bind_request_id("request-1234")
    try:
        payload = json.loads(formatter.format(record))
    finally:
        reset_request_id(token)
    assert payload["event"] == "safe.event"
    assert payload["request_id"] == "request-1234"
    assert "secret" not in payload


@pytest.mark.asyncio
async def test_headers_request_id_rate_limit_drain_and_safe_500() -> None:
    drain = DrainController()
    app = _app(_settings(login_rate_limit=1), drain)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        read = await client.get("/read", headers={"X-Request-ID": "valid-request-123"})
        assert read.status_code == 200
        assert read.headers["X-Request-ID"] == "valid-request-123"
        assert read.headers["X-Content-Type-Options"] == "nosniff"
        assert read.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in read.headers["Content-Security-Policy"]

        assert (await client.post("/api/auth/login")).status_code == 200
        limited = await client.post("/api/auth/login")
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "RATE_LIMITED"

        failure = await client.get("/boom")
        assert failure.status_code == 500
        assert failure.json() == {
            "error": {"code": "INTERNAL_ERROR", "message": "服务遇到内部错误"}
        }
        assert "private database detail" not in failure.text

        drain.request_drain()
        assert (await client.get("/read")).status_code == 200
        blocked = await client.post("/write")
        assert blocked.status_code == 503
        assert blocked.json()["error"]["code"] == "SERVICE_DRAINING"
