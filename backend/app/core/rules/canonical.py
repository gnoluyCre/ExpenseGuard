"""规则配置与规则集的稳定 canonical JSON 和 SHA-256 指纹。"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel

from app.core.rules.models import (
    MAX_CANONICAL_BYTES,
    RULE_DEFINITION_ADAPTER,
    RuleDefinition,
    RuleFamilyManifest,
)

SELECTION_ALGORITHM = "effective-on-expense-date-v1"


def _canonical_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, set | frozenset):
        return sorted((_canonical_value(item) for item in value), key=_sort_token)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _sort_token(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_rule_definition(value: object) -> RuleDefinition:
    definition = RULE_DEFINITION_ADAPTER.validate_python(value)
    if len(canonical_json(definition).encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ValueError("RULE_CONFIG_INVALID: canonical JSON 超过 256 KiB")
    return definition


def rule_config_fingerprint(
    *, rule_id: str, effective_from: date, definition: RuleDefinition
) -> str:
    if not rule_id.strip() or len(rule_id) > 128:
        raise ValueError("RULE_CONFIG_INVALID: rule_id 非法")
    payload = {
        "rule_id": rule_id,
        "effective_from": effective_from,
        "definition": definition,
    }
    if len(canonical_json(payload).encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ValueError("RULE_CONFIG_INVALID: canonical JSON 超过 256 KiB")
    return sha256_fingerprint(payload)


def value_set_fingerprint(values: Sequence[str]) -> str:
    return sha256_fingerprint(sorted(values))


def ruleset_fingerprint(
    *, mapping_version_id: uuid.UUID, rule_families: Sequence[RuleFamilyManifest]
) -> str:
    families = []
    for family in sorted(rule_families, key=lambda item: (item.kind.value, item.rule_id)):
        versions = sorted(
            family.selected_versions,
            key=lambda item: (item.version, item.config_fingerprint),
        )
        families.append(
            {
                "rule_id": family.rule_id,
                "kind": family.kind,
                "selected_versions": [
                    {
                        "version": selected.version,
                        "config_fingerprint": selected.config_fingerprint,
                    }
                    for selected in versions
                ],
            }
        )
    return sha256_fingerprint(
        {
            "schema_version": 1,
            "selection_algorithm": SELECTION_ALGORITHM,
            "mapping_version_id": mapping_version_id,
            "rule_families": families,
        }
    )
