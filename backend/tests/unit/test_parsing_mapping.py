"""F2 映射完整性、推断与单行解析测试。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.parsing.mapping import (
    MappingValidationError,
    compute_header_signature,
    parse_expense_row,
    validate_mapping,
)
from app.core.parsing.models import MappingVersionConfig, UnifiedField

pytestmark = pytest.mark.unit
MAPPING_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
UPLOADED_AT = datetime(2026, 7, 27, tzinfo=UTC)
HEADERS = ("金额", "日期", "商户")


def _config(**overrides: object) -> MappingVersionConfig:
    data: dict[str, object] = {
        "id": MAPPING_ID,
        "version": 1,
        "header_signature": compute_header_signature(HEADERS),
        "mappings": [
            {"source_column": "金额", "target_field": "amount"},
            {"source_column": "日期", "target_field": "expense_date"},
            {"source_column": "商户", "target_field": "merchant"},
        ],
        "availability_thresholds": {
            "available_min_non_null_rate": "0.8000",
            "inferred_min_success_rate": "0.8000",
        },
        "currency_aliases": {"RMB": "CNY"},
        "inference_config": {"rules": []},
    }
    data.update(overrides)
    return MappingVersionConfig.model_validate(data)


@pytest.mark.parametrize(
    ("mappings", "code"),
    [
        (
            [{"source_column": "日期", "target_field": "expense_date"}],
            "MAPPING_REQUIRED_FIELD_MISSING",
        ),
        (
            [
                {"source_column": "金额", "target_field": "amount"},
                {"source_column": "日期", "target_field": "expense_date"},
                {"source_column": "未知", "target_field": "merchant"},
            ],
            "MAPPING_SOURCE_COLUMN_UNKNOWN",
        ),
        (
            [
                {"source_column": "金额", "target_field": "amount"},
                {"source_column": "金额", "target_field": "expense_date"},
            ],
            "MAPPING_SOURCE_COLUMN_DUPLICATED",
        ),
        (
            [
                {"source_column": "金额", "target_field": "amount"},
                {"source_column": "日期", "target_field": "expense_date"},
                {"source_column": "商户", "target_field": "amount"},
            ],
            "MAPPING_TARGET_FIELD_DUPLICATED",
        ),
    ],
)
def test_映射完整性返回稳定错误码(mappings: list[dict[str, str]], code: str) -> None:
    config = _config(mappings=mappings)
    with pytest.raises(MappingValidationError) as exc:
        validate_mapping(config, HEADERS)
    assert exc.value.code == code


def test_推断不能覆盖直接映射字段() -> None:
    config = _config(
        inference_config={
            "rules": [
                {
                    "rule_id": "merchant-v1",
                    "type": "literal_lookup",
                    "target_field": "merchant",
                    "source_fields": ["merchant"],
                    "cases": [{"literal": "酒店", "value": "上海"}],
                }
            ]
        }
    )
    with pytest.raises(MappingValidationError) as exc:
        validate_mapping(config, HEADERS)
    assert exc.value.code == "MAPPING_INFERENCE_INVALID"


def test_nfkc_后重复的币种别名会被拒绝() -> None:
    config = _config(currency_aliases={"￥": "CNY", "¥": "CNY"})
    with pytest.raises(MappingValidationError) as exc:
        validate_mapping(config, HEADERS)
    assert exc.value.code == "MAPPING_INFERENCE_INVALID"


def test_未命中的非法推断输出也会在解析前被拒绝() -> None:
    config = _config(
        inference_config={
            "rules": [
                {
                    "rule_id": "location-invalid",
                    "type": "literal_lookup",
                    "target_field": "location",
                    "source_fields": ["merchant"],
                    "cases": [{"literal": "不会命中", "value": "字" * 513}],
                }
            ]
        }
    )
    with pytest.raises(MappingValidationError) as exc:
        validate_mapping(config, HEADERS, uploaded_at=UPLOADED_AT)
    assert exc.value.code == "MAPPING_INFERENCE_INVALID"


def test_字面量首命中与常量推断可追溯() -> None:
    config = _config(
        inference_config={
            "rules": [
                {
                    "rule_id": "location-v1",
                    "type": "literal_lookup",
                    "target_field": "location",
                    "source_fields": ["merchant"],
                    "cases": [
                        {"literal": "上海", "value": "上海"},
                        {"literal": "酒店", "value": "其他"},
                    ],
                },
                {
                    "rule_id": "currency-v1",
                    "type": "constant",
                    "target_field": "currency",
                    "value": "RMB",
                },
            ]
        }
    )
    validate_mapping(config, HEADERS)
    result = parse_expense_row(
        {"金额": "1,234.50", "日期": "2026/7/1", "商户": "上海 酒店"},
        config=config,
        uploaded_at=UPLOADED_AT,
    )
    assert result.normalized is not None
    assert result.normalized.amount == "1234.5"
    assert result.normalized.location == "上海"
    assert result.normalized.currency == "CNY"
    assert result.normalized.field_provenance[UnifiedField.LOCATION].model_dump() == {
        "mode": "inferred",
        "source_columns": ("商户",),
        "inference_rule_id": "location-v1",
    }
    dumped = result.normalized.model_dump(mode="json")
    assert set(dumped) >= {field.value for field in UnifiedField}
    assert dumped["employee"] is None


def test_多字段错误按统一字段顺序且不复制原值() -> None:
    result = parse_expense_row(
        {"金额": "secret-invalid", "日期": None, "商户": "正常"},
        config=_config(),
        uploaded_at=UPLOADED_AT,
    )
    assert result.normalized is None
    assert result.error_detail is not None
    assert [error.field for error in result.error_detail.errors] == [
        UnifiedField.AMOUNT,
        UnifiedField.EXPENSE_DATE,
    ]
    detail = result.error_detail.model_dump(mode="json")
    assert "secret-invalid" not in str(detail)


def test_阈值越界由_pydantic_拒绝() -> None:
    with pytest.raises(ValidationError):
        _config(
            availability_thresholds={
                "available_min_non_null_rate": "1.0001",
                "inferred_min_success_rate": "0.8",
            }
        )
