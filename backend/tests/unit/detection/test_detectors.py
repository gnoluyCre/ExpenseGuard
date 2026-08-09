from __future__ import annotations

from copy import deepcopy

import pytest

from app.core.detection.engine import run_detection_core
from app.core.detection.models import (
    CapabilityReason,
    CapabilityStatus,
    DetectionBatch,
    DetectorKind,
    ParseErrorSourceRow,
)
from app.core.parsing.models import UnifiedField
from tests.unit.detection.helpers import batch, profile, record

pytestmark = pytest.mark.unit


def _output(result: object, detector: DetectorKind):
    outputs = result.outputs
    return next(item for item in outputs if item.declaration.detector is detector)


def test_split_gte_floor_alias_window_and_golden_identity() -> None:
    records = [
        record(
            amount="500",
            employee="E-split",
            merchant="商户甲",
            expense_date="2026-01-10",
            invoice_no="BAD-A",
        ),
        record(
            amount="500",
            employee="E-split",
            merchant="商户甲分店",
            expense_date="2026-01-12",
            invoice_no="BAD-B",
        ),
        record(
            amount="999",
            employee="E-split",
            merchant="商户甲",
            expense_date="2026-01-15",
            invoice_no="BAD-C",
        ),
        record(amount="1000", employee="E-noise", merchant="商户甲", invoice_no="BAD-D"),
        record(amount="0", employee="E-noise", merchant="商户甲", invoice_no="BAD-E"),
    ]
    result = run_detection_core(profile(), batch(records))
    output = _output(result, DetectorKind.SPLIT_INVOICE)

    assert output.declaration.status is CapabilityStatus.ENABLED
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.participating_row_nos == (1, 2)
    assert finding.evidence.facts.total == "1000"
    assert finding.finding_key == (
        "3e45c0b24805731d3aa7dcaf420ddf6063739df9fc6e9a916df2fff305b45bef"
    )


def test_split_gt_boundary_and_non_overlapping_windows() -> None:
    records = [
        record(amount="500", employee="E", merchant="商户甲", expense_date="2026-01-01"),
        record(amount="500", employee="E", merchant="商户甲", expense_date="2026-01-02"),
        record(amount="600", employee="E", merchant="商户甲", expense_date="2026-01-10"),
        record(amount="500", employee="E", merchant="商户甲", expense_date="2026-01-11"),
    ]
    result = run_detection_core(profile(split_invoice={"aggregate_operator": "gt"}), batch(records))
    findings = _output(result, DetectorKind.SPLIT_INVOICE).findings
    assert [item.participating_row_nos for item in findings] == [(3, 4)]


def test_split_missing_threshold_degrades_when_coverage_survives() -> None:
    records = [
        record(amount="600", employee="E", merchant="商户甲", currency="CNY"),
        record(amount="500", employee="E", merchant="商户甲", currency="CNY"),
        record(amount="600", employee="E2", merchant="商户甲", currency="USD"),
    ]
    output = _output(run_detection_core(profile(), batch(records)), DetectorKind.SPLIT_INVOICE)
    assert output.declaration.status is CapabilityStatus.DEGRADED
    assert output.declaration.reason_code is CapabilityReason.THRESHOLD_CURRENCY_UNCONFIGURED
    assert len(output.findings) == 1


def test_sequential_ascii_width_duplicate_gap_partition_and_golden() -> None:
    records = [
        record(employee="E", merchant="M", invoice_no="INV001"),
        record(employee="E", merchant="M", invoice_no="INV002"),
        record(employee="E", merchant="M", invoice_no="INV002"),
        record(employee="E", merchant="M", invoice_no="INV003"),
        record(employee="E", merchant="M", invoice_no="INV005"),
        record(employee="E", merchant="M", invoice_no="INV٠٠٦"),
        record(employee="E2", merchant="M", invoice_no="INV004"),
        record(employee="E", merchant="M", invoice_no="INV0007"),
    ]
    output = _output(
        run_detection_core(
            profile(sequential_invoice={"numeric_suffix_max_digits": 4}), batch(records)
        ),
        DetectorKind.SEQUENTIAL_INVOICE,
    )
    assert output.declaration.status is CapabilityStatus.DEGRADED
    assert output.declaration.details.runtime.serial_unparseable_count == 1
    assert output.declaration.details.runtime.duplicate_serial_row_count == 1
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.participating_row_nos == (1, 2, 3, 4)
    assert finding.evidence.facts.ordered_row_nos == (1, 2, 3, 4)
    assert finding.evidence.facts.start_serial == "001"
    assert finding.finding_key == (
        "570b543b99f9070f7efa6b7010803f8e6b76edddf97fa7adebf2857b6e3a48a7"
    )


