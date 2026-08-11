from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.core.grading.matrix import calculate_cell, generate_matrix_cells
from app.core.grading.models import (
    CapabilityConfidenceCap,
    CorrelationGradingSource,
    Disposition,
    GradingConfigV1,
    GradingReasonCode,
    InvestigationGradingOutcome,
    InvestigationRunSource,
    ParticipantRow,
    SeverityLevel,
)
from tests.unit.grading.helpers import config, config_payload, hex64, stable_uuid


def test_reason_code_contract_has_exact_specification_order() -> None:
    assert len(GradingReasonCode) == 23
    assert tuple(reason.value for reason in GradingReasonCode)[0::22] == (
        "IMPACT_RULE_MAPPING",
        "FINAL_CLEARED",
    )


@pytest.mark.parametrize("bad_value", [True, False, 1.0, "1"])
@pytest.mark.parametrize(
    "field",
    [
        "false_negative_multiplier_bps",
        "false_positive_cost_units",
        "manual_review_cost_units",
    ],
)
def test_cost_parameters_reject_coercion(field: str, bad_value: object) -> None:
    payload = config_payload()
    payload[field] = bad_value
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)


@pytest.mark.parametrize("bad_value", [True, 1.0, "1"])
def test_level_and_vector_values_reject_coercion(bad_value: object) -> None:
    payload = config_payload()
    payload["impact_by_rule_kind"]["limit"] = bad_value
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    payload = config_payload()
    payload["impact_cost_units"][1] = bad_value
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)


def test_config_rejects_missing_extra_unknown_and_unsafe_values() -> None:
    payload = config_payload()
    del payload["impact_by_rule_kind"]["limit"]
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    payload = config_payload()
    payload["impact_by_detector"]["unknown"] = 1
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    payload = config_payload()
    payload["confidence_by_investigation_outcome"]["not_run"] = 1
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    with pytest.raises(ValidationError):
        CapabilityConfidenceCap(enabled=3, degraded=3, unavailable=0)

    for field in (
        "false_negative_multiplier_bps",
        "false_positive_cost_units",
        "manual_review_cost_units",
    ):
        payload = config_payload()
        payload[field] = 1_000_001
        with pytest.raises(ValidationError):
            GradingConfigV1.model_validate(payload)


def test_vectors_and_submitted_matrix_are_mechanically_validated() -> None:
    payload = config_payload()
    payload["impact_cost_units"] = [0, 10, 10, 20]
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    payload = config_payload()
    payload["confidence_issue_probability_bps"] = [0, 1000, 500, 9000]
    with pytest.raises(ValidationError):
        GradingConfigV1.model_validate(payload)

    payload = config_payload()
    payload["disposition_matrix"][1][1] = "high_attention"
    with pytest.raises(ValidationError, match="mechanically generated"):
        GradingConfigV1.model_validate(payload)


def test_all_16_cells_use_integer_losses_and_match_matrix() -> None:
    grading_config = config()
    cells = generate_matrix_cells(grading_config)
    assert len(cells) == 16
    assert {(int(cell.impact), int(cell.confidence)) for cell in cells} == {
        (impact, confidence) for impact in range(4) for confidence in range(4)
    }
    for cell in cells:
        assert all(type(value) is int for value in cell.losses.model_dump().values())
        assert cell.disposition is grading_config.disposition_matrix[cell.impact][cell.confidence]


def test_maximum_parameters_keep_losses_in_signed_64_bit_range() -> None:
    grading_config = config(
        impact_cost_units=[0, 999_998, 999_999, 1_000_000],
        confidence_issue_probability_bps=[0, 9998, 9999, 10_000],
        false_negative_multiplier_bps=1_000_000,
        false_positive_cost_units=1_000_000,
        manual_review_cost_units=1_000_000,
    )
    losses = calculate_cell(grading_config, SeverityLevel.LEVEL_3, SeverityLevel.LEVEL_3).losses
    assert max(losses.clear_loss, losses.flag_loss, losses.review_loss) <= 2**63 - 1


