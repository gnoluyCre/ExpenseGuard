"""五类规则的无 I/O、无时钟确定性 evaluator。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from typing import cast

from app.core.parsing.models import (
    FieldProvenance,
    NormalizedExpenseRecord,
    ProvenanceMode,
    UnifiedField,
)
from app.core.rules.canonical import value_set_fingerprint
from app.core.rules.models import (
    DuplicateMatch,
    EvidenceBase,
    ExemptionGroup,
    InvoiceDuplicateEvidence,
    InvoiceDuplicateRuleDefinition,
    InvoiceOccurrence,
    InvoiceTitleEvidence,
    InvoiceTitleRuleDefinition,
    InvoiceTypeEvidence,
    InvoiceTypeRuleDefinition,
    LimitEvidence,
    LimitRuleDefinition,
    ReasonCode,
    RowVerdict,
    RuleDefinition,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    RuleSelection,
    TimelinessEvidence,
    TimelinessRuleDefinition,
)

EvidenceFactory = Callable[..., EvidenceBase]

_DEPENDENCIES: dict[RuleKind, tuple[UnifiedField, ...]] = {
    RuleKind.LIMIT: (
        UnifiedField.AMOUNT,
        UnifiedField.EXPENSE_TYPE,
        UnifiedField.CURRENCY,
    ),
    RuleKind.INVOICE_TYPE: (UnifiedField.EXPENSE_TYPE, UnifiedField.INVOICE_TYPE),
    RuleKind.TIMELINESS: (
        UnifiedField.EXPENSE_TYPE,
        UnifiedField.EXPENSE_DATE,
        UnifiedField.SUBMISSION_DATE,
    ),
    RuleKind.INVOICE_TITLE: (UnifiedField.INVOICE_TITLE,),
    RuleKind.INVOICE_DUPLICATE: (UnifiedField.INVOICE_NO,),
}


def _record_values(
    record: NormalizedExpenseRecord, fields: Sequence[UnifiedField]
) -> dict[str, object]:
    return {field.value: getattr(record, field.value) for field in fields}


def _provenance(
    record: NormalizedExpenseRecord, fields: Sequence[UnifiedField]
) -> dict[UnifiedField, FieldProvenance]:
    return {
        field: record.field_provenance[field]
        for field in fields
        if field in record.field_provenance
    }


def _evaluation(evidence: EvidenceBase) -> RuleEvaluation:
    return RuleEvaluation(
        outcome=evidence.outcome,
        reason_code=evidence.reason_code,
        evidence=evidence,
    )


def _base_evidence_data(
    *,
    definition: RuleDefinition,
    record: NormalizedExpenseRecord,
    outcome: RuleOutcome,
    reason_code: ReasonCode,
    required_fields: Sequence[UnifiedField] | None = None,
    exemption_id: str | None = None,
) -> dict[str, object]:
    fields = tuple(required_fields or _DEPENDENCIES[definition.kind])
    return {
        "outcome": outcome,
        "rule_kind": definition.kind,
        "reason_code": reason_code,
        "required_fields": fields,
        "provenance": _provenance(record, fields),
        "exemption_id": exemption_id,
    }


def _unavailable_or_exempted(
    definition: RuleDefinition,
    record: NormalizedExpenseRecord,
    evidence_factory: EvidenceFactory,
) -> RuleEvaluation | None:
    if not definition.enabled:
        return _evaluation(
            evidence_factory(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code="RULE_DISABLED",
                ),
                **_record_values(record, _DEPENDENCIES[definition.kind]),
            )
        )

    for group in definition.exemptions:
        matched = _exemption_matches(group, record)
        if not matched:
            continue
        fields = tuple(UnifiedField(condition.field.value) for condition in group.all)
        if definition.require_direct and any(
            record.field_provenance[field].mode is ProvenanceMode.INFERRED for field in fields
        ):
            return _evaluation(
                evidence_factory(
                    **_base_evidence_data(
                        definition=definition,
                        record=record,
                        outcome=RuleOutcome.UNAVAILABLE,
                        reason_code="INFERRED_FIELD_NOT_ALLOWED",
                        required_fields=fields,
                    ),
                    **_record_values(record, _DEPENDENCIES[definition.kind]),
                )
            )
        return _evaluation(
            evidence_factory(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.EXEMPTED,
                    reason_code="EXEMPTION_MATCHED",
                    required_fields=fields,
                    exemption_id=group.exemption_id,
                ),
                **_record_values(record, _DEPENDENCIES[definition.kind]),
            )
        )

    dependencies = _DEPENDENCIES[definition.kind]
    if any(
        getattr(record, field.value) is None or field not in record.field_provenance
        for field in dependencies
    ):
        return _evaluation(
            evidence_factory(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code=_missing_reason(definition.kind),
                ),
                **_record_values(record, dependencies),
            )
        )
    if definition.require_direct and any(
        record.field_provenance[field].mode is ProvenanceMode.INFERRED for field in dependencies
    ):
        return _evaluation(
            evidence_factory(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code="INFERRED_FIELD_NOT_ALLOWED",
                ),
                **_record_values(record, dependencies),
            )
        )
    return None


def _missing_reason(kind: RuleKind) -> ReasonCode:
    if kind is RuleKind.INVOICE_TITLE:
        return "INVOICE_TITLE_MISSING"
    if kind is RuleKind.INVOICE_DUPLICATE:
        return "INVOICE_NO_MISSING"
    return "MISSING_REQUIRED_FIELD"


def _exemption_matches(group: ExemptionGroup, record: NormalizedExpenseRecord) -> bool:
    return all(
        getattr(record, condition.field.value) is not None
        and getattr(record, condition.field.value) == condition.value
        for condition in group.all
    )


def _passed() -> RuleEvaluation:
    return RuleEvaluation(outcome=RuleOutcome.PASSED)


def evaluate_limit(
    definition: LimitRuleDefinition, record: NormalizedExpenseRecord
) -> RuleEvaluation:
    early = _unavailable_or_exempted(definition, record, LimitEvidence)
    if early is not None:
        return early
    threshold = next(
        (
            item
            for item in definition.thresholds
            if item.expense_type == record.expense_type and item.currency == record.currency
        ),
        None,
    )
    if threshold is None:
        return _evaluation(
            LimitEvidence(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code="LIMIT_THRESHOLD_NOT_CONFIGURED",
                ),
                amount=record.amount,
                expense_type=record.expense_type,
                currency=record.currency,
            )
        )
    if Decimal(record.amount) <= Decimal(threshold.max_amount):
        return _passed()
    return _evaluation(
        LimitEvidence(
            **_base_evidence_data(
                definition=definition,
                record=record,
                outcome=RuleOutcome.FLAGGED,
                reason_code="limit_exceeded",
            ),
            amount=record.amount,
            expense_type=record.expense_type,
            currency=record.currency,
            max_amount=threshold.max_amount,
        )
    )


def evaluate_invoice_type(
    definition: InvoiceTypeRuleDefinition, record: NormalizedExpenseRecord
) -> RuleEvaluation:
    early = _unavailable_or_exempted(definition, record, InvoiceTypeEvidence)
    if early is not None:
        return early
    allowance = next(
        (item for item in definition.allowances if item.expense_type == record.expense_type), None
    )
    if allowance is None:
        return _evaluation(
            InvoiceTypeEvidence(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code="INVOICE_TYPE_POLICY_NOT_CONFIGURED",
                ),
                expense_type=record.expense_type,
                invoice_type=record.invoice_type,
            )
        )
    if record.invoice_type in allowance.allowed_invoice_types:
        return _passed()
    return _evaluation(
        InvoiceTypeEvidence(
            **_base_evidence_data(
                definition=definition,
                record=record,
                outcome=RuleOutcome.FLAGGED,
                reason_code="invoice_type_not_allowed",
            ),
            expense_type=record.expense_type,
            invoice_type=record.invoice_type,
            allowed_invoice_types_fingerprint=value_set_fingerprint(
                allowance.allowed_invoice_types
            ),
        )
    )


def evaluate_timeliness(
    definition: TimelinessRuleDefinition, record: NormalizedExpenseRecord
) -> RuleEvaluation:
    early = _unavailable_or_exempted(definition, record, TimelinessEvidence)
    if early is not None:
        return early
    policy = next(
        (item for item in definition.policies if item.expense_type == record.expense_type), None
    )
    if policy is None:
        return _evaluation(
            TimelinessEvidence(
                **_base_evidence_data(
                    definition=definition,
                    record=record,
                    outcome=RuleOutcome.UNAVAILABLE,
                    reason_code="TIMELINESS_POLICY_NOT_CONFIGURED",
                ),
                expense_type=record.expense_type,
                expense_date=record.expense_date,
                submission_date=record.submission_date,
            )
        )
    actual_days = (
        date.fromisoformat(cast(str, record.submission_date))
        - date.fromisoformat(record.expense_date)
    ).days
    if actual_days < 0:
        outcome = RuleOutcome.UNAVAILABLE
        reason: ReasonCode = "SUBMISSION_BEFORE_EXPENSE_DATE"
    elif actual_days > policy.max_calendar_days:
        outcome = RuleOutcome.FLAGGED
        reason = "claim_submitted_late"
    else:
        return _passed()
    return _evaluation(
        TimelinessEvidence(
            **_base_evidence_data(
                definition=definition,
                record=record,
                outcome=outcome,
                reason_code=reason,
            ),
            expense_type=record.expense_type,
            expense_date=record.expense_date,
            submission_date=record.submission_date,
            actual_calendar_days=actual_days,
            max_calendar_days=policy.max_calendar_days,
        )
    )


def evaluate_invoice_title(
    definition: InvoiceTitleRuleDefinition, record: NormalizedExpenseRecord
) -> RuleEvaluation:
    early = _unavailable_or_exempted(definition, record, InvoiceTitleEvidence)
    if early is not None:
        return early
    if record.invoice_title in definition.allowed_titles:
        return _passed()
    return _evaluation(
        InvoiceTitleEvidence(
            **_base_evidence_data(
                definition=definition,
                record=record,
                outcome=RuleOutcome.FLAGGED,
                reason_code="invoice_title_not_allowed",
            ),
            invoice_title=record.invoice_title,
            allowed_titles_fingerprint=value_set_fingerprint(definition.allowed_titles),
        )
    )


def evaluate_invoice_duplicate(
    definition: InvoiceDuplicateRuleDefinition,
    record: NormalizedExpenseRecord,
    *,
    duplicate_match: DuplicateMatch | None,
) -> RuleEvaluation:
    early = _unavailable_or_exempted(definition, record, InvoiceDuplicateEvidence)
    if early is not None:
        return early
    if duplicate_match is None:
        return _passed()
    return _evaluation(
        InvoiceDuplicateEvidence(
            **_base_evidence_data(
                definition=definition,
                record=record,
                outcome=RuleOutcome.FLAGGED,
                reason_code="invoice_duplicate",
            ),
            invoice_no=record.invoice_no,
            duplicate_of_file_version_id=duplicate_match.file_version_id,
            duplicate_of_root_file_version_id=duplicate_match.root_file_version_id,
            duplicate_of_row_no=duplicate_match.row_no,
        )
    )


def evaluate_rule(
    definition: RuleDefinition,
    record: NormalizedExpenseRecord,
    *,
    duplicate_match: DuplicateMatch | None = None,
) -> RuleEvaluation:
    if isinstance(definition, LimitRuleDefinition):
        return evaluate_limit(definition, record)
    if isinstance(definition, InvoiceTypeRuleDefinition):
        return evaluate_invoice_type(definition, record)
    if isinstance(definition, TimelinessRuleDefinition):
        return evaluate_timeliness(definition, record)
    if isinstance(definition, InvoiceTitleRuleDefinition):
        return evaluate_invoice_title(definition, record)
    return evaluate_invoice_duplicate(definition, record, duplicate_match=duplicate_match)


def evaluate_rule_selection(
    selection: RuleSelection,
    record: NormalizedExpenseRecord,
    *,
    duplicate_match: DuplicateMatch | None = None,
) -> RuleEvaluation:
    """把有效版本选择（含未生效）机械转换为统一求值结果。"""
    if selection.selected is not None:
        return evaluate_rule(
            selection.selected.definition,
            record,
            duplicate_match=duplicate_match,
        )
    evidence_types: dict[RuleKind, EvidenceFactory] = {
        RuleKind.LIMIT: LimitEvidence,
        RuleKind.INVOICE_TYPE: InvoiceTypeEvidence,
        RuleKind.TIMELINESS: TimelinessEvidence,
        RuleKind.INVOICE_TITLE: InvoiceTitleEvidence,
        RuleKind.INVOICE_DUPLICATE: InvoiceDuplicateEvidence,
    }
    evidence = evidence_types[selection.rule_kind](
        outcome=RuleOutcome.UNAVAILABLE,
        rule_kind=selection.rule_kind,
        reason_code="RULE_NOT_EFFECTIVE",
        required_fields=_DEPENDENCIES[selection.rule_kind],
        provenance=_provenance(record, _DEPENDENCIES[selection.rule_kind]),
        **_record_values(record, _DEPENDENCIES[selection.rule_kind]),
    )
    return _evaluation(evidence)


def select_duplicate_match(
    *,
    current: InvoiceOccurrence,
    current_batch_occurrences: Sequence[InvoiceOccurrence],
    historical_occurrences: Sequence[InvoiceOccurrence],
) -> DuplicateMatch | None:
    """在编排层已冻结的候选中按 root 证据顺序确定唯一首条。"""
    if any(
        item.root_file_version_id != current.root_file_version_id
        or item.file_version_id != current.file_version_id
        for item in current_batch_occurrences
    ):
        raise ValueError("当前批次 occurrence 必须属于同一 file revision 和 root lineage")
    candidates = [
        item for item in current_batch_occurrences if item.invoice_no == current.invoice_no
    ]
    candidates.extend(
        item
        for item in historical_occurrences
        if item.invoice_no == current.invoice_no
        and item.root_file_version_id != current.root_file_version_id
    )
    by_identity: dict[tuple[uuid.UUID, int], InvoiceOccurrence] = {}
    for item in candidates:
        identity = (item.root_file_version_id, item.row_no)
        existing = by_identity.get(identity)
        if existing is not None and existing != item:
            raise ValueError("同一物理证据 occurrence 不得出现冲突版本")
        if existing is None:
            by_identity[identity] = item
    by_identity[(current.root_file_version_id, current.row_no)] = current
    first = min(
        by_identity.values(),
        key=lambda item: (
            item.root_uploaded_at,
            str(item.root_file_version_id),
            item.row_no,
        ),
    )
    if (first.root_file_version_id, first.row_no) == (
        current.root_file_version_id,
        current.row_no,
    ):
        return None
    return DuplicateMatch(
        file_version_id=first.file_version_id,
        root_file_version_id=first.root_file_version_id,
        row_no=first.row_no,
    )


def aggregate_verdict(evaluations: Sequence[RuleEvaluation]) -> RowVerdict:
    if any(result.outcome is RuleOutcome.FLAGGED for result in evaluations):
        return RowVerdict.FLAGGED
    if any(result.outcome is RuleOutcome.UNAVAILABLE for result in evaluations):
        return RowVerdict.MANUAL_REVIEW
    return RowVerdict.PASSED
