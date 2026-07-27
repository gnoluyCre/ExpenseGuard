"""Excel 批次导入的纯逻辑测试。"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.core.batches.importer import (
    BatchImportError,
    cell_to_json,
    parse_xlsx,
    sha256_hex,
)

pytestmark = pytest.mark.unit


def _xlsx_bytes(*, rows: int, headers: tuple[object, ...] = ("员工", "金额")) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(headers)
    for index in range(rows):
        sheet.append((f"员工{index}", index))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_文件哈希稳定() -> None:
    content = b"same file"
    assert sha256_hex(content) == sha256_hex(content)
    assert sha256_hex(content) != sha256_hex(b"other file")


@pytest.mark.parametrize(
    ("rows", "code"),
    [
        (499, "BATCH_ROW_COUNT_TOO_LOW"),
        (5001, "BATCH_ROW_COUNT_TOO_HIGH"),
    ],
)
def test_行数边界会返回稳定错误码(rows: int, code: str) -> None:
    with pytest.raises(BatchImportError) as exc:
        parse_xlsx(_xlsx_bytes(rows=rows))

    assert exc.value.code == code


def test_合法行数保留_excel_物理行号() -> None:
    parsed = parse_xlsx(_xlsx_bytes(rows=500))

    assert parsed.row_count == 500
    assert parsed.rows[0].row_no == 2
    assert parsed.rows[-1].row_no == 501
    assert parsed.rows[0].raw_json == {"员工": "员工0", "金额": 0}


def test_空表头会返回稳定错误码() -> None:
    with pytest.raises(BatchImportError) as exc:
        parse_xlsx(_xlsx_bytes(rows=500, headers=("员工", None)))

    assert exc.value.code == "BATCH_HEADER_EMPTY"


def test_重复表头会返回稳定错误码() -> None:
    with pytest.raises(BatchImportError) as exc:
        parse_xlsx(_xlsx_bytes(rows=500, headers=("员工", "员工")))

    assert exc.value.code == "BATCH_HEADER_DUPLICATED"


def test_坏_workbook_会返回稳定错误码() -> None:
    with pytest.raises(BatchImportError) as exc:
        parse_xlsx(b"not an xlsx")

    assert exc.value.code == "BATCH_XLSX_INVALID"


def test_excel_cell_转换为_jsonb_安全值() -> None:
    assert cell_to_json(None) is None
    assert cell_to_json("文本") == "文本"
    assert cell_to_json(True) is True
    assert cell_to_json(3) == 3
    assert cell_to_json(3.5) == 3.5
    assert cell_to_json(Decimal("12.30")) == "12.30"
    assert cell_to_json(date(2026, 7, 27)) == "2026-07-27"
    assert cell_to_json(time(9, 30)) == "09:30:00"
    assert cell_to_json(datetime(2026, 7, 27, 9, 30)) == "2026-07-27T09:30:00"
