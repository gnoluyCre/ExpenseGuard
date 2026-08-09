"""Fixed, no-random physical patterns for F6 cross-row detector evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.core.detection.models import DetectorKind
from app.synth.generator import DATA_COLUMNS, DEFAULT_ANCHOR_DATE


class CorrelationScenario(StrEnum):
    SPLIT_BOUNDARY = "split_boundary"
    SEQUENTIAL_WITH_NOISE = "sequential_with_noise"
    FREQUENCY_WITH_BASELINE = "frequency_with_baseline"
    SPATIOTEMPORAL_WITH_UNMAPPED = "spatiotemporal_with_unmapped"


class CorrelationTruth(BaseModel):
    """Expected cross-row truth kept physically separate from input columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    detector: DetectorKind
    participating_row_nos: tuple[int, ...] = Field(min_length=2)
    boundary: str
    degraded_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CorrelationSyntheticCase:
    """One deterministic physical pattern with separate labels."""

    scenario: CorrelationScenario
    rows: tuple[dict[str, object], ...]
    truths: tuple[CorrelationTruth, ...]


def build_f6_synthetic_cases(
    *, anchor_date: date = DEFAULT_ANCHOR_DATE
) -> tuple[CorrelationSyntheticCase, ...]:
    """Build positive, negative, boundary, noise, and degraded F6 examples."""
    return (
        _split_case(anchor_date),
        _sequential_case(anchor_date),
        _frequency_case(anchor_date),
        _spatiotemporal_case(anchor_date),
    )


def _base_row(*, row_no: int, occurred: date) -> dict[str, object]:
    return {
        "报销单号": f"F6-{row_no:04d}",
        "员工工号": f"E{row_no:04d}",
        "员工姓名": f"合成员工{row_no:04d}",
        "部门": "财务部",
        "费用类型": "餐饮",
        "费用发生日期": occurred,
        "提交日期": occurred + timedelta(days=1),
        "金额": Decimal("100.00"),
        "币种": "CNY",
        "发票号码": f"NOISE-{row_no:04d}",
        "发票类型": "电子发票",
        "发票抬头": "示例科技（北京）有限公司",
        "商户名称": f"噪声商户{row_no:04d}",
        "发生地点": "未知地点",
        "事由": "F6 合成噪声样本",
    }


def _row(*, row_no: int, occurred: date, **updates: object) -> dict[str, object]:
    row = _base_row(row_no=row_no, occurred=occurred)
    row.update(updates)
    if tuple(row) != DATA_COLUMNS:
        raise ValueError("F6 synthetic row columns drifted from DATA_COLUMNS")
    return row


def _split_case(anchor: date) -> CorrelationSyntheticCase:
    rows = (
        _row(
            row_no=1,
            occurred=anchor,
            **{"员工姓名": "拆单员工", "金额": Decimal("500.00"), "商户名称": "商户甲"},
        ),
        _row(
            row_no=2,
            occurred=anchor + timedelta(days=2),
            **{"员工姓名": "拆单员工", "金额": Decimal("500.00"), "商户名称": "商户甲分店"},
        ),
        _row(
            row_no=3,
            occurred=anchor + timedelta(days=3),
            **{"员工姓名": "拆单员工", "金额": Decimal("999.00"), "商户名称": "商户甲"},
        ),
        _row(row_no=4, occurred=anchor, **{"金额": Decimal("0.00")}),
        _row(row_no=5, occurred=anchor, **{"金额": Decimal("1000.00")}),
    )
    return CorrelationSyntheticCase(
        scenario=CorrelationScenario.SPLIT_BOUNDARY,
        rows=rows,
        truths=(
            CorrelationTruth(
                detector=DetectorKind.SPLIT_INVOICE,
                participating_row_nos=(1, 2),
                boundary="gte_total_and_floor_equal",
            ),
        ),
    )


def _sequential_case(anchor: date) -> CorrelationSyntheticCase:
    rows = tuple(
        _row(
            row_no=index,
            occurred=anchor,
            **{
                "员工姓名": "连号员工",
                "商户名称": "连号商户",
                "发票号码": invoice,
            },
        )
        for index, invoice in enumerate(
            ("INV001", "INV002", "INV002", "INV003", "INV005", "INV٠٠٦"), start=1
        )
    )
    return CorrelationSyntheticCase(
        scenario=CorrelationScenario.SEQUENTIAL_WITH_NOISE,
        rows=rows,
        truths=(
            CorrelationTruth(
                detector=DetectorKind.SEQUENTIAL_INVOICE,
                participating_row_nos=(1, 2, 3, 4),
                boundary="duplicate_serial_does_not_extend_sequence",
                degraded_fields=("invoice_no_unparseable",),
            ),
        ),
    )


def _frequency_case(anchor: date) -> CorrelationSyntheticCase:
    employees = ("频次员工A", "频次员工A", "频次员工A", "频次员工B", "频次员工C")
    rows = tuple(
        _row(
            row_no=index,
            occurred=anchor.replace(day=index),
            **{"员工姓名": employee, "发票号码": f"FREQ{index:03d}"},
        )
        for index, employee in enumerate(employees, start=1)
    )
    return CorrelationSyntheticCase(
        scenario=CorrelationScenario.FREQUENCY_WITH_BASELINE,
        rows=rows,
        truths=(
            CorrelationTruth(
                detector=DetectorKind.FREQUENCY_ANOMALY,
                participating_row_nos=(1, 2, 3),
                boundary="mad_zero_uses_explicit_floor",
            ),
        ),
    )


def _spatiotemporal_case(anchor: date) -> CorrelationSyntheticCase:
    rows = (
        _row(row_no=1, occurred=anchor, **{"员工姓名": "时空员工", "发生地点": "北京"}),
        _row(row_no=2, occurred=anchor, **{"员工姓名": "时空员工", "发生地点": "上海"}),
        _row(row_no=3, occurred=anchor, **{"员工姓名": "时空员工", "发生地点": "未知地点"}),
        _row(row_no=4, occurred=anchor, **{"员工姓名": "其他员工", "发生地点": "北京"}),
    )
    return CorrelationSyntheticCase(
        scenario=CorrelationScenario.SPATIOTEMPORAL_WITH_UNMAPPED,
        rows=rows,
        truths=(
            CorrelationTruth(
                detector=DetectorKind.SPATIOTEMPORAL_TIER0,
                participating_row_nos=(1, 2),
                boundary="exact_alias_and_unmapped_noise",
                degraded_fields=("location_unmapped",),
            ),
        ),
    )
