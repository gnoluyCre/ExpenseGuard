"""F2 全字段三级可用性探测测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.parsing.availability import detect_field_availability
from app.core.parsing.mapping import parse_expense_row, validate_mapping
from app.core.parsing.models import UNIFIED_FIELDS
from tests.unit.test_parsing_mapping import HEADERS, UPLOADED_AT, _config

pytestmark = pytest.mark.unit


def test_可用性覆盖十二字段且分母包含失败行() -> None:
    config = _config(
        inference_config={
            "rules": [
                {
                    "rule_id": "location-v1",
                    "type": "literal_lookup",
                    "target_field": "location",
                    "source_fields": ["merchant"],
                    "cases": [{"literal": "酒店", "value": "上海"}],
                }
            ]
        }
    )
    validate_mapping(config, HEADERS)
    raw_rows = tuple(
        {"金额": "bad" if index == 4 else "100", "日期": "2026-07-01", "商户": "酒店"}
        for index in range(5)
    )
    parsed = tuple(
        parse_expense_row(row, config=config, uploaded_at=UPLOADED_AT) for row in raw_rows
    )
    results = detect_field_availability(parsed, config=config, total_rows=5)

    assert [result.field_name for result in results] == list(UNIFIED_FIELDS)
    by_name = {result.field_name.value: result for result in results}
    assert by_name["amount"].status == "available"
    assert by_name["amount"].evidence.direct.model_dump(mode="json") == {
        "configured": True,
        "source_columns": ["金额"],
        "non_null_count": 4,
        "non_null_rate": "0.8000",
        "threshold": "0.8000",
    }
    assert by_name["expense_date"].evidence.direct.non_null_count == 5
    assert by_name["location"].status == "inferred"
    assert by_name["location"].evidence.selected_basis == "inference"
    assert by_name["employee"].status == "missing"
    assert "酒店" not in str(by_name["location"].evidence)


def test_直接映射低于阈值不会回退推断() -> None:
    config = _config(
        availability_thresholds={
            "available_min_non_null_rate": "1.0",
            "inferred_min_success_rate": "0.0",
        }
    )
    parsed = tuple(
        parse_expense_row(
            {"金额": "bad" if index == 0 else "1", "日期": "2026-07-01", "商户": "酒店"},
            config=config,
            uploaded_at=datetime(2026, 7, 27, tzinfo=UTC),
        )
        for index in range(2)
    )
    amount = detect_field_availability(parsed, config=config, total_rows=2)[0]
    assert amount.status == "missing"
    assert amount.evidence.selected_basis == "none"
