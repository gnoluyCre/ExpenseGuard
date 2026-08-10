"""Mechanical last-mile checks before any cloud-model request."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from app.core.errors import ExpenseGuardError


class OutboundSafetyError(ExpenseGuardError):
    status_code = 409


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_NATIONAL_ID = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


def assert_safe_outbound_text(text: str, *, forbidden_values: Iterable[str] = ()) -> None:
    """Reject known plaintext and high-confidence Chinese identifier patterns."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    for value in forbidden_values:
        candidate = unicodedata.normalize("NFKC", value).strip().casefold()
        if candidate and candidate in normalized:
            raise OutboundSafetyError(
                code="INVESTIGATION_PII_OUTBOUND_BLOCKED",
                message="模型出站载荷包含未脱敏字段，已阻止调用",
            )
    if _PHONE.search(text) or _NATIONAL_ID.search(text):
        raise OutboundSafetyError(
            code="INVESTIGATION_IDENTIFIER_OUTBOUND_BLOCKED",
            message="模型出站载荷包含疑似敏感标识，已阻止调用",
        )
