"""Pure deterministic F8 grader."""

from __future__ import annotations

from app.core.detection.models import CapabilityStatus
from app.core.grading.canonical import (
    grading_input_fingerprint,
    item_fingerprint,
    result_fingerprint,
)
from app.core.grading.matrix import calculate_cell
from app.core.grading.models import (
    DISPOSITION_ORDER,
    REASON_CODE_ORDER,
    SOURCE_KIND_ORDER,
    CorrelationEvidenceSnapshot,
    CorrelationGradingSource,
    DeterministicEvidenceSnapshot,
    DeterministicGradingSource,
    Disposition,
    GradingConfigV1,
    GradingCoreResult,
    GradingInput,
    GradingItemResult,
    GradingReasonCode,
    InvestigationGradingOutcome,
    InvestigationNotRunSource,
    SeverityLevel,
    SourceKind,
)
from app.core.rules.models import RuleOutcome

_DISPOSITION_RANK = {value: rank for rank, value in enumerate(DISPOSITION_ORDER)}
_SOURCE_RANK = {value: rank for rank, value in enumerate(SOURCE_KIND_ORDER)}
_REASON_RANK = {value: rank for rank, value in enumerate(REASON_CODE_ORDER)}

_OUTCOME_REASON: dict[InvestigationGradingOutcome, GradingReasonCode] = {
    InvestigationGradingOutcome.SUFFICIENT: GradingReasonCode.CONFIDENCE_F7_SUFFICIENT,
    InvestigationGradingOutcome.INSUFFICIENT: GradingReasonCode.CONFIDENCE_F7_INSUFFICIENT,
    InvestigationGradingOutcome.UNAVAILABLE: GradingReasonCode.CONFIDENCE_F7_UNAVAILABLE,
    InvestigationGradingOutcome.MAX_STEPS: GradingReasonCode.CONFIDENCE_F7_MAX_STEPS,
    InvestigationGradingOutcome.FAILED: GradingReasonCode.CONFIDENCE_F7_FAILED,
    InvestigationGradingOutcome.NOT_RUN: GradingReasonCode.CONFIDENCE_F7_NOT_RUN,
}
_CAPABILITY_REASON: dict[CapabilityStatus, GradingReasonCode] = {
    CapabilityStatus.ENABLED: GradingReasonCode.CAPABILITY_ENABLED,
    CapabilityStatus.DEGRADED: GradingReasonCode.CAPABILITY_DEGRADED,
    CapabilityStatus.UNAVAILABLE: GradingReasonCode.CAPABILITY_UNAVAILABLE,
}
_FINAL_REASON: dict[Disposition, GradingReasonCode] = {
    Disposition.HIGH_ATTENTION: GradingReasonCode.FINAL_HIGH_ATTENTION,
    Disposition.MANUAL_ATTENTION: GradingReasonCode.FINAL_MANUAL_ATTENTION,
    Disposition.CLEARED: GradingReasonCode.FINAL_CLEARED,
}


def _ordered_reasons(reasons: list[GradingReasonCode]) -> tuple[GradingReasonCode, ...]:
    return tuple(sorted(set(reasons), key=_REASON_RANK.__getitem__))


def _grade_deterministic(
    config: GradingConfigV1,
    source: DeterministicGradingSource,
    grading_input_fingerprint: str,
) -> GradingItemResult:
    impact = config.impact_by_rule_kind.for_kind(source.rule_kind)
    if source.outcome is RuleOutcome.FLAGGED:
        confidence = SeverityLevel.LEVEL_3
        confidence_reason = GradingReasonCode.CONFIDENCE_F3_FLAGGED
    else:
        confidence = SeverityLevel.LEVEL_0
        confidence_reason = GradingReasonCode.CONFIDENCE_F3_UNAVAILABLE

    cell = calculate_cell(config, impact, confidence)
    disposition = cell.disposition
    reasons = [
        GradingReasonCode.IMPACT_RULE_MAPPING,
        confidence_reason,
        GradingReasonCode.COST_MATRIX_SELECTED,
        *cell.override_reason_codes,
    ]
    if (
        source.outcome is RuleOutcome.UNAVAILABLE
        and disposition is not Disposition.MANUAL_ATTENTION
    ):
        disposition = Disposition.MANUAL_ATTENTION
        reasons.append(GradingReasonCode.OVERRIDE_F3_UNAVAILABLE)
    reasons.append(_FINAL_REASON[disposition])

    evidence = DeterministicEvidenceSnapshot(
        source_kind=SourceKind.DETERMINISTIC,
        rule_kind=source.rule_kind,
        detector=None,
        source_outcome=source.outcome,
        capability_status=None,
        mapped_impact=impact,
        confidence_before_cap=confidence,
        confidence_after_cap=confidence,
        losses=cell.losses,
        matrix_disposition=cell.disposition,
    )
    payload = {
        "input_fingerprint": grading_input_fingerprint,
        "source": source,
        "severity_impact": impact,
        "severity_confidence": confidence,
        "matrix_disposition": cell.disposition,
        "disposition": disposition,
        "reason_codes": _ordered_reasons(reasons),
        "evidence_snapshot": evidence,
    }
    return GradingItemResult(
        source_kind=source.kind,
        source_id=source.source_id,
        first_row_no=source.first_row_no,
        severity_impact=impact,
        severity_confidence=confidence,
        matrix_disposition=cell.disposition,
        disposition=disposition,
        reason_codes=payload["reason_codes"],
        evidence_snapshot=evidence,
        item_fingerprint=item_fingerprint(payload),
    )


