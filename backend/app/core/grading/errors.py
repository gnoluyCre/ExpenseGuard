"""Stable service-layer errors for F8 grading."""

from app.core.errors import ExpenseGuardError


class GradingError(ExpenseGuardError):
    status_code = 409


class GradingInputError(GradingError):
    status_code = 422


class GradingNotFoundError(GradingError):
    status_code = 404


class GradingUnavailableError(GradingError):
    status_code = 503


class GradingInternalError(GradingError):
    status_code = 500

    def __init__(
        self,
        *,
        code: str = "GRADING_RUN_FAILED",
        message: str = "二维分级暂时不可用，未保存不完整结果",
    ) -> None:
        super().__init__(code=code, message=message)
