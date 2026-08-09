from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError

from app.core.detection.canonical import (
    canonical_json,
    canonical_sha256,
    finding_identity_payload,
    profile_canonical_json,
    profile_fingerprint,
)
from app.core.detection.engine import run_detection_core
from app.core.detection.models import DetectionProfileDefinition, DetectorKind
from tests.unit.detection.helpers import batch, profile, profile_data, record

pytestmark = pytest.mark.unit


def test_profile_canonical_order_and_fingerprint_are_stable() -> None:
    data = profile_data()
    detectors = data["detectors"]
    assert isinstance(detectors, list)
    first = DetectionProfileDefinition.model_validate(data)
    data["detectors"] = list(reversed(detectors))
    second = DetectionProfileDefinition.model_validate(data)

    assert tuple(item.type for item in first.detectors) == tuple(DetectorKind)
    assert first == second
    assert profile_canonical_json(first) == profile_canonical_json(second)
    assert profile_fingerprint(first) == profile_fingerprint(second)
    assert profile_fingerprint(first) == (
        "2726a521a98c485e7f04f1e6b1f884549900210ba51f2afd2733de1a6d62b3ae"
    )


def test_semantic_profile_change_changes_fingerprint() -> None:
    baseline = profile()
    changed = profile(split_invoice={"date_window_days": 3})
    assert profile_fingerprint(baseline) != profile_fingerprint(changed)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update({"unexpected": True}),
        lambda data: data.update({"detectors": data["detectors"][:-1]}),
        lambda data: data.update({"detectors": [*data["detectors"], data["detectors"][0]]}),
    ],
)
def test_profile_rejects_unknown_missing_and_duplicate_detectors(mutation: object) -> None:
    data = profile_data()
    assert callable(mutation)
    mutation(data)
    with pytest.raises(ValidationError):
        DetectionProfileDefinition.model_validate(data)


@pytest.mark.parametrize(
    ("detector", "updates"),
    [
        ("split_invoice", {"approval_thresholds": {"CNY": "1e3"}}),
        ("split_invoice", {"approval_thresholds": {"CNY": 1000.0}}),
        ("split_invoice", {"fixed_currency": "CNY", "currency_mode": "field"}),
        (
            "sequential_invoice",
            {"numeric_suffix_min_digits": 4, "numeric_suffix_max_digits": 3},
        ),
        ("sequential_invoice", {"partition_fields": ["employee", "employee"]}),
        ("frequency_anomaly", {"mad_multiplier": "0"}),
        ("spatiotemporal_tier0", {"incompatible_zone_pairs": [["BJS", "BJS"]]}),
        ("spatiotemporal_tier0", {"incompatible_zone_pairs": [["BJS", "UNKNOWN"]]}),
    ],
)
def test_profile_boundaries_fail_closed(detector: str, updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        profile(**{detector: updates})


def test_zone_pairs_are_canonical_and_duplicates_rejected() -> None:
    canonical = profile(spatiotemporal_tier0={"incompatible_zone_pairs": [["SHA", "BJS"]]})
    detector = canonical.detectors[-1]
    assert detector.type is DetectorKind.SPATIOTEMPORAL_TIER0
    assert detector.incompatible_zone_pairs == (("BJS", "SHA"),)
    with pytest.raises(ValidationError):
        profile(spatiotemporal_tier0={"incompatible_zone_pairs": [["SHA", "BJS"], ["BJS", "SHA"]]})


def test_canonical_encoder_supports_decimal_fraction_and_rejects_float() -> None:
    value = {"fraction": Fraction(2, 4), "decimal": Decimal("10.5000"), "set": {"b", "a"}}
    assert canonical_json(value) == (
        '{"decimal":"10.5","fraction":{"denominator":2,"numerator":1},"set":["a","b"]}'
    )
    assert canonical_sha256(value) == (
        "f13203d55a84dd730410b371f2e662746ca706bfea6dc7f760d72b5636f0c1d0"
    )
    with pytest.raises(TypeError, match="float"):
        canonical_json({"value": 0.1})
    with pytest.raises(TypeError, match="mapping keys"):
        canonical_json({1: "invalid"})
    with pytest.raises(TypeError, match="unsupported"):
        canonical_json(object())
    assert canonical_json({"integer_decimal": Decimal("10"), "negative_zero": Decimal("-0")}) == (
        '{"integer_decimal":"10","negative_zero":"0"}'
    )
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json(Decimal("NaN"))
    with pytest.raises(TypeError, match="unsupported"):
        canonical_json(b"bytes")


def test_finding_identity_rejects_invalid_participating_rows() -> None:
    result = run_detection_core(
        profile(),
        batch(
            [
                record(amount="500", employee="E", merchant="商户甲"),
                record(amount="500", employee="E", merchant="商户甲"),
            ]
        ),
    )
    finding = result.outputs[0].findings[0]
    with pytest.raises(ValueError, match="at least two"):
        finding_identity_payload(
            detector=finding.detector,
            detector_version=finding.detector_version,
            profile_fingerprint_value=finding.evidence.profile_fingerprint,
            participating_row_nos=(1,),
            evidence=finding.evidence,
        )


def test_profile_canonical_size_is_utf8_bytes() -> None:
    oversized = {f"alias-{index:04d}-{'地' * 300}": f"key-{index}" for index in range(1000)}
    with pytest.raises(ValueError, match="256 KiB"):
        profile_canonical_json(profile(split_invoice={"merchant_aliases": oversized}))
