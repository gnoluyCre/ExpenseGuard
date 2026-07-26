"""领域错误基类。

统一的错误 shape 使前端可以按 code 映射用户安全提示，
而开发者上下文留在服务端日志里。

原则（AGENTS.md）：绝不静默吞掉错误；AI 相关失败的回退一律是
「转人工 + 显式标注」，绝不是「猜一个结论」。
"""


class ExpenseGuardError(Exception):
    """所有领域错误的基类。

    Args:
        code: 稳定的机器可读错误码，前端据此映射提示文案。
        message: 用户安全的中文提示，不含内部细节。
    """

    #: HTTP 状态码，子类可覆盖
    status_code: int = 400

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class NotFoundError(ExpenseGuardError):
    """请求的资源不存在（或当前租户无权见到它）。"""

    status_code = 404


class AuthenticationError(ExpenseGuardError):
    """未认证或凭据无效。"""

    status_code = 401


class PermissionDeniedError(ExpenseGuardError):
    """已认证但角色权限不足。"""

    status_code = 403


class TenantScopeMissingError(ExpenseGuardError):
    """查询在没有租户上下文的情况下发起。

    这是 fail-closed 设计的核心：一个「忘了注入租户」的 bug 必须表现为 500，
    绝不能静默返回全租户数据。宁可报错，也不泄漏。
    """

    status_code = 500

    def __init__(self) -> None:
        super().__init__(
            code="TENANT_SCOPE_MISSING",
            message="内部错误：查询缺少租户上下文",
        )