def _investigation_outcome(source: CorrelationGradingSource) -> InvestigationGradingOutcome:
    if isinstance(source.investigation, InvestigationNotRunSource):
        return InvestigationGradingOutcome.NOT_RUN
    return InvestigationGradingOutcome(source.investigation.outcome)


def _grade_correlation(
    config: GradingConfigV1,
    source: CorrelationGradingSource,
    grading_input_fingerprint: str,
) -> GradingItemResult:
    impact = config.impact_by_detector.for_kind(source.detector)
    outcome = _investigation_outcome(source)
    confidence_before_cap = config.confidence_by_investigation_outcome.for_outcome(outcome)
    cap = config.capability_confidence_cap.for_status(source.capability_status)
    confidence = SeverityLevel(min(confidence_before_cap, cap))
    cell = calculate_cell(config, impact, confidence)
    disposition = cell.disposition
    reasons = [
        GradingReasonCode.IMPACT_DETECTOR_MAPPING,
        _OUTCOME_REASON[outcome],
        _CAPABILITY_REASON[source.capability_status],
        GradingReasonCode.COST_MATRIX_SELECTED,
        *cell.override_reason_codes,
    ]
    if confidence_before_cap > cap:
        reasons.append(GradingReasonCode.CONFIDENCE_CAP_APPLIED)

    if (
        outcome is not InvestigationGradingOutcome.SUFFICIENT
        and disposition is not Disposition.MANUAL_ATTENTION
    ):
        disposition = Disposition.MANUAL_ATTENTION
        reasons.append(GradingReasonCode.OVERRIDE_F7_NON_SUFFICIENT)
    if (
        source.capability_status is CapabilityStatus.UNAVAILABLE
        and disposition is not Disposition.MANUAL_ATTENTION
    ):
        disposition = Disposition.MANUAL_ATTENTION
        reasons.append(GradingReasonCode.OVERRIDE_CAPABILITY_UNAVAILABLE)
    reasons.append(_FINAL_REASON[disposition])

    evidence = CorrelationEvidenceSnapshot(
        source_kind=SourceKind.CORRELATION,
        rule_kind=None,
        detector=source.detector,
        source_outcome=outcome,
        capability_status=source.capability_status,
        mapped_impact=impact,
        confidence_before_cap=confidence_before_cap,
        confidence_after_cap=confidence,
        losses=cell.losses,
        matrix_disposition=cell.disposition,
    )
    payload = {
        "input_fingerprint": grading_input_fingerprint,
        "source": source,
        "severity_impact": impact,
        "severity_confidence": confidence,
        "matrix_disposition": cell.disposition,
        "disposition": disposition,
        "reason_codes": _ordered_reasons(reasons),
        "evidence_snapshot": evidence,
    }
    return GradingItemResult(
        source_kind=source.kind,
        source_id=source.source_id,
        first_row_no=source.first_row_no,
        severity_impact=impact,
        severity_confidence=confidence,
        matrix_disposition=cell.disposition,
        disposition=disposition,
        reason_codes=payload["reason_codes"],
        evidence_snapshot=evidence,
        item_fingerprint=item_fingerprint(payload),
    )


def _item_sort_key(item: GradingItemResult) -> tuple[int, int, int, int, int, str]:
    return (
        _DISPOSITION_RANK[item.disposition],
        -int(item.severity_impact),
        -int(item.severity_confidence),
        item.first_row_no,
        _SOURCE_RANK[item.source_kind],
        str(item.source_id),
    )


def grade(grading_input: GradingInput) -> GradingCoreResult:
    """Grade frozen F3/F6/F7 sources without any external side effect."""
    current_input_fingerprint = grading_input_fingerprint(grading_input)
    items = tuple(
        sorted(
            (
                *(
                    _grade_deterministic(grading_input.config, source, current_input_fingerprint)
                    for source in grading_input.deterministic_sources
                ),
                *(
                    _grade_correlation(grading_input.config, source, current_input_fingerprint)
                    for source in grading_input.correlation_sources
                ),
            ),
            key=_item_sort_key,
        )
    )
    result_payload = {
        "input_fingerprint": current_input_fingerprint,
        "items": items,
    }
    return GradingCoreResult(
        input_fingerprint=current_input_fingerprint,
        result_fingerprint=result_fingerprint(result_payload),
        items=items,
        deterministic_item_count=len(grading_input.deterministic_sources),
        correlation_item_count=len(grading_input.correlation_sources),
        high_attention_count=sum(item.disposition is Disposition.HIGH_ATTENTION for item in items),
        manual_attention_count=sum(
            item.disposition is Disposition.MANUAL_ATTENTION for item in items
        ),
        cleared_count=sum(item.disposition is Disposition.CLEARED for item in items),
    )
