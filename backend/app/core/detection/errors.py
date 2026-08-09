"""Stable service-layer errors for F6 detection."""

from app.core.errors import ExpenseGuardError


class DetectionError(ExpenseGuardError):
    status_code = 409


class DetectionInputError(DetectionError):
    status_code = 422


class DetectionNotFoundError(DetectionError):
    status_code = 404


class DetectionInternalError(DetectionError):
    status_code = 500

    def __init__(
        self,
        *,
        code: str = "DETECTION_RUN_FAILED",
        message: str = "关联检测暂时不可用",
    ) -> None:
        super().__init__(code=code, message=message)
