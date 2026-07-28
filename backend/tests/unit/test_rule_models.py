"""F3 强类型规则配置、canonical 指纹与版本选择测试。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from pydantic import ValidationError

from app.core.rules.canonical import (
    rule_config_fingerprint,
    ruleset_fingerprint,
    validate_rule_definition,
)
from app.core.rules.models import LimitEvidence, RuleFamilyManifest, RuleKind, RuleVersion
from app.core.rules.selection import select_effective_rule_version

pytestmark = pytest.mark.unit
VERSION_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
MAPPING_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def _limit_definition(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "kind": "limit",
        "enabled": True,
        "require_direct": False,
        "exemptions": [],
        "thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "5000.00"}],
    }
    value.update(overrides)
    return value


def test_五类规则使用严格判别联合并冻结() -> None:
    definitions = [
        _limit_definition(),
        {
            "kind": "invoice_type",
            "allowances": [{"expense_type": "差旅", "allowed_invoice_types": ["电子票", "专票"]}],
        },
        {
            "kind": "timeliness",
            "policies": [{"expense_type": "差旅", "max_calendar_days": 30}],
        },
        {"kind": "invoice_title", "allowed_titles": ["费用公司"]},
        {"kind": "invoice_duplicate"},
    ]
    parsed = [validate_rule_definition(item) for item in definitions]
    assert [item.kind.value for item in parsed] == [
        "limit",
        "invoice_type",
        "timeliness",
        "invoice_title",
        "invoice_duplicate",
    ]
    with pytest.raises(ValidationError):
        parsed[0].enabled = False
    with pytest.raises(ValidationError):
        validate_rule_definition({**_limit_definition(), "legacy_operator": ">"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"thresholds": []},
        {"thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "1e3"}]},
        {"thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "0"}]},
        {
            "thresholds": [
                {"expense_type": "差旅", "currency": "CNY", "max_amount": "1"},
                {"expense_type": "差旅", "currency": "CNY", "max_amount": "2"},
            ]
        },
        {
            "exemptions": [
                {
                    "exemption_id": "duplicate-field",
                    "all": [
                        {"field": "currency", "value": "CNY"},
                        {"field": "currency", "value": "USD"},
                    ],
                }
            ]
        },
        {
            "exemptions": [
                {
                    "exemption_id": "forbidden-field",
                    "all": [{"field": "merchant", "value": "某商户"}],
                }
            ]
        },
    ],
)
def test_非法限额配置稳定拒绝(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        validate_rule_definition(_limit_definition(**overrides))


def test_配置规范化使对象集合和决策表顺序不改变指纹() -> None:
    first = validate_rule_definition(
        _limit_definition(
            exemptions=[
                {
                    "exemption_id": "b",
                    "all": [
                        {"field": "currency", "value": "cny"},
                        {"field": "expense_type", "value": " 差旅  "},
                    ],
                },
                {"exemption_id": "a", "all": [{"field": "invoice_type", "value": "电子票"}]},
            ],
            thresholds=[
                {"expense_type": "招待", "currency": "CNY", "max_amount": "1000"},
                {"expense_type": "差旅", "currency": "CNY", "max_amount": "5000.00"},
            ],
        )
    )
    second = validate_rule_definition(
        _limit_definition(
            exemptions=[
                {"all": [{"value": "电子票", "field": "invoice_type"}], "exemption_id": "a"},
                {
                    "all": [
                        {"value": "差旅", "field": "expense_type"},
                        {"value": "CNY", "field": "currency"},
                    ],
                    "exemption_id": "b",
                },
            ],
            thresholds=[
                {"max_amount": "5000", "currency": "CNY", "expense_type": "差旅"},
                {"max_amount": "1000", "expense_type": "招待", "currency": "CNY"},
            ],
        )
    )
    first_fingerprint = rule_config_fingerprint(
        rule_id="expense.limit", effective_from=date(2026, 1, 1), definition=first
    )
    second_fingerprint = rule_config_fingerprint(
        rule_id="expense.limit", effective_from=date(2026, 1, 1), definition=second
    )
    assert first_fingerprint == second_fingerprint
    assert first.thresholds[0].max_amount == "5000"
    assert first.exemptions[1].all[0].field.value == "currency"


def test_任何有效字段或生效日变化都会改变配置指纹() -> None:
    definition = validate_rule_definition(_limit_definition())
    changed = validate_rule_definition(
        _limit_definition(
            thresholds=[{"expense_type": "差旅", "currency": "CNY", "max_amount": "5001"}]
        )
    )
    base = rule_config_fingerprint(
        rule_id="expense.limit", effective_from=date(2026, 1, 1), definition=definition
    )
    assert base != rule_config_fingerprint(
        rule_id="expense.limit", effective_from=date(2026, 1, 2), definition=definition
    )
    assert base != rule_config_fingerprint(
        rule_id="expense.limit", effective_from=date(2026, 1, 1), definition=changed
    )


def test_规则集指纹对_family_和版本输入顺序稳定() -> None:
    first = [
        RuleFamilyManifest(
            rule_id="expense.limit",
            kind=RuleKind.LIMIT,
            selected_versions=(
                {"version": 2, "config_fingerprint": FINGERPRINT_B},
                {"version": 1, "config_fingerprint": FINGERPRINT_A},
            ),
        ),
        RuleFamilyManifest(
            rule_id="expense.title",
            kind=RuleKind.INVOICE_TITLE,
            selected_versions=({"version": 1, "config_fingerprint": FINGERPRINT_A},),
        ),
    ]
    assert ruleset_fingerprint(
        mapping_version_id=MAPPING_ID, rule_families=first
    ) == ruleset_fingerprint(mapping_version_id=MAPPING_ID, rule_families=list(reversed(first)))


def test_有效版本按生效日再按版本号选择且未生效显式返回() -> None:
    definition = validate_rule_definition(_limit_definition())
    versions = [
        RuleVersion(
            id=VERSION_ID,
            rule_id="expense.limit",
            version=1,
            effective_from=date(2026, 1, 1),
            config_fingerprint=FINGERPRINT_A,
            definition=definition,
        ),
        RuleVersion(
            id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            rule_id="expense.limit",
            version=2,
            effective_from=date(2026, 1, 1),
            config_fingerprint=FINGERPRINT_B,
            definition=definition,
        ),
    ]
    selected = select_effective_rule_version(
        rule_id="expense.limit",
        rule_kind=RuleKind.LIMIT,
        candidates=versions,
        expense_date=date(2026, 1, 1),
    )
    assert selected.selected is not None
    assert selected.selected.version == 2
    unavailable = select_effective_rule_version(
        rule_id="expense.limit",
        rule_kind=RuleKind.LIMIT,
        candidates=versions,
        expense_date=date(2025, 12, 31),
    )
    assert unavailable.reason_code == "RULE_NOT_EFFECTIVE"


def test_超长值与空集合不静默截断() -> None:
    with pytest.raises(ValidationError):
        validate_rule_definition({"kind": "invoice_title", "allowed_titles": []})
    with pytest.raises(ValidationError):
        validate_rule_definition({"kind": "invoice_title", "allowed_titles": ["字" * 513]})


def test_完整配置_canonical_超过_256_kib_被拒绝() -> None:
    values = [f"{index:03d}" + "字" * 509 for index in range(500)]
    with pytest.raises(ValueError, match="256 KiB"):
        validate_rule_definition(
            {
                "kind": "invoice_type",
                "allowances": [{"expense_type": "差旅", "allowed_invoice_types": values}],
            }
        )


def test_金额前导零与尾零被规范化() -> None:
    definition = validate_rule_definition(
        _limit_definition(
            thresholds=[{"expense_type": "差旅", "currency": "cny", "max_amount": "005000.0100"}]
        )
    )
    assert definition.thresholds[0].currency == "CNY"
    assert definition.thresholds[0].max_amount == "5000.01"


def test_evidence_拒绝_kind_reason_错配和不完整命中() -> None:
    base: dict[str, object] = {
        "outcome": "flagged",
        "rule_kind": "limit",
        "required_fields": ["amount", "expense_type", "currency"],
        "provenance": {},
        "amount": "10",
        "expense_type": "差旅",
        "currency": "CNY",
        "max_amount": "5",
    }
    with pytest.raises(ValidationError):
        LimitEvidence.model_validate({**base, "reason_code": "invoice_duplicate"})
    with pytest.raises(ValidationError):
        LimitEvidence.model_validate({**base, "reason_code": "limit_exceeded", "max_amount": None})
