from __future__ import annotations

import socket

import pytest

from app.core.detection.models import CapabilityStatus, DetectorKind
from app.core.grading.grader import grade
from app.core.grading.models import (
    CorrelationGradingSource,
    DeterministicGradingSource,
    Disposition,
    GradingInput,
    GradingReasonCode,
    InvestigationGradingOutcome,
    InvestigationNotRunSource,
    InvestigationRunSource,
    ParticipantRow,
    SeverityLevel,
)
from app.core.rules.models import RuleKind, RuleOutcome
from tests.unit.grading.helpers import config, hex64, stable_uuid


def row(number: int) -> ParticipantRow:
    return ParticipantRow(row_no=number, source_row_fingerprint=hex64(number + 1000))


def deterministic(
    number: int, rule_kind: RuleKind, outcome: RuleOutcome
) -> DeterministicGradingSource:
    return DeterministicGradingSource(
        finding_id=stable_uuid(number),
        row=row(number),
        rule_kind=rule_kind,
        outcome=outcome,
        evidence_fingerprint=hex64(number),
    )


def investigation(outcome: InvestigationGradingOutcome) -> object:
    if outcome is InvestigationGradingOutcome.NOT_RUN:
        return InvestigationNotRunSource()
    evidence_sufficient: bool | None
    if outcome is InvestigationGradingOutcome.SUFFICIENT:
        evidence_sufficient = True
    elif outcome is InvestigationGradingOutcome.INSUFFICIENT:
        evidence_sufficient = False
    else:
        evidence_sufficient = None
    return InvestigationRunSource(outcome=outcome, evidence_sufficient=evidence_sufficient)


def correlation(
    number: int,
    detector: DetectorKind,
    outcome: InvestigationGradingOutcome,
    capability: CapabilityStatus,
) -> CorrelationGradingSource:
    return CorrelationGradingSource.model_validate(
        {
            "correlation_finding_id": stable_uuid(number),
            "detector": detector,
            "finding_key": hex64(number),
            "evidence_fingerprint": hex64(number + 1),
            "participating_rows": [
                row(number + 1).model_dump(mode="python"),
                row(number + 2).model_dump(mode="python"),
            ],
            "capability_status": capability,
            "investigation": investigation(outcome),
        }
    )


def grading_input(
    deterministic_sources: tuple[DeterministicGradingSource, ...] = (),
    correlation_sources: tuple[CorrelationGradingSource, ...] = (),
    *,
    grading_config: object | None = None,
) -> GradingInput:
    return GradingInput(
        config=config() if grading_config is None else grading_config,
        f3_manifest_fingerprint=hex64(9001),
        f6_manifest_fingerprint=hex64(9002),
        f7_manifest_fingerprint=hex64(9003),
        deterministic_sources=deterministic_sources,
        correlation_sources=correlation_sources,
    )


def test_all_five_rules_and_both_f3_outcomes() -> None:
    sources = tuple(
        deterministic(index * 2 + offset, kind, outcome)
        for index, kind in enumerate(RuleKind, start=1)
        for offset, outcome in enumerate((RuleOutcome.FLAGGED, RuleOutcome.UNAVAILABLE), start=1)
    )
    result = grade(grading_input(deterministic_sources=sources))
    assert result.deterministic_item_count == 10
    assert {item.evidence_snapshot.rule_kind for item in result.items} == set(RuleKind)
    for item in result.items:
        if item.evidence_snapshot.source_outcome is RuleOutcome.FLAGGED:
            assert item.severity_confidence is SeverityLevel.LEVEL_3
            assert GradingReasonCode.CONFIDENCE_F3_FLAGGED in item.reason_codes
        else:
            assert item.severity_confidence is SeverityLevel.LEVEL_0
            assert item.disposition is Disposition.MANUAL_ATTENTION
            assert GradingReasonCode.CONFIDENCE_F3_UNAVAILABLE in item.reason_codes


def test_four_detectors_six_outcomes_by_three_capabilities() -> None:
    sources = tuple(
        correlation(
            100 + detector_index * 100 + outcome_index * 10 + capability_index,
            detector,
            outcome,
            capability,
        )
        for detector_index, detector in enumerate(DetectorKind)
        for outcome_index, outcome in enumerate(InvestigationGradingOutcome)
        for capability_index, capability in enumerate(CapabilityStatus)
    )
    result = grade(grading_input(correlation_sources=sources))
    assert result.correlation_item_count == 4 * 6 * 3
    assert {item.evidence_snapshot.detector for item in result.items} == set(DetectorKind)

    confidence_map = config().confidence_by_investigation_outcome
    cap_map = config().capability_confidence_cap
    for source in sources:
        item = next(item for item in result.items if item.source_id == source.source_id)
        outcome = (
            InvestigationGradingOutcome.NOT_RUN
            if isinstance(source.investigation, InvestigationNotRunSource)
            else InvestigationGradingOutcome(source.investigation.outcome)
        )
        expected = min(
            confidence_map.for_outcome(outcome),
            cap_map.for_status(source.capability_status),
        )
        assert item.severity_confidence == expected
        if outcome is not InvestigationGradingOutcome.SUFFICIENT:
            assert item.disposition is Disposition.MANUAL_ATTENTION
        if source.capability_status is CapabilityStatus.UNAVAILABLE:
            assert item.severity_confidence is SeverityLevel.LEVEL_0
            assert item.disposition is Disposition.MANUAL_ATTENTION


