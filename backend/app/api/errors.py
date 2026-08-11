"""统一的 API 错误响应。

所有错误走同一个 shape,前端据 `code` 映射用户提示文案,
而开发者上下文留在服务端日志里 —— 绝不把内部细节抛给浏览器。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.errors import ExpenseGuardError

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    """错误详情。"""

    code: str
    message: str


class ErrorResponse(BaseModel):
    """统一错误响应体。注册进 OpenAPI，使前端能生成对应类型。"""

    error: ErrorDetail


async def _handle_domain_error(request: Request, exc: Exception) -> JSONResponse:
    """领域错误 → 统一 JSON。"""
    # 不用 assert:`python -O` 会把 assert 整条剥掉，
    # 那时类型收窄失效而代码仍在跑。显式判断才是可靠的。
    if not isinstance(exc, ExpenseGuardError):  # pragma: no cover
        raise exc
    # 5xx 记 error 级，4xx 是预期内的客户端问题，记 info 即可
    log = logger.error if exc.status_code >= 500 else logger.info
    log(
        "domain error: %s",
        exc.code,
        extra={"path": request.url.path, "code": exc.code},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=ErrorDetail(code=exc.code, message=exc.message)).model_dump(),
        headers=_grading_no_store_headers(request),
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """将 FastAPI/Pydantic 的输入错误收敛到项目统一错误 shape。"""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    logger.info("request validation error", extra={"path": request.url.path})
    if request.url.path == "/api/v1/grading-configs" and request.method == "POST":
        code = "GRADING_CONFIG_INVALID"
        message = "二维分级配置无效"
    elif "/grading-runs" in request.url.path and request.method == "POST":
        code = "GRADING_RUN_INVALID"
        message = "二维分级运行请求无效"
    elif request.method == "PUT" and request.url.path == "/api/detection/configs":
        code = "DETECTION_CONFIG_INVALID"
        message = "关联检测配置无效"
    elif request.method == "PUT" and request.url.path == "/api/rules":
        code = "RULE_CONFIG_INVALID"
        message = "规则配置无效"
    elif (
        request.method == "PUT"
        and request.url.path == "/api/review/sampling-config"
        and _has_body_error(exc)
    ):
        code = "SAMPLING_CONFIG_INVALID"
        message = "抽样配置无效"
    elif (
        request.method == "POST" and request.url.path.endswith("/decision") and _has_body_error(exc)
    ):
        code = "REVIEW_DECISION_INVALID"
        message = "复核结论无效"
    else:
        code = "REQUEST_VALIDATION_ERROR"
        message = "请求参数无效"
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(error=ErrorDetail(code=code, message=message)).model_dump(),
        headers=_grading_no_store_headers(request),
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Fail closed without exposing stack traces, SQL, paths or request content."""
    logger.error(
        "unexpected error",
        extra={"path": request.url.path, "code": "INTERNAL_ERROR"},
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error=ErrorDetail(code="INTERNAL_ERROR", message="服务遇到内部错误")
        ).model_dump(),
        headers=_grading_no_store_headers(request),
    )


def _has_body_error(exc: RequestValidationError) -> bool:
    return any(error.get("loc", (None,))[0] == "body" for error in exc.errors())


def _grading_no_store_headers(request: Request) -> dict[str, str] | None:
    path = request.url.path
    if path.startswith("/api/v1/grading-") or (
        path.startswith("/api/v1/files/") and "/grading-runs" in path
    ):
        return {"Cache-Control": "private, no-store"}
    return None


def register_error_handlers(app: FastAPI) -> None:
    """挂上领域错误处理器。"""
    app.add_exception_handler(ExpenseGuardError, _handle_domain_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