def test_conservative_ties_choose_high_then_manual_before_clear() -> None:
    high_tie = config(
        impact_cost_units=[0, 1, 2, 3],
        confidence_issue_probability_bps=[0, 5000, 6000, 7000],
        false_negative_multiplier_bps=10_000,
        false_positive_cost_units=1,
        manual_review_cost_units=100,
    )
    cell = calculate_cell(high_tie, SeverityLevel.LEVEL_1, SeverityLevel.LEVEL_1)
    assert cell.losses.clear_loss == cell.losses.flag_loss
    assert cell.selected_disposition is Disposition.HIGH_ATTENTION

    review_tie = config(
        impact_cost_units=[0, 100, 200, 300],
        confidence_issue_probability_bps=[0, 1000, 2000, 3000],
        false_negative_multiplier_bps=10_000,
        false_positive_cost_units=100,
        manual_review_cost_units=10,
    )
    cell = calculate_cell(review_tie, SeverityLevel.LEVEL_1, SeverityLevel.LEVEL_1)
    assert cell.losses.clear_loss == cell.losses.review_loss
    assert cell.selected_disposition is Disposition.MANUAL_ATTENTION


def test_two_generic_safety_overrides_are_matrix_only() -> None:
    grading_config = config(
        impact_cost_units=[0, 1, 2, 3],
        confidence_issue_probability_bps=[0, 1, 2, 3],
        false_negative_multiplier_bps=1,
        false_positive_cost_units=1_000_000,
        manual_review_cost_units=1_000_000,
    )
    impact_override = calculate_cell(grading_config, SeverityLevel.LEVEL_3, SeverityLevel.LEVEL_1)
    assert impact_override.selected_disposition is Disposition.CLEARED
    assert impact_override.disposition is Disposition.MANUAL_ATTENTION
    assert impact_override.override_reason_codes == (GradingReasonCode.OVERRIDE_IMPACT_3,)

    confidence_override = calculate_cell(
        grading_config, SeverityLevel.LEVEL_1, SeverityLevel.LEVEL_0
    )
    assert confidence_override.selected_disposition is Disposition.CLEARED
    assert confidence_override.disposition is Disposition.MANUAL_ATTENTION
    assert confidence_override.override_reason_codes == (GradingReasonCode.OVERRIDE_CONFIDENCE_0,)


def test_correlation_requires_explicit_consistent_investigation_and_rows() -> None:
    base = {
        "correlation_finding_id": stable_uuid(1),
        "detector": "split_invoice",
        "finding_key": hex64(1),
        "evidence_fingerprint": hex64(2),
        "participating_rows": [
            {"row_no": 1, "source_row_fingerprint": hex64(3)},
            {"row_no": 2, "source_row_fingerprint": hex64(4)},
        ],
        "capability_status": "enabled",
    }
    with pytest.raises(ValidationError):
        CorrelationGradingSource.model_validate(base)

    duplicated = copy.deepcopy(base)
    duplicated["participating_rows"][1]["row_no"] = 1
    duplicated["investigation"] = {"kind": "not_run"}
    with pytest.raises(ValidationError):
        CorrelationGradingSource.model_validate(duplicated)

    with pytest.raises(ValidationError):
        InvestigationRunSource(
            outcome=InvestigationGradingOutcome.SUFFICIENT,
            evidence_sufficient=False,
        )

    assert ParticipantRow(row_no=1, source_row_fingerprint=hex64(1)).row_no == 1


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (InvestigationGradingOutcome.SUFFICIENT, True),
        (InvestigationGradingOutcome.INSUFFICIENT, False),
        (InvestigationGradingOutcome.UNAVAILABLE, None),
        (InvestigationGradingOutcome.MAX_STEPS, None),
        (InvestigationGradingOutcome.FAILED, None),
    ],
)
def test_f7_terminal_evidence_sufficient_is_strict_three_state(
    outcome: InvestigationGradingOutcome, expected: bool | None
) -> None:
    assert (
        InvestigationRunSource(outcome=outcome, evidence_sufficient=expected).evidence_sufficient
        is expected
    )
    wrong = False if expected is not False else None
    with pytest.raises(ValidationError, match="evidence_sufficient"):
        InvestigationRunSource(outcome=outcome, evidence_sufficient=wrong)
