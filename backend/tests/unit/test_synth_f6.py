from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from app.synth import DATA_COLUMNS, CorrelationScenario, build_f6_synthetic_cases

pytestmark = pytest.mark.unit


def test_f6_cases_cover_four_physical_patterns_and_replay_exactly() -> None:
    first = build_f6_synthetic_cases(anchor_date=date(2026, 6, 30))
    second = build_f6_synthetic_cases(anchor_date=date(2026, 6, 30))

    assert first == second
    assert tuple(case.scenario for case in first) == tuple(CorrelationScenario)
    assert all(case.truths for case in first)
    assert all(len(case.rows) >= 4 for case in first)


def test_f6_input_rows_have_exact_columns_and_no_label_leak() -> None:
    label_names = {"detector", "participating_row_nos", "boundary", "degraded_fields"}
    for case in build_f6_synthetic_cases():
        for row in case.rows:
            assert tuple(row) == DATA_COLUMNS
            assert set(row).isdisjoint(label_names)
        assert all(
            set(truth.participating_row_nos) <= set(range(1, len(case.rows) + 1))
            for truth in case.truths
        )


def test_f6_cases_include_boundaries_noise_and_degradation() -> None:
    cases = {case.scenario: case for case in build_f6_synthetic_cases()}
    assert cases[CorrelationScenario.SPLIT_BOUNDARY].truths[0].boundary == (
        "gte_total_and_floor_equal"
    )
    assert cases[CorrelationScenario.SEQUENTIAL_WITH_NOISE].truths[0].degraded_fields
    assert cases[CorrelationScenario.FREQUENCY_WITH_BASELINE].truths[0].boundary == (
        "mad_zero_uses_explicit_floor"
    )
    assert cases[CorrelationScenario.SPATIOTEMPORAL_WITH_UNMAPPED].truths[0].degraded_fields


def test_f6_synth_module_does_not_import_or_call_random_float_hash_regex() -> None:
    path = Path(__file__).resolve().parents[2] / "app" / "synth" / "correlation.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".", 1)[0] in {"random", "re"}
            )
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.split(".", 1)[0] in {"random", "re"}:
                violations.append(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"float", "hash"}
        ):
            violations.append(node.func.id)
    assert violations == []
