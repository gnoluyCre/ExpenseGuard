from __future__ import annotations

from decimal import Decimal

import pytest

from app.core.grading.canonical import (
    canonical_bytes,
    canonical_json,
    config_fingerprint,
    source_fingerprint,
)
from app.core.grading.models import (
    CorrelationGradingSource,
    InvestigationNotRunSource,
    ParticipantRow,
)
from tests.unit.grading.helpers import config, hex64, stable_uuid


def test_canonical_json_is_compact_utf8_sorted_and_order_independent() -> None:
    left = {"z": {"乙", "甲"}, "a": Decimal("1.2300"), "unicode": "费用"}
    right = {"unicode": "费用", "a": Decimal("1.23"), "z": {"甲", "乙"}}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_bytes(left) == canonical_json(left).encode("utf-8")
    assert " " not in canonical_json({"a": 1, "b": 2})
    assert "费用" in canonical_json(left)


@pytest.mark.parametrize("value", [1.0, float("inf"), {1: "bad"}, object()])
def test_canonical_rejects_float_non_string_keys_and_unknown_types(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_json(value)


def test_config_fingerprint_is_stable_and_domain_separated() -> None:
    grading_config = config()
    assert config_fingerprint(grading_config) == config_fingerprint(
        grading_config.model_copy(deep=True)
    )
    assert config_fingerprint(grading_config) != source_fingerprint(
        CorrelationGradingSource(
            correlation_finding_id=stable_uuid(1),
            detector="split_invoice",
            finding_key=hex64(1),
            evidence_fingerprint=hex64(2),
            participating_rows=(
                ParticipantRow(row_no=1, source_row_fingerprint=hex64(3)),
                ParticipantRow(row_no=2, source_row_fingerprint=hex64(4)),
            ),
            capability_status="enabled",
            investigation=InvestigationNotRunSource(),
        )
    )


def test_large_bounded_source_is_fingerprintable_but_row_limit_fails_closed() -> None:
    rows = tuple(
        ParticipantRow(row_no=index, source_row_fingerprint=hex64(index))
        for index in range(1, 5001)
    )
    source = CorrelationGradingSource(
        correlation_finding_id=stable_uuid(1),
        detector="split_invoice",
        finding_key=hex64(1),
        evidence_fingerprint=hex64(2),
        participating_rows=rows,
        capability_status="enabled",
        investigation=InvestigationNotRunSource(),
    )
    assert len(source_fingerprint(source)) == 64
    with pytest.raises(ValueError, match="at most 5000"):
        CorrelationGradingSource(
            correlation_finding_id=stable_uuid(2),
            detector="split_invoice",
            finding_key=hex64(3),
            evidence_fingerprint=hex64(4),
            participating_rows=(
                *rows,
                ParticipantRow(row_no=5001, source_row_fingerprint=hex64(1)),
            ),
            capability_status="enabled",
            investigation=InvestigationNotRunSource(),
        )
