"""Strict fixed fixtures for CP-F6.2 golden vectors."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.core.detection.models import (
    AvailabilityStatus,
    DetectionBatch,
    DetectionProfileDefinition,
    FieldAvailabilitySnapshot,
    ParsedSourceRow,
)
from app.core.parsing.models import NormalizedExpenseRecord, UnifiedField

MAPPING_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def profile_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "algorithm_bundle_version": "correlation-v1",
        "detectors": [
            {
                "type": "split_invoice",
                "enabled": True,
                "min_eligible_rows": 2,
                "min_eligible_rate_bps": 1,
                "approval_thresholds": {"CNY": "1000.00"},
                "currency_mode": "field",
                "fixed_currency": None,
                "aggregate_operator": "gte",
                "individual_floor_bps": 5000,
                "date_window_days": 2,
                "min_rows": 2,
                "merchant_aliases": {"商户甲分店": "merchant-a", "商户甲": "merchant-a"},
            },
            {
                "type": "sequential_invoice",
                "enabled": True,
                "min_eligible_rows": 2,
                "min_eligible_rate_bps": 1,
                "min_sequence_length": 3,
                "numeric_suffix_min_digits": 3,
                "numeric_suffix_max_digits": 3,
                "partition_fields": ["employee", "merchant"],
            },
            {
                "type": "frequency_anomaly",
                "enabled": True,
                "min_eligible_rows": 2,
                "min_eligible_rate_bps": 1,
                "period": "calendar_month",
                "min_population": 3,
                "absolute_min_count": 3,
                "mad_multiplier": "2.0",
                "mad_floor": "1.0",
            },
            {
                "type": "spatiotemporal_tier0",
                "enabled": True,
                "min_eligible_rows": 2,
                "min_eligible_rate_bps": 1,
                "location_aliases": {"上海": "SHA", "北京": "BJS", "上海虹桥": "SHA"},
                "incompatible_zone_pairs": [["SHA", "BJS"]],
                "min_zone_mapping_rate_bps": 1,
            },
        ],
    }


def profile(**detector_updates: dict[str, object]) -> DetectionProfileDefinition:
    data = profile_data()
    detectors = data["detectors"]
    assert isinstance(detectors, list)
    for detector in detectors:
        assert isinstance(detector, dict)
        detector_type = detector["type"]
        assert isinstance(detector_type, str)
        detector.update(detector_updates.get(detector_type, {}))
    return DetectionProfileDefinition.model_validate(data)


def record(**overrides: object) -> NormalizedExpenseRecord:
    values: dict[str, object] = {
        "schema_version": 1,
        "mapping_version_id": MAPPING_ID,
        "amount": "100",
        "expense_date": "2026-01-10",
        "employee": "employee-default",
        "expense_type": "差旅",
        "invoice_type": "电子票",
        "invoice_no": "N900",
        "merchant": "商户默认",
        "invoice_title": "示例公司",
        "submission_date": "2026-01-11",
        "location": "未知地点",
        "currency": "CNY",
        "description": None,
    }
    values.update(overrides)
    provenance: dict[str, object] = {}
    for field in UnifiedField:
        if values[field.value] is not None:
            provenance[field.value] = {
                "mode": "mapped",
                "source_columns": [field.value],
                "inference_rule_id": None,
            }
    values["field_provenance"] = provenance
    return NormalizedExpenseRecord.model_validate(values)


def batch(
    records: Iterable[NormalizedExpenseRecord],
    *,
    inferred: Iterable[UnifiedField] = (),
    missing: Iterable[UnifiedField] = (),
) -> DetectionBatch:
    inferred_fields = set(inferred)
    missing_fields = set(missing)
    snapshots = tuple(
        FieldAvailabilitySnapshot(
            field_name=field,
            status=(
                AvailabilityStatus.MISSING
                if field in missing_fields
                else AvailabilityStatus.INFERRED
                if field in inferred_fields
                else AvailabilityStatus.AVAILABLE
            ),
        )
        for field in UnifiedField
    )
    return DetectionBatch(
        rows=tuple(
            ParsedSourceRow(row_no=row_no, normalized=item)
            for row_no, item in enumerate(records, start=1)
        ),
        field_availability=snapshots,
    )