def test_frequency_fraction_mad_equal_multiplier_month_and_golden() -> None:
    records = [
        *[
            record(employee="A", expense_date="2026-01-31", invoice_no=f"A{index:03d}")
            for index in range(1, 4)
        ],
        record(employee="B", expense_date="2026-01-01", invoice_no="B001"),
        record(employee="C", expense_date="2026-01-02", invoice_no="C001"),
    ]
    output = _output(
        run_detection_core(
            profile(frequency_anomaly={"mad_multiplier": "2", "mad_floor": "1"}),
            batch(records),
        ),
        DetectorKind.FREQUENCY_ANOMALY,
    )
    assert output.declaration.status is CapabilityStatus.ENABLED
    assert len(output.findings) == 1
    facts = output.findings[0].evidence.facts
    assert facts.median.model_dump() == {"numerator": 1, "denominator": 1}
    assert facts.mad.model_dump() == {"numerator": 0, "denominator": 1}
    assert facts.observed_ratio.model_dump() == {"numerator": 2, "denominator": 1}
    assert output.findings[0].finding_key == (
        "ccc86453fbd295bad7abda49244c51574c3039d589d3b5fb063375303a8ec3aa"
    )


def test_frequency_week_monday_and_partial_period_degradation() -> None:
    records = [
        record(employee="A", expense_date="2025-12-29"),
        record(employee="B", expense_date="2025-12-30"),
        record(employee="C", expense_date="2026-01-04"),
        record(employee="A", expense_date="2026-01-05"),
    ]
    output = _output(
        run_detection_core(
            profile(
                frequency_anomaly={
                    "period": "calendar_week_monday",
                    "absolute_min_count": 2,
                }
            ),
            batch(records),
        ),
        DetectorKind.FREQUENCY_ANOMALY,
    )
    assert output.declaration.status is CapabilityStatus.DEGRADED
    assert output.declaration.reason_code is CapabilityReason.PARTIAL_PERIOD_SKIPPED
    assert output.declaration.details.runtime.period_count == 2


def test_spatiotemporal_exact_alias_multiple_pairs_merge_and_golden() -> None:
    configured = profile(
        spatiotemporal_tier0={
            "location_aliases": {"北京": "BJS", "上海": "SHA", "深圳": "SZX"},
            "incompatible_zone_pairs": [["BJS", "SHA"], ["BJS", "SZX"]],
        }
    )
    records = [
        record(employee="E", expense_date="2026-01-10", location="北京"),
        record(employee="E", expense_date="2026-01-10", location="上海"),
        record(employee="E", expense_date="2026-01-10", location="深圳"),
        record(employee="E", expense_date="2026-01-10", location="北京"),
        record(employee="E", expense_date="2026-01-11", location="上海"),
        record(employee="E", expense_date="2026-01-10", location="上海 "),
    ]
    output = _output(
        run_detection_core(configured, batch(records)), DetectorKind.SPATIOTEMPORAL_TIER0
    )
    assert output.declaration.status is CapabilityStatus.DEGRADED
    assert len(output.findings) == 1
    finding = output.findings[0]
    assert finding.participating_row_nos == (1, 2, 3, 4)
    assert finding.evidence.facts.incompatible_zone_pairs == (("BJS", "SHA"), ("BJS", "SZX"))
    assert finding.finding_key == (
        "3f9e41e5851cd433a3388b9a4ef2c83ea428f0aaeb93e2ca6650060edbc87ce6"
    )


