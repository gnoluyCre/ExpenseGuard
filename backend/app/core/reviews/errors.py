"""Stable service-layer errors for F5 mutations and reads."""

from app.core.errors import ExpenseGuardError


class ReviewError(ExpenseGuardError):
    status_code = 409


class ReviewInputError(ReviewError):
    status_code = 422


class ReviewNotFoundError(ReviewError):
    status_code = 404


class ReviewInternalError(ReviewError):
    status_code = 500

    def __init__(
        self,
        *,
        code: str = "REVIEW_SUBMIT_FAILED",
        message: str = "复核提交暂时不可用",
    ) -> None:
        super().__init__(code=code, message=message)
