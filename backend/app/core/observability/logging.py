"""PII-safe JSON logging and request/task correlation context."""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("task_id", default=None)


class JsonFormatter(logging.Formatter):
    """Emit one compact JSON object per record without serializing arbitrary extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        request_id = _request_id.get()
        task_id = _task_id.get()
        if request_id is not None:
            payload["request_id"] = request_id
        if task_id is not None:
            payload["task_id"] = task_id
        for name in (
            "code",
            "duration_ms",
            "estimated_cost_usd",
            "http_method",
            "http_status",
            "model",
            "file_version_id",
            "investigation_run_id",
            "path",
            "phase",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "provider",
            "step_no",
            "task_id",
        ):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info is not None:
            exception_type = record.exc_info[0]
            if exception_type is not None:
                payload["exception_type"] = exception_type.__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_json_logging(level: str) -> None:
    """Replace root handlers with the application's deterministic JSON formatter."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def bind_request_id(value: str) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def bind_task_id(value: str) -> Token[str | None]:
    return _task_id.set(value)


def reset_task_id(token: Token[str | None]) -> None:
    _task_id.reset(token)
