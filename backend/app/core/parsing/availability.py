"""全部统一字段的三级可用性探测。"""

from __future__ import annotations

from decimal import Decimal

from app.core.parsing.models import (
    UNIFIED_FIELDS,
    AvailabilityEvidence,
    AvailabilityResult,
    DirectAvailabilityEvidence,
    InferenceAvailabilityEvidence,
    MappingVersionConfig,
    ProvenanceMode,
    RowParseResult,
    UnifiedField,
)


def detect_field_availability(
    rows: tuple[RowParseResult, ...],
    *,
    config: MappingVersionConfig,
    total_rows: int,
) -> tuple[AvailabilityResult, ...]:
    """按固定顺序产生 12 项结果；失败行仍计入分母。"""
    if total_rows <= 0 or len(rows) != total_rows:
        raise ValueError("可用性探测要求非零且与批次行数一致的输入")

    direct_sources = {entry.target_field: entry.source_column for entry in config.mappings}
    inference_by_target: dict[UnifiedField, list[str]] = {}
    for rule in config.inference_config.rules:
        inference_by_target.setdefault(rule.target_field, []).append(rule.rule_id)

    results: list[AvailabilityResult] = []
    for field in UNIFIED_FIELDS:
        direct_count = _count(rows, field, ProvenanceMode.MAPPED)
        inference_count = _count(rows, field, ProvenanceMode.INFERRED)
        direct_rate = Decimal(direct_count) / Decimal(total_rows)
        inference_rate = Decimal(inference_count) / Decimal(total_rows)
        direct_configured = field in direct_sources
        inference_rule_ids = tuple(inference_by_target.get(field, ()))

        if (
            direct_configured
            and direct_rate >= config.availability_thresholds.available_min_non_null_rate
        ):
            status = "available"
            selected_basis = "direct"
        elif (
            not direct_configured
            and inference_rule_ids
            and inference_rate >= config.availability_thresholds.inferred_min_success_rate
        ):
            status = "inferred"
            selected_basis = "inference"
        else:
            status = "missing"
            selected_basis = "none"

        evidence = AvailabilityEvidence(
            mapping_version_id=config.id,
            total_rows=total_rows,
            direct=DirectAvailabilityEvidence(
                configured=direct_configured,
                source_columns=(direct_sources[field],) if direct_configured else (),
                non_null_count=direct_count,
                non_null_rate=_rate(direct_rate),
                threshold=_rate(config.availability_thresholds.available_min_non_null_rate),
            ),
            inference=InferenceAvailabilityEvidence(
                configured=bool(inference_rule_ids),
                rule_ids=inference_rule_ids,
                success_count=inference_count,
                success_rate=_rate(inference_rate),
                threshold=_rate(config.availability_thresholds.inferred_min_success_rate),
            ),
            selected_basis=selected_basis,
        )
        results.append(AvailabilityResult(field_name=field, status=status, evidence=evidence))
    return tuple(results)


def _count(rows: tuple[RowParseResult, ...], field: UnifiedField, mode: ProvenanceMode) -> int:
    return sum(
        1
        for row in rows
        if (provenance := row.observed_provenance.get(field)) is not None
        and provenance.mode is mode
    )


def _rate(value: Decimal) -> str:
    return f"{value:.4f}"
