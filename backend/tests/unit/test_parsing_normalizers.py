"""F2 确定性字段归一化测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.core.parsing.normalizers import (
    NormalizationError,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_invoice_no,
    normalize_text,
)

pytestmark = pytest.mark.unit
UPLOADED_AT = datetime(2026, 7, 27, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1234, "1234"),
        (1234.5, "1234.5"),
        (Decimal("1234.5000"), "1234.5"),
        ("1,234.5000", "1234.5"),
        ("(123.45)", "-123.45"),
        ("-0.0000", "0"),
        ("CNY 1,234.50", "1234.5"),
        ("￥1,234.50", "1234.5"),
        ("12345678901234.1234", "12345678901234.1234"),
    ],
)
def test_金额合法边界(value: object, expected: str) -> None:
    assert normalize_amount(value, currency_markers=frozenset({"CNY", "￥"})) == expected


@pytest.mark.parametrize(
    "value",
    [None, True, float("nan"), float("inf"), "1e3", "12,34.50", "( -1.00 )", "1元"],
)
def test_金额非法格式返回稳定错误码(value: object) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_amount(value)
    assert exc.value.code == "AMOUNT_INVALID_FORMAT"


@pytest.mark.parametrize("value", ["1234567890123456789", "1.12345"])
def test_金额超精度返回稳定错误码(value: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_amount(value)
    assert exc.value.code == "AMOUNT_OUT_OF_RANGE"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 7, 1), "2026-07-01"),
        (datetime(2026, 7, 1, 23, 30), "2026-07-01"),
        ("2026-07-01T23:30:00-08:00", "2026-07-01"),
        ("2026/7/1", "2026-07-01"),
        ("2026.7.1", "2026-07-01"),
        ("2026年7月1日", "2026-07-01"),
        ("20260701", "2026-07-01"),
        ("2027-07-28", "2027-07-28"),
    ],
)
def test_日期合法格式与范围(value: object, expected: str) -> None:
    assert normalize_date(value, uploaded_at=UPLOADED_AT) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        46000,
        "03/04/2026",
        "2026-02-30",
        "2026-07-01T10:00:00+99:99",
    ],
)
def test_日期非法格式返回稳定错误码(value: object) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_date(value, uploaded_at=UPLOADED_AT)
    assert exc.value.code == "DATE_INVALID_FORMAT"


@pytest.mark.parametrize("value", ["1899-12-31", "2027-07-29"])
def test_日期超范围返回稳定错误码(value: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        normalize_date(value, uploaded_at=UPLOADED_AT)
    assert exc.value.code == "DATE_OUT_OF_RANGE"


def test_文本发票号与币种归一化() -> None:
    assert normalize_text("  Ａ公司\u3000 上海\t店  ") == "A公司 上海 店"
    assert normalize_text(" \t ") is None
    assert normalize_invoice_no(" ００12 ab-中 文 ") == "0012AB-中文"
    assert normalize_currency("人民币", aliases={"人民币": "CNY"}) == "CNY"
    assert normalize_currency("usd", aliases={}) == "USD"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: normalize_text("字" * 513), "TEXT_TOO_LONG"),
        (lambda: normalize_invoice_no("1" * 129), "TEXT_TOO_LONG"),
        (lambda: normalize_currency("人民币", aliases={}), "CURRENCY_INVALID"),
    ],
)
def test_文本类边界返回稳定错误码(call, code: str) -> None:
    with pytest.raises(NormalizationError) as exc:
        call()
    assert exc.value.code == code
