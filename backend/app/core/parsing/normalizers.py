"""确定性的金额、日期、文本、发票号和币种归一化。"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import NoReturn


class NormalizationError(ValueError):
    """携带稳定错误码和用户安全消息的字段数据错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_AMOUNT_RE = re.compile(r"^(?P<sign>-?)(?P<number>(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?)$")
_ISO_DATETIME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?$"
)
_YEAR_FIRST_DATE_RE = re.compile(
    r"^(?P<year>\d{4})(?:/(?P<slash_month>\d{1,2})/(?P<slash_day>\d{1,2})"
    r"|\.(?P<dot_month>\d{1,2})\.(?P<dot_day>\d{1,2})"
    r"|年(?P<cn_month>\d{1,2})月(?P<cn_day>\d{1,2})日)$"
)
_COMPACT_DATE_RE = re.compile(r"^(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_amount(value: object, *, currency_markers: frozenset[str] = frozenset()) -> str:
    """将支持的金额输入转换为无指数、无无意义尾零的十进制字符串。"""
    if value is None or isinstance(value, bool):
        _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")

    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")
        decimal_value = Decimal(str(value))
    elif isinstance(value, int | Decimal):
        decimal_value = Decimal(value)
        if not decimal_value.is_finite():
            _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")
    elif isinstance(value, str):
        decimal_value = _parse_amount_text(value, currency_markers=currency_markers)
    else:
        _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")

    normalized = format(decimal_value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if Decimal(normalized) == 0:
        normalized = "0"

    unsigned = normalized.lstrip("-")
    integer_part, _, fraction_part = unsigned.partition(".")
    significant_digits = (integer_part.lstrip("0") + fraction_part).lstrip("0") or "0"
    if len(significant_digits) > 18 or len(fraction_part) > 4:
        _fail("AMOUNT_OUT_OF_RANGE", "金额精度或有效位数超出允许范围")
    return normalized


def normalize_date(value: object, *, uploaded_at: datetime) -> str:
    """将不歧义日期归一化为 ISO 日期，并按批次上传时间检查范围。"""
    parsed: date
    if isinstance(value, bool | int | float | Decimal) or value is None:
        _fail("DATE_INVALID_FORMAT", "日期格式无法识别")
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        parsed = _parse_date_text(value)
    else:
        _fail("DATE_INVALID_FORMAT", "日期格式无法识别")

    uploaded_date = uploaded_at.astimezone(UTC).date() if uploaded_at.tzinfo else uploaded_at.date()
    if parsed < date(1900, 1, 1) or parsed > uploaded_date + timedelta(days=366):
        _fail("DATE_OUT_OF_RANGE", "日期超出允许的数据质量范围")
    return parsed.isoformat()


def normalize_text(value: object, *, max_length: int = 512) -> str | None:
    """NFKC、去首尾空白并折叠连续 Unicode 空白。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > max_length:
        _fail("TEXT_TOO_LONG", "文本长度超出允许范围")
    return text


def normalize_invoice_no(value: object) -> str | None:
    """规范化发票号，同时保留前导零。"""
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(character for character in text if not character.isspace())
    text = "".join(
        character.upper() if "a" <= character <= "z" else character for character in text
    )
    if not text:
        return None
    if len(text) > 128:
        _fail("TEXT_TOO_LONG", "发票号长度超出允许范围")
    return text


def normalize_currency(value: object, *, aliases: dict[str, str]) -> str | None:
    """按版本别名表转换币种，并校验为三字母大写代码。"""
    text = normalize_text(value, max_length=64)
    if text is None:
        return None
    normalized_aliases = {
        unicodedata.normalize("NFKC", key).strip().upper(): target.upper()
        for key, target in aliases.items()
    }
    candidate = normalized_aliases.get(text.upper(), text.upper())
    if not _CURRENCY_RE.fullmatch(candidate):
        _fail("CURRENCY_INVALID", "币种无法归一化为三字母代码")
    return candidate


def normalize_lookup_text(value: object) -> str:
    """推断匹配专用 NFKC 文本；不改变大小写或做正则解释。"""
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value))


def _parse_amount_text(value: str, *, currency_markers: frozenset[str]) -> Decimal:
    text = unicodedata.normalize("NFKC", value).strip()
    if not text:
        _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")

    parenthesized = text.startswith("(") and text.endswith(")")
    if parenthesized:
        text = text[1:-1].strip()
    elif text.startswith("(") or text.endswith(")"):
        _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")

    normalized_markers = sorted(
        {unicodedata.normalize("NFKC", marker).strip() for marker in currency_markers if marker},
        key=len,
        reverse=True,
    )
    matched_marker = next(
        (marker for marker in normalized_markers if text.startswith(marker)), None
    )
    if matched_marker is not None:
        text = text[len(matched_marker) :].strip()

    match = _AMOUNT_RE.fullmatch(text)
    if match is None or (parenthesized and match.group("sign")):
        _fail("AMOUNT_INVALID_FORMAT", "金额格式无法识别")
    numeric_text = match.group("number").replace(",", "")
    if parenthesized:
        numeric_text = f"-{numeric_text}"
    try:
        return Decimal(numeric_text)
    except InvalidOperation as exc:  # pragma: no cover - regex 已限制输入
        raise NormalizationError("AMOUNT_INVALID_FORMAT", "金额格式无法识别") from exc


def _parse_date_text(value: str) -> date:
    text = unicodedata.normalize("NFKC", value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        datetime_match = _ISO_DATETIME_RE.fullmatch(text)
        if datetime_match is not None:
            # 只取字符串中写明的日历日期，但仍用标准库机械校验时间和偏移量。
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return date.fromisoformat(datetime_match.group("date"))
        compact_match = _COMPACT_DATE_RE.fullmatch(text)
        if compact_match is not None:
            return date(
                int(compact_match.group("year")),
                int(compact_match.group("month")),
                int(compact_match.group("day")),
            )
        year_first_match = _YEAR_FIRST_DATE_RE.fullmatch(text)
        if year_first_match is not None:
            groups = year_first_match.groupdict()
            month = groups["slash_month"] or groups["dot_month"] or groups["cn_month"]
            day = groups["slash_day"] or groups["dot_day"] or groups["cn_day"]
            return date(int(groups["year"]), int(month), int(day))
    except ValueError as exc:
        raise NormalizationError("DATE_INVALID_FORMAT", "日期格式无法识别") from exc
    _fail("DATE_INVALID_FORMAT", "日期格式无法识别")


def _fail(code: str, message: str) -> NoReturn:
    raise NormalizationError(code, message)
