"""Fail-closed canonical JSON and domain-separated F8 fingerprints."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import Enum
from fractions import Fraction

from pydantic import BaseModel

from app.core.grading.models import (
    MAX_CANONICAL_BYTES,
    MAX_SOURCE_CANONICAL_BYTES,
    GradingConfigV1,
    GradingInput,
    GradingSource,
)

_CONFIG_DOMAIN = b"expenseguard-grading-config-v1\0"
_SOURCE_DOMAIN = b"expenseguard-grading-source-v1\0"
_INPUT_DOMAIN = b"expenseguard-grading-input-v1\0"
_ITEM_DOMAIN = b"expenseguard-grading-item-v1\0"
_RESULT_DOMAIN = b"expenseguard-grading-result-v1\0"


def canonical_value(value: object) -> object:
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
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, float):
        raise TypeError("float is forbidden in F8 canonical values")
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


def _bounded_bytes(value: object, *, maximum: int = MAX_CANONICAL_BYTES) -> bytes:
    encoded = canonical_bytes(value)
    if len(encoded) > maximum:
        raise ValueError(f"canonical JSON exceeds {maximum} bytes")
    return encoded


def _domain_fingerprint(domain: bytes, value: object, *, bounded: bool = True) -> str:
    encoded = _bounded_bytes(value) if bounded else canonical_bytes(value)
    return hashlib.sha256(domain + encoded).hexdigest()


def _limited_domain_fingerprint(domain: bytes, value: object, maximum: int) -> str:
    return hashlib.sha256(domain + _bounded_bytes(value, maximum=maximum)).hexdigest()


def config_fingerprint(config: GradingConfigV1) -> str:
    return _domain_fingerprint(_CONFIG_DOMAIN, config)


def source_fingerprint(source: GradingSource) -> str:
    return _limited_domain_fingerprint(_SOURCE_DOMAIN, source, MAX_SOURCE_CANONICAL_BYTES)


def input_fingerprint(value: object) -> str:
    return _domain_fingerprint(_INPUT_DOMAIN, value, bounded=False)


def grading_input_fingerprint(grading_input: GradingInput) -> str:
    """Compute the public input identity without executing the grader."""
    return input_fingerprint(
        {
            "config_fingerprint": config_fingerprint(grading_input.config),
            "f3_manifest_fingerprint": grading_input.f3_manifest_fingerprint,
            "f6_manifest_fingerprint": grading_input.f6_manifest_fingerprint,
            "f7_manifest_fingerprint": grading_input.f7_manifest_fingerprint,
            "deterministic_sources": grading_input.deterministic_sources,
            "correlation_sources": grading_input.correlation_sources,
        }
    )


def item_fingerprint(value: object) -> str:
    return _domain_fingerprint(_ITEM_DOMAIN, value, bounded=False)


def result_fingerprint(value: object) -> str:
    return _domain_fingerprint(_RESULT_DOMAIN, value, bounded=False)
