"""Pure integer cost matrix for F8 grading."""

from __future__ import annotations

from typing import cast

from app.core.grading.models import (
    CostLosses,
    Disposition,
    DispositionMatrix,
    GradingConfigV1,
    GradingReasonCode,
    MatrixCellResult,
    SeverityLevel,
)

_ACTION_PRIORITY: dict[Disposition, int] = {
    Disposition.HIGH_ATTENTION: 0,
    Disposition.MANUAL_ATTENTION: 1,
    Disposition.CLEARED: 2,
}


def calculate_cell(
    config: GradingConfigV1, impact: SeverityLevel, confidence: SeverityLevel
) -> MatrixCellResult:
    probability = config.confidence_issue_probability_bps[confidence]
    losses = CostLosses(
        clear_loss=(
            config.impact_cost_units[impact] * config.false_negative_multiplier_bps * probability
        ),
        flag_loss=config.false_positive_cost_units * 10_000 * (10_000 - probability),
        review_loss=config.manual_review_cost_units * 10_000 * 10_000,
    )
    candidates = (
        (
            losses.flag_loss,
            _ACTION_PRIORITY[Disposition.HIGH_ATTENTION],
            Disposition.HIGH_ATTENTION,
        ),
        (
            losses.review_loss,
            _ACTION_PRIORITY[Disposition.MANUAL_ATTENTION],
            Disposition.MANUAL_ATTENTION,
        ),
        (losses.clear_loss, _ACTION_PRIORITY[Disposition.CLEARED], Disposition.CLEARED),
    )
    selected = min(candidates, key=lambda item: (item[0], item[1]))[2]
    disposition = selected
    overrides: list[GradingReasonCode] = []
    if disposition is Disposition.CLEARED and impact is SeverityLevel.LEVEL_3:
        disposition = Disposition.MANUAL_ATTENTION
        overrides.append(GradingReasonCode.OVERRIDE_IMPACT_3)
    if disposition is Disposition.CLEARED and confidence is SeverityLevel.LEVEL_0:
        disposition = Disposition.MANUAL_ATTENTION
        overrides.append(GradingReasonCode.OVERRIDE_CONFIDENCE_0)
    return MatrixCellResult(
        impact=impact,
        confidence=confidence,
        losses=losses,
        selected_disposition=selected,
        disposition=disposition,
        override_reason_codes=tuple(overrides),
    )


def generate_matrix_cells(config: GradingConfigV1) -> tuple[MatrixCellResult, ...]:
    return tuple(
        calculate_cell(config, impact, confidence)
        for impact in SeverityLevel
        for confidence in SeverityLevel
    )


def generate_disposition_matrix(config: GradingConfigV1) -> DispositionMatrix:
    rows = tuple(
        tuple(
            calculate_cell(config, impact, confidence).disposition for confidence in SeverityLevel
        )
        for impact in SeverityLevel
    )
    return cast(DispositionMatrix, rows)
