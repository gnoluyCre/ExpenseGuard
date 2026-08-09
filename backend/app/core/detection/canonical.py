"""Fail-closed canonical JSON and stable F6 identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from pydantic import BaseModel

from app.core.detection.models import (
    MAX_CANONICAL_BYTES,
    CorrelationEvidence,
    DetectionProfileDefinition,
    DetectorKind,
)

_FINDING_DOMAIN = b"expenseguard-correlation-finding-v1\0"


def canonical_value(value: object) -> object:
    """Convert only explicitly supported values into JSON-compatible primitives."""
    if isinstance(value, BaseModel):
        return canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mapping keys must be strings")
        return {
            key: canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: pair[0])
        }
    if isinstance(value, set | frozenset):
        converted = [canonical_value(item) for item in value]
        return sorted(converted, key=canonical_json)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not canonical")
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return "0" if rendered in {"-0", ""} else rendered
    if isinstance(value, Fraction):
        return {"denominator": value.denominator, "numerator": value.numerator}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, float):
        raise TypeError("float is forbidden in F6 canonical values")
    if value is None or isinstance(value, bool | int | str):
        return value
    raise TypeError(f"unsupported canonical type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def profile_canonical_json(profile: DetectionProfileDefinition) -> str:
    encoded = canonical_bytes(profile)
    if len(encoded) > MAX_CANONICAL_BYTES:
        raise ValueError("DETECTION_CONFIG_INVALID: canonical JSON 超过 256 KiB")
    return encoded.decode("utf-8")


def profile_fingerprint(profile: DetectionProfileDefinition) -> str:
    return hashlib.sha256(profile_canonical_json(profile).encode("utf-8")).hexdigest()


def group_key_fingerprint(value: object) -> str:
    return canonical_sha256(value)


def finding_identity_payload(
    *,
    detector: DetectorKind,
    detector_version: str,
    profile_fingerprint_value: str,
    participating_row_nos: Sequence[int],
    evidence: CorrelationEvidence,
) -> bytes:
    rows = tuple(sorted(participating_row_nos))
    if len(rows) < 2 or len(rows) != len(set(rows)) or any(row_no < 1 for row_no in rows):
        raise ValueError("finding identity requires at least two unique positive rows")
    return b"".join(
        (
            _FINDING_DOMAIN,
            canonical_bytes(detector),
            canonical_bytes(detector_version),
            canonical_bytes(profile_fingerprint_value),
            canonical_bytes(rows),
            canonical_bytes(evidence.facts),
        )
    )


def finding_key(
    *,
    detector: DetectorKind,
    detector_version: str,
    profile_fingerprint_value: str,
    participating_row_nos: Sequence[int],
    evidence: CorrelationEvidence,
) -> str:
    return hashlib.sha256(
        finding_identity_payload(
            detector=detector,
            detector_version=detector_version,
            profile_fingerprint_value=profile_fingerprint_value,
            participating_row_nos=participating_row_nos,
            evidence=evidence,
        )
    ).hexdigest()