def test_capability_lattice_disabled_missing_inferred_and_zero_findings() -> None:
    records = [record(employee="A"), record(employee="B"), record(employee="C")]
    disabled = _output(
        run_detection_core(profile(split_invoice={"enabled": False}), batch(records)),
        DetectorKind.SPLIT_INVOICE,
    ).declaration
    assert disabled.status is CapabilityStatus.UNAVAILABLE
    assert disabled.reason_code is CapabilityReason.CONFIG_DISABLED

    missing = _output(
        run_detection_core(profile(), batch(records, missing=[UnifiedField.EMPLOYEE])),
        DetectorKind.FREQUENCY_ANOMALY,
    ).declaration
    assert missing.status is CapabilityStatus.UNAVAILABLE
    assert missing.reason_code is CapabilityReason.REQUIRED_FIELD_MISSING

    inferred = _output(
        run_detection_core(profile(), batch(records, inferred=[UnifiedField.EMPLOYEE])),
        DetectorKind.FREQUENCY_ANOMALY,
    ).declaration
    assert inferred.status is CapabilityStatus.DEGRADED
    assert inferred.finding_count == 0


def test_parse_error_counts_in_denominator_and_insufficient_coverage_stops_detector() -> None:
    valid = batch([record(employee="A"), record(employee="B")])
    rows = (
        *valid.rows,
        ParseErrorSourceRow(row_no=3, error_code="ROW_PARSE_FAILED"),
        ParseErrorSourceRow(row_no=4, error_code="ROW_PARSE_FAILED"),
    )
    with_errors = DetectionBatch(rows=rows, field_availability=valid.field_availability)
    output = _output(
        run_detection_core(profile(frequency_anomaly={"min_eligible_rate_bps": 7500}), with_errors),
        DetectorKind.FREQUENCY_ANOMALY,
    )
    assert output.declaration.status is CapabilityStatus.UNAVAILABLE
    assert output.declaration.reason_code is CapabilityReason.INSUFFICIENT_ELIGIBLE_ROWS
    assert output.declaration.details.source_row_count == 4
    assert output.declaration.details.parsed_row_count == 2
    assert output.findings == ()


def test_fixed_currency_conflict_and_zone_mapping_floor_are_explicit() -> None:
    records = [
        record(employee="E", merchant="商户甲", amount="600", currency="USD", location="北京"),
        record(employee="E", merchant="商户甲", amount="500", currency="CNY", location="未知"),
        record(employee="E", merchant="商户甲", amount="500", currency=None, location="上海"),
    ]
    configured = profile(
        split_invoice={"currency_mode": "fixed", "fixed_currency": "CNY"},
        spatiotemporal_tier0={"min_zone_mapping_rate_bps": 9000},
    )
    result = run_detection_core(configured, batch(records))
    split = _output(result, DetectorKind.SPLIT_INVOICE).declaration
    assert split.status is CapabilityStatus.DEGRADED
    assert split.reason_code is CapabilityReason.CURRENCY_CONFLICT
    zone = _output(result, DetectorKind.SPATIOTEMPORAL_TIER0).declaration
    assert zone.status is CapabilityStatus.UNAVAILABLE
    assert CapabilityReason.ZONE_MAPPING_BELOW_MINIMUM in zone.details.causes


def test_frequency_without_sufficient_population_is_unavailable() -> None:
    output = _output(
        run_detection_core(
            profile(frequency_anomaly={"min_population": 4}),
            batch([record(employee="A"), record(employee="B"), record(employee="C")]),
        ),
        DetectorKind.FREQUENCY_ANOMALY,
    )
    assert output.declaration.reason_code is CapabilityReason.INSUFFICIENT_POPULATION
    assert output.findings == ()


def test_input_order_does_not_change_finding_bytes_or_order() -> None:
    records = [
        record(amount="500", employee="E", merchant="商户甲", expense_date="2026-01-01"),
        record(amount="600", employee="E", merchant="商户甲", expense_date="2026-01-02"),
        record(employee="A", invoice_no="INV001"),
        record(employee="A", invoice_no="INV002"),
        record(employee="A", invoice_no="INV003"),
    ]
    first_batch = batch(records)
    second_data = first_batch.model_dump(mode="python")
    second_data = deepcopy(second_data)
    second_data["rows"] = list(reversed(second_data["rows"]))
    second_data["field_availability"] = list(reversed(second_data["field_availability"]))
    second_batch = type(first_batch).model_validate(second_data)
    first = run_detection_core(profile(), first_batch)
    second = run_detection_core(profile(), second_batch)
    assert first == second
