"""F3 五类纯 evaluator、evidence、reasoning 与 verdict 测试。"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from app.core.parsing.models import NormalizedExpenseRecord, UnifiedField
from app.core.rules.canonical import validate_rule_definition
from app.core.rules.evaluator import (
    aggregate_verdict,
    evaluate_rule,
    evaluate_rule_selection,
    select_duplicate_match,
)
from app.core.rules.models import (
    DuplicateMatch,
    InvoiceDuplicateRuleDefinition,
    InvoiceOccurrence,
    InvoiceTitleRuleDefinition,
    InvoiceTypeRuleDefinition,
    LimitRuleDefinition,
    RowVerdict,
    RuleDefinition,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    RuleSelection,
    TimelinessRuleDefinition,
)
from app.core.rules.reasoning import render_reasoning

pytestmark = pytest.mark.unit
MAPPING_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _record(
    *,
    inferred: Iterable[UnifiedField] = (),
    **overrides: object,
) -> NormalizedExpenseRecord:
    values: dict[str, object] = {
        "schema_version": 1,
        "mapping_version_id": MAPPING_ID,
        "amount": "5000",
        "expense_date": "2024-02-29",
        "employee": None,
        "expense_type": "差旅",
        "invoice_type": "电子票",
        "invoice_no": "000ABC",
        "merchant": None,
        "invoice_title": "费用公司",
        "submission_date": "2024-03-30",
        "location": None,
        "currency": "CNY",
        "description": None,
    }
    values.update(overrides)
    inferred_set = set(inferred)
    provenance: dict[str, object] = {}
    for field in UnifiedField:
        if values[field.value] is None:
            continue
        if field in inferred_set:
            provenance[field.value] = {
                "mode": "inferred",
                "source_columns": ["描述"],
                "inference_rule_id": f"infer-{field.value}",
            }
        else:
            provenance[field.value] = {
                "mode": "mapped",
                "source_columns": [field.value],
                "inference_rule_id": None,
            }
    values["field_provenance"] = provenance
    return NormalizedExpenseRecord.model_validate(values)


def _definition(kind: str, **overrides: object) -> RuleDefinition:
    definitions: dict[str, dict[str, object]] = {
        "limit": {
            "kind": "limit",
            "thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "5000"}],
        },
        "invoice_type": {
            "kind": "invoice_type",
            "allowances": [{"expense_type": "差旅", "allowed_invoice_types": ["电子票", "专票"]}],
        },
        "timeliness": {
            "kind": "timeliness",
            "policies": [{"expense_type": "差旅", "max_calendar_days": 30}],
        },
        "invoice_title": {"kind": "invoice_title", "allowed_titles": ["费用公司"]},
        "invoice_duplicate": {"kind": "invoice_duplicate"},
    }
    definitions[kind].update(overrides)
    return validate_rule_definition(definitions[kind])


@pytest.mark.parametrize(
    ("definition", "record", "expected_reason"),
    [
        (_definition("limit"), _record(amount="5000.0000"), None),
        (_definition("limit"), _record(amount="5000.0001"), "limit_exceeded"),
        (_definition("invoice_type"), _record(invoice_type="电子票"), None),
        (
            _definition("invoice_type"),
            _record(invoice_type="电子票（专用）"),
            "invoice_type_not_allowed",
        ),
        (_definition("timeliness"), _record(), None),
        (_definition("timeliness"), _record(submission_date="2024-03-31"), "claim_submitted_late"),
        (_definition("invoice_title"), _record(invoice_title="费用公司"), None),
        (
            _definition("invoice_title"),
            _record(invoice_title="费用公司上海分部"),
            "invoice_title_not_allowed",
        ),
    ],
)
def test_四类规则通过命中与精确边界(
    definition: object, record: NormalizedExpenseRecord, expected_reason: str | None
) -> None:
    result = evaluate_rule(definition, record)
    assert result.reason_code == expected_reason
    assert result.outcome is (
        RuleOutcome.PASSED if expected_reason is None else RuleOutcome.FLAGGED
    )


@pytest.mark.parametrize(
    ("kind", "field", "expected_reason"),
    [
        ("limit", "currency", "MISSING_REQUIRED_FIELD"),
        ("invoice_type", "invoice_type", "MISSING_REQUIRED_FIELD"),
        ("timeliness", "submission_date", "MISSING_REQUIRED_FIELD"),
        ("invoice_title", "invoice_title", "INVOICE_TITLE_MISSING"),
        ("invoice_duplicate", "invoice_no", "INVOICE_NO_MISSING"),
    ],
)
def test_五类缺失依赖显式不可判定(kind: str, field: str, expected_reason: str) -> None:
    result = evaluate_rule(_definition(kind), _record(**{field: None}))
    assert result.outcome is RuleOutcome.UNAVAILABLE
    assert result.reason_code == expected_reason
    assert result.evidence is not None


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("limit", UnifiedField.CURRENCY),
        ("invoice_type", UnifiedField.INVOICE_TYPE),
        ("timeliness", UnifiedField.SUBMISSION_DATE),
        ("invoice_title", UnifiedField.INVOICE_TITLE),
        ("invoice_duplicate", UnifiedField.INVOICE_NO),
    ],
)
def test_inferred_默认可求值但_require_direct_转人工(kind: str, field: UnifiedField) -> None:
    allowed = evaluate_rule(_definition(kind), _record(inferred=[field]))
    assert allowed.outcome is RuleOutcome.PASSED
    blocked = evaluate_rule(
        _definition(kind, require_direct=True),
        _record(inferred=[field]),
    )
    assert blocked.reason_code == "INFERRED_FIELD_NOT_ALLOWED"
    assert blocked.evidence is not None
    assert blocked.evidence.provenance[field].inference_rule_id == f"infer-{field.value}"


def test_例外先于主规则依赖且创建_exempted_evidence() -> None:
    definition = _definition(
        "invoice_type",
        exemptions=[
            {
                "exemption_id": "travel-cny",
                "all": [
                    {"field": "expense_type", "value": "差旅"},
                    {"field": "currency", "value": "CNY"},
                ],
            }
        ],
    )
    result = evaluate_rule(definition, _record(invoice_type=None))
    assert result.outcome is RuleOutcome.EXEMPTED
    assert result.reason_code == "EXEMPTION_MATCHED"
    assert result.evidence is not None
    assert result.evidence.exemption_id == "travel-cny"
    assert "差旅" not in render_reasoning(result.evidence)


def test_例外匹配但来源推断且要求直接时不可豁免() -> None:
    definition = _definition(
        "invoice_type",
        require_direct=True,
        exemptions=[
            {
                "exemption_id": "travel",
                "all": [{"field": "expense_type", "value": "差旅"}],
            }
        ],
    )
    result = evaluate_rule(
        definition, _record(invoice_type=None, inferred=[UnifiedField.EXPENSE_TYPE])
    )
    assert result.reason_code == "INFERRED_FIELD_NOT_ALLOWED"


@pytest.mark.parametrize(
    "kind", ["limit", "invoice_type", "timeliness", "invoice_title", "invoice_duplicate"]
)
def test_disabled_五类规则均显式转人工(kind: str) -> None:
    result = evaluate_rule(_definition(kind, enabled=False), _record())
    assert result.reason_code == "RULE_DISABLED"
    assert result.outcome is RuleOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    ("definition", "record", "reason"),
    [
        (_definition("limit"), _record(expense_type="培训"), "LIMIT_THRESHOLD_NOT_CONFIGURED"),
        (
            _definition("invoice_type"),
            _record(expense_type="培训"),
            "INVOICE_TYPE_POLICY_NOT_CONFIGURED",
        ),
        (
            _definition("timeliness"),
            _record(expense_type="培训"),
            "TIMELINESS_POLICY_NOT_CONFIGURED",
        ),
        (
            _definition("timeliness"),
            _record(expense_date="2024-03-01", submission_date="2024-02-29"),
            "SUBMISSION_BEFORE_EXPENSE_DATE",
        ),
    ],
)
def test_决策表无匹配与负日差不可判定(
    definition: object, record: NormalizedExpenseRecord, reason: str
) -> None:
    result = evaluate_rule(definition, record)
    assert result.outcome is RuleOutcome.UNAVAILABLE
    assert result.reason_code == reason


def test_发票号查重只消费已确定的精确候选并保留前导零() -> None:
    definition = _definition("invoice_duplicate")
    assert isinstance(definition, InvoiceDuplicateRuleDefinition)
    assert evaluate_rule(definition, _record(), duplicate_match=None).outcome is RuleOutcome.PASSED
    match = DuplicateMatch(
        file_version_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        root_file_version_id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
        row_no=7,
    )
    result = evaluate_rule(definition, _record(), duplicate_match=match)
    assert result.reason_code == "invoice_duplicate"
    assert result.evidence is not None
    assert result.evidence.invoice_no == "000ABC"
    assert "第 7 行" in render_reasoning(result.evidence)


def test_发票号首条选择对输入顺序稳定并排除当前_lineage_历史候选() -> None:
    root_a = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    root_b = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    current = InvoiceOccurrence(
        file_version_id=root_b,
        root_file_version_id=root_b,
        root_uploaded_at=datetime(2026, 2, 1, tzinfo=UTC),
        row_no=2,
        invoice_no="000ABC",
    )
    same_batch_first = current.model_copy(update={"row_no": 1})
    excluded_revision = InvoiceOccurrence(
        file_version_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
        root_file_version_id=root_b,
        root_uploaded_at=datetime(2026, 2, 1, tzinfo=UTC),
        row_no=9,
        invoice_no="000ABC",
    )
    historical_first = InvoiceOccurrence(
        file_version_id=root_a,
        root_file_version_id=root_a,
        root_uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
        row_no=8,
        invoice_no="000ABC",
    )
    first = select_duplicate_match(
        current=current,
        current_batch_occurrences=[current, same_batch_first],
        historical_occurrences=[excluded_revision, historical_first],
    )
    reversed_input = select_duplicate_match(
        current=current,
        current_batch_occurrences=[same_batch_first, current],
        historical_occurrences=[historical_first, excluded_revision],
    )
    assert first == reversed_input
    assert first is not None
    assert first.root_file_version_id == root_a
    assert first.row_no == 8


def test_唯一首条不命中且规范化结果不同不参与() -> None:
    current = InvoiceOccurrence(
        file_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        root_file_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        root_uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
        row_no=1,
        invoice_no="000ABC",
    )
    different = current.model_copy(update={"invoice_no": "ABC"})
    assert (
        select_duplicate_match(
            current=current,
            current_batch_occurrences=[different],
            historical_occurrences=[],
        )
        is None
    )


def test_查重候选冲突时拒绝猜测() -> None:
    current = InvoiceOccurrence(
        file_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        root_file_version_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        root_uploaded_at=datetime(2026, 1, 1, tzinfo=UTC),
        row_no=1,
        invoice_no="000ABC",
    )
    wrong_batch = current.model_copy(
        update={"root_file_version_id": uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")}
    )
    with pytest.raises(ValueError, match="同一 file revision"):
        select_duplicate_match(
            current=current,
            current_batch_occurrences=[wrong_batch],
            historical_occurrences=[],
        )


def test_未生效选择机械生成对应_kind_evidence() -> None:
    selection = RuleSelection(
        rule_id="expense.limit",
        rule_kind=RuleKind.LIMIT,
        reason_code="RULE_NOT_EFFECTIVE",
    )
    result = evaluate_rule_selection(selection, _record())
    assert result.outcome is RuleOutcome.UNAVAILABLE
    assert result.reason_code == "RULE_NOT_EFFECTIVE"
    assert result.evidence is not None
    assert result.evidence.rule_kind is RuleKind.LIMIT


def test_reasoning_由_evidence_稳定重建且不复制大型允许集合() -> None:
    definition = _definition("invoice_type")
    assert isinstance(definition, InvoiceTypeRuleDefinition)
    result = evaluate_rule(definition, _record(invoice_type="不允许票种"))
    assert result.evidence is not None
    assert render_reasoning(result.evidence) == render_reasoning(result.evidence)
    dumped = result.evidence.model_dump(mode="json")
    assert "allowed_invoice_types" not in dumped
    assert dumped["allowed_invoice_types_fingerprint"] is not None


def test_verdict_聚合优先级且_exempted_不提升() -> None:
    passed = RuleEvaluation(outcome=RuleOutcome.PASSED)
    exempted = evaluate_rule(
        _definition(
            "invoice_title",
            exemptions=[
                {
                    "exemption_id": "travel",
                    "all": [{"field": "expense_type", "value": "差旅"}],
                }
            ],
        ),
        _record(),
    )
    unavailable = evaluate_rule(_definition("invoice_title"), _record(invoice_title=None))
    flagged = evaluate_rule(_definition("limit"), _record(amount="5000.01"))
    assert aggregate_verdict([]) is RowVerdict.PASSED
    assert aggregate_verdict([passed, exempted]) is RowVerdict.PASSED
    assert aggregate_verdict([passed, unavailable]) is RowVerdict.MANUAL_REVIEW
    assert aggregate_verdict([unavailable, flagged]) is RowVerdict.FLAGGED


def test_evidence_模型与具体定义类型可识别() -> None:
    definitions = [
        _definition("limit"),
        _definition("invoice_type"),
        _definition("timeliness"),
        _definition("invoice_title"),
    ]
    expected = [
        LimitRuleDefinition,
        InvoiceTypeRuleDefinition,
        TimelinessRuleDefinition,
        InvoiceTitleRuleDefinition,
    ]
    assert [type(item) for item in definitions] == expected
