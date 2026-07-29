"""Stable service-layer errors for F5 mutations and reads."""

from app.core.errors import ExpenseGuardError


class ReviewError(ExpenseGuardError):
    status_code = 409


class ReviewInputError(ReviewError):
    status_code = 422


class ReviewInternalError(ReviewError):
    status_code = 500

    def __init__(self) -> None:
        super().__init__(code="REVIEW_INTERNAL_ERROR", message="复核服务暂时不可用")
