from __future__ import annotations

import uuid
from typing import Any

from app.core.grading.models import Disposition, GradingConfigV1


def _matrix(payload: dict[str, Any]) -> list[list[str]]:
    costs = payload["impact_cost_units"]
    probabilities = payload["confidence_issue_probability_bps"]
    matrix: list[list[str]] = []
    for impact in range(4):
        row: list[str] = []
        for confidence in range(4):
            probability = probabilities[confidence]
            candidates = (
                (
                    payload["false_positive_cost_units"] * 10_000 * (10_000 - probability),
                    0,
                    Disposition.HIGH_ATTENTION.value,
                ),
                (
                    payload["manual_review_cost_units"] * 10_000 * 10_000,
                    1,
                    Disposition.MANUAL_ATTENTION.value,
                ),
                (
                    costs[impact] * payload["false_negative_multiplier_bps"] * probability,
                    2,
                    Disposition.CLEARED.value,
                ),
            )
            selected = min(candidates, key=lambda item: (item[0], item[1]))[2]
            if selected == Disposition.CLEARED and (impact == 3 or confidence == 0):
                selected = Disposition.MANUAL_ATTENTION.value
            row.append(selected)
        matrix.append(row)
    return matrix


def config_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "algorithm_version": "cost-matrix-v1",
        "impact_by_rule_kind": {
            "limit": 0,
            "invoice_type": 1,
            "timeliness": 2,
            "invoice_title": 3,
            "invoice_duplicate": 2,
        },
        "impact_by_detector": {
            "split_invoice": 0,
            "sequential_invoice": 1,
            "frequency_anomaly": 2,
            "spatiotemporal_tier0": 3,
        },
        "confidence_by_investigation_outcome": {
            "sufficient": 3,
            "insufficient": 2,
            "unavailable": 1,
            "max_steps": 2,
            "failed": 1,
            "not_run": 0,
        },
        "capability_confidence_cap": {"enabled": 3, "degraded": 2, "unavailable": 0},
        "impact_cost_units": [0, 10, 100, 1000],
        "confidence_issue_probability_bps": [0, 1000, 5000, 9000],
        "false_negative_multiplier_bps": 10_000,
        "false_positive_cost_units": 100,
        "manual_review_cost_units": 20,
    }
    payload.update(overrides)
    payload["disposition_matrix"] = _matrix(payload)
    return payload


def config(**overrides: Any) -> GradingConfigV1:
    return GradingConfigV1.model_validate(config_payload(**overrides))


def hex64(number: int) -> str:
    return f"{number:064x}"


def stable_uuid(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)
