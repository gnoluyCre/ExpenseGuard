from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.detection.canonical import finding_key, group_key_fingerprint, profile_fingerprint
from app.core.detection.engine import run_detection_core, stable_findings
from app.core.detection.models import DetectionBatch, DetectorKind, FindingDraft
from tests.unit.detection.helpers import batch, profile, record

pytestmark = pytest.mark.unit


def test_finding_key_excludes_reasoning_but_binds_facts_and_rows() -> None:
    records = [
        record(amount="500", employee="E", merchant="商户甲"),
        record(amount="500", employee="E", merchant="商户甲"),
    ]
    result = run_detection_core(profile(), batch(records))
    finding = result.outputs[0].findings[0]
    changed_reasoning = finding.model_copy(update={"reasoning_snapshot": "另一段稳定展示文案"})
    assert changed_reasoning.finding_key == finding.finding_key
    assert (
        finding_key(
            detector=finding.detector,
            detector_version=finding.detector_version,
            profile_fingerprint_value=finding.evidence.profile_fingerprint,
            participating_row_nos=finding.participating_row_nos,
            evidence=finding.evidence,
        )
        == finding.finding_key
    )
    assert (
        finding_key(
            detector=finding.detector,
            detector_version=finding.detector_version,
            profile_fingerprint_value=finding.evidence.profile_fingerprint,
            participating_row_nos=(1, 3),
            evidence=finding.evidence,
        )
        != finding.finding_key
    )


def test_stable_findings_deduplicates_equal_identity_and_orders_detector_enum() -> None:
    records = [
        record(amount="500", employee="E", merchant="商户甲", invoice_no="INV001"),
        record(amount="500", employee="E", merchant="商户甲", invoice_no="INV002"),
        record(amount="500", employee="E", merchant="商户甲", invoice_no="INV003"),
    ]
    result = run_detection_core(profile(), batch(records))
    findings = [finding for output in result.outputs for finding in output.findings]
    ordered = stable_findings([*reversed(findings), *findings])
    assert tuple(item.detector for item in ordered) == tuple(
        detector for detector in DetectorKind if any(item.detector is detector for item in findings)
    )
    assert len(ordered) == len(findings)


def test_stable_findings_rejects_well_formed_but_tampered_key() -> None:
    result = run_detection_core(
        profile(),
        batch(
            [
                record(amount="500", employee="E", merchant="商户甲"),
                record(amount="500", employee="E", merchant="商户甲"),
            ]
        ),
    )
    finding = result.outputs[0].findings[0]
    tampered = finding.model_copy(update={"finding_key": "0" * 64})
    with pytest.raises(ValueError, match="canonical payload"):
        stable_findings((tampered,))


def test_strict_input_rejects_duplicate_rows_and_incomplete_availability() -> None:
    valid = batch([record(), record(employee="B")])
    dumped = valid.model_dump(mode="python")
    rows = dumped["rows"]
    assert isinstance(rows, tuple)
    duplicated = {**dumped, "rows": (rows[0], rows[0])}
    with pytest.raises(ValidationError, match="row_no"):
        DetectionBatch.model_validate(duplicated)
    availability = dumped["field_availability"]
    assert isinstance(availability, tuple)
    with pytest.raises(ValidationError):
        DetectionBatch.model_validate({**dumped, "field_availability": availability[:-1]})


def test_finding_model_rejects_unsorted_rows_and_wrong_key() -> None:
    result = run_detection_core(
        profile(),
        batch(
            [
                record(amount="500", employee="E", merchant="商户甲"),
                record(amount="500", employee="E", merchant="商户甲"),
            ]
        ),
    )
    finding = result.outputs[0].findings[0]
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(
            finding.model_dump(mode="python") | {"participating_row_nos": (2, 1)}
        )
    with pytest.raises(ValidationError):
        FindingDraft.model_validate(finding.model_dump(mode="python") | {"finding_key": "bad"})


def test_group_and_profile_fingerprints_are_lower_hex() -> None:
    values = (group_key_fingerprint(("员工", "商户")), profile_fingerprint(profile()))
    assert all(len(value) == 64 for value in values)
    assert all(set(value) <= set("0123456789abcdef") for value in values)


def test_detection_core_ast_forbids_nondeterministic_or_external_dependencies() -> None:
    package = Path(__file__).resolve().parents[3] / "app" / "core" / "detection"
    forbidden_import_roots = {"asyncio", "httpx", "os", "random", "re", "socket", "sqlalchemy"}
    forbidden_calls = {"hash", "now", "today", "utcnow"}
    violations: list[str] = []
    pure_core_files = ("canonical.py", "engine.py", "models.py")
    for path in (package / name for name in pure_core_files):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] in forbidden_import_roots:
                        violations.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.split(".", 1)[0] in forbidden_import_roots:
                    violations.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                    violations.append(f"{path.name}: call {node.func.id}")
                if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls:
                    violations.append(f"{path.name}: call .{node.func.attr}")
    assert violations == []