def test_source_override_changes_high_to_manual_and_reason_order_is_fixed() -> None:
    source = correlation(
        700,
        DetectorKind.FREQUENCY_ANOMALY,
        InvestigationGradingOutcome.INSUFFICIENT,
        CapabilityStatus.ENABLED,
    )
    grading_config = config(
        confidence_by_investigation_outcome={
            "sufficient": 3,
            "insufficient": 3,
            "unavailable": 1,
            "max_steps": 2,
            "failed": 1,
            "not_run": 0,
        }
    )
    result = grade(grading_input(correlation_sources=(source,), grading_config=grading_config))
    item = result.items[0]
    assert item.matrix_disposition is Disposition.HIGH_ATTENTION
    assert item.disposition is Disposition.MANUAL_ATTENTION
    assert GradingReasonCode.OVERRIDE_F7_NON_SUFFICIENT in item.reason_codes
    assert item.reason_codes == tuple(
        reason for reason in GradingReasonCode if reason in item.reason_codes
    )
    assert GradingReasonCode.COST_MATRIX_SELECTED in item.reason_codes
    assert item.reason_codes[-1] is GradingReasonCode.FINAL_MANUAL_ATTENTION


def test_cap_reason_and_cap_applied_are_precise() -> None:
    source = correlation(
        800,
        DetectorKind.SEQUENTIAL_INVOICE,
        InvestigationGradingOutcome.SUFFICIENT,
        CapabilityStatus.DEGRADED,
    )
    item = grade(grading_input(correlation_sources=(source,))).items[0]
    assert item.severity_confidence is SeverityLevel.LEVEL_2
    assert GradingReasonCode.CAPABILITY_DEGRADED in item.reason_codes
    assert GradingReasonCode.CONFIDENCE_CAP_APPLIED in item.reason_codes


def test_overlapping_source_conditions_do_not_record_noop_overrides() -> None:
    source = correlation(
        850,
        DetectorKind.FREQUENCY_ANOMALY,
        InvestigationGradingOutcome.INSUFFICIENT,
        CapabilityStatus.UNAVAILABLE,
    )
    grading_config = config(false_positive_cost_units=1, manual_review_cost_units=20)
    item = grade(grading_input(correlation_sources=(source,), grading_config=grading_config)).items[
        0
    ]
    assert item.severity_confidence is SeverityLevel.LEVEL_0
    # unavailable cap is fixed at zero; confidence=0 first selects clear_loss=0, then the
    # generic matrix override changes it to manual. Neither source rule changes it again.
    assert item.matrix_disposition is Disposition.MANUAL_ATTENTION
    assert item.disposition is Disposition.MANUAL_ATTENTION
    assert GradingReasonCode.OVERRIDE_F7_NON_SUFFICIENT not in item.reason_codes
    assert GradingReasonCode.OVERRIDE_CAPABILITY_UNAVAILABLE not in item.reason_codes


def test_sort_fingerprint_and_results_are_stable_under_input_order() -> None:
    first = deterministic(901, RuleKind.INVOICE_TITLE, RuleOutcome.FLAGGED)
    second = deterministic(902, RuleKind.LIMIT, RuleOutcome.FLAGGED)
    correlation_source = correlation(
        903,
        DetectorKind.SPATIOTEMPORAL_TIER0,
        InvestigationGradingOutcome.SUFFICIENT,
        CapabilityStatus.ENABLED,
    )
    forward = grade(
        grading_input(
            deterministic_sources=(first, second), correlation_sources=(correlation_source,)
        )
    )
    reverse = grade(
        grading_input(
            deterministic_sources=(second, first), correlation_sources=(correlation_source,)
        )
    )
    assert forward == reverse
    assert forward.input_fingerprint == reverse.input_fingerprint
    assert forward.result_fingerprint == reverse.result_fingerprint
    assert [item.item_fingerprint for item in forward.items] == [
        item.item_fingerprint for item in reverse.items
    ]
    sort_keys = [
        (
            (Disposition.HIGH_ATTENTION, Disposition.MANUAL_ATTENTION, Disposition.CLEARED).index(
                item.disposition
            ),
            -int(item.severity_impact),
            -int(item.severity_confidence),
            item.first_row_no,
        )
        for item in forward.items
    ]
    assert sort_keys == sorted(sort_keys)


def test_duplicate_source_identity_is_rejected() -> None:
    source = deterministic(950, RuleKind.LIMIT, RuleOutcome.FLAGGED)
    with pytest.raises(ValueError, match="unique"):
        grading_input(deterministic_sources=(source, source))


def test_grader_performs_zero_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    source = correlation(
        999,
        DetectorKind.SPLIT_INVOICE,
        InvestigationGradingOutcome.NOT_RUN,
        CapabilityStatus.UNAVAILABLE,
    )
    result = grade(grading_input(correlation_sources=(source,)))
    assert result.items[0].disposition is Disposition.MANUAL_ATTENTION
