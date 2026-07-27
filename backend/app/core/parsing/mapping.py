"""映射完整性、推断配置与单行解析。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Any, cast

from app.core.errors import ExpenseGuardError
from app.core.parsing.models import (
    REQUIRED_FIELDS,
    UNIFIED_FIELDS,
    ConstantInferenceRule,
    FieldError,
    FieldErrorCode,
    FieldProvenance,
    LiteralLookupInferenceRule,
    MappingEntry,
    MappingVersionConfig,
    NormalizedExpenseRecord,
    ProvenanceMode,
    RowErrorDetail,
    RowParseResult,
    UnifiedField,
)
from app.core.parsing.normalizers import (
    NormalizationError,
    normalize_amount,
    normalize_currency,
    normalize_date,
    normalize_invoice_no,
    normalize_lookup_text,
    normalize_text,
)


class MappingValidationError(ExpenseGuardError):
    """映射或推断配置不满足稳定契约。"""

    status_code = 422


def compute_header_signature(source_columns: tuple[str, ...]) -> str:
    """按 Unicode 码点排序后的紧凑 JSON 计算稳定 SHA-256。"""
    payload = json.dumps(sorted(source_columns), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def validate_mapping(
    config: MappingVersionConfig,
    source_columns: tuple[str, ...],
    *,
    uploaded_at: datetime | None = None,
) -> None:
    """按稳定优先级校验映射完整性与推断依赖。"""
    known_sources = set(source_columns)
    seen_sources: set[str] = set()
    seen_targets: set[UnifiedField] = set()
    direct_by_target: dict[UnifiedField, MappingEntry] = {}

    for entry in config.mappings:
        if entry.source_column not in known_sources:
            _mapping_error("MAPPING_SOURCE_COLUMN_UNKNOWN", "映射包含批次中不存在的源列")
        if entry.source_column in seen_sources:
            _mapping_error("MAPPING_SOURCE_COLUMN_DUPLICATED", "同一源列不能重复映射")
        if entry.target_field in seen_targets:
            _mapping_error("MAPPING_TARGET_FIELD_DUPLICATED", "同一目标字段不能重复直接映射")
        seen_sources.add(entry.source_column)
        seen_targets.add(entry.target_field)
        direct_by_target[entry.target_field] = entry

    missing_required = [
        field for field in UNIFIED_FIELDS if field in REQUIRED_FIELDS - seen_targets
    ]
    if missing_required:
        names = ", ".join(field.value for field in missing_required)
        _mapping_error("MAPPING_REQUIRED_FIELD_MISSING", f"映射缺少必填字段: {names}")

    seen_rule_ids: set[str] = set()
    inferred_targets: set[UnifiedField] = set()
    for rule in config.inference_config.rules:
        if rule.rule_id in seen_rule_ids or rule.target_field in inferred_targets:
            _mapping_error("MAPPING_INFERENCE_INVALID", "推断规则 ID 或目标字段重复")
        if rule.target_field in REQUIRED_FIELDS or rule.target_field in seen_targets:
            _mapping_error("MAPPING_INFERENCE_INVALID", "推断只能写入未直接映射的可选字段")
        if isinstance(rule, LiteralLookupInferenceRule):
            if len(set(rule.source_fields)) != len(rule.source_fields):
                _mapping_error("MAPPING_INFERENCE_INVALID", "推断来源字段不能重复")
            if any(field not in direct_by_target for field in rule.source_fields):
                _mapping_error("MAPPING_INFERENCE_INVALID", "字面量推断只能读取已直接映射字段")
            literals = [unicodedata.normalize("NFKC", item.literal) for item in rule.cases]
            if len(set(literals)) != len(literals):
                _mapping_error("MAPPING_INFERENCE_INVALID", "字面量推断匹配项不能重复")
            outputs = tuple(item.value for item in rule.cases)
        else:
            outputs = (rule.value,)
        for output in outputs:
            _validate_inference_output(
                rule.target_field,
                output,
                config=config,
                uploaded_at=uploaded_at,
            )
        seen_rule_ids.add(rule.rule_id)
        inferred_targets.add(rule.target_field)

    canonical_aliases: set[str] = set()
    for alias, target in config.currency_aliases.items():
        canonical_alias = unicodedata.normalize("NFKC", alias).strip().upper()
        if not canonical_alias or canonical_alias in canonical_aliases:
            _mapping_error("MAPPING_INFERENCE_INVALID", "币种别名配置无效或重复")
        canonical_aliases.add(canonical_alias)
        try:
            valid_target = normalize_currency(target, aliases={})
        except NormalizationError as exc:
            raise MappingValidationError(
                code="MAPPING_INFERENCE_INVALID", message="币种别名配置无效"
            ) from exc
        if valid_target is None:
            _mapping_error("MAPPING_INFERENCE_INVALID", "币种别名配置无效")


def parse_expense_row(
    raw_json: dict[str, Any],
    *,
    config: MappingVersionConfig,
    uploaded_at: datetime,
) -> RowParseResult:
    """从不可变 ``raw_json`` 解析单行；收集全部字段数据错误。"""
    direct_by_target = {entry.target_field: entry for entry in config.mappings}
    values: dict[UnifiedField, str | None] = {field: None for field in UNIFIED_FIELDS}
    provenance: dict[UnifiedField, FieldProvenance] = {}
    errors: list[FieldError] = []
    currency_markers = frozenset(config.currency_aliases) | frozenset(
        config.currency_aliases.values()
    )

    for field in UNIFIED_FIELDS:
        entry = direct_by_target.get(field)
        if entry is None:
            continue
        raw_value = raw_json.get(entry.source_column)
        try:
            if field in REQUIRED_FIELDS and _is_blank(raw_value):
                raise NormalizationError("REQUIRED_VALUE_MISSING", "必填字段为空")
            values[field] = _normalize_field(
                field,
                raw_value,
                uploaded_at=uploaded_at,
                currency_aliases=config.currency_aliases,
                currency_markers=currency_markers,
            )
            if values[field] is not None:
                provenance[field] = FieldProvenance(
                    mode=ProvenanceMode.MAPPED,
                    source_columns=(entry.source_column,),
                )
        except NormalizationError as exc:
            errors.append(
                FieldError(
                    field=field,
                    code=cast("FieldErrorCode", exc.code),
                    source_column=entry.source_column,
                    message=exc.message,
                )
            )

    _apply_inference(
        values,
        provenance,
        config=config,
        direct_by_target=direct_by_target,
        uploaded_at=uploaded_at,
    )
    if errors:
        return RowParseResult(
            error_detail=RowErrorDetail(
                mapping_version_id=config.id,
                errors=tuple(errors),
            ),
            observed_provenance=provenance,
        )
    normalized = NormalizedExpenseRecord(
        mapping_version_id=config.id,
        amount=cast("str", values[UnifiedField.AMOUNT]),
        expense_date=cast("str", values[UnifiedField.EXPENSE_DATE]),
        employee=values[UnifiedField.EMPLOYEE],
        expense_type=values[UnifiedField.EXPENSE_TYPE],
        invoice_type=values[UnifiedField.INVOICE_TYPE],
        invoice_no=values[UnifiedField.INVOICE_NO],
        merchant=values[UnifiedField.MERCHANT],
        invoice_title=values[UnifiedField.INVOICE_TITLE],
        submission_date=values[UnifiedField.SUBMISSION_DATE],
        location=values[UnifiedField.LOCATION],
        currency=values[UnifiedField.CURRENCY],
        description=values[UnifiedField.DESCRIPTION],
        field_provenance=provenance,
    )
    return RowParseResult(normalized=normalized, observed_provenance=provenance)


def _apply_inference(
    values: dict[UnifiedField, str | None],
    provenance: dict[UnifiedField, FieldProvenance],
    *,
    config: MappingVersionConfig,
    direct_by_target: dict[UnifiedField, MappingEntry],
    uploaded_at: datetime,
) -> None:
    for rule in config.inference_config.rules:
        inferred: str | None = None
        source_columns: tuple[str, ...] = ()
        if isinstance(rule, ConstantInferenceRule):
            inferred = rule.value
        else:
            source_columns = tuple(
                direct_by_target[field].source_column for field in rule.source_fields
            )
            source_texts = tuple(
                normalize_lookup_text(values[field]) for field in rule.source_fields
            )
            for case in rule.cases:
                literal = normalize_lookup_text(case.literal)
                if any(literal in source_text for source_text in source_texts):
                    inferred = case.value
                    break
        if inferred is None:
            continue
        try:
            normalized = _normalize_field(
                rule.target_field,
                inferred,
                uploaded_at=uploaded_at,
                currency_aliases=config.currency_aliases,
                currency_markers=frozenset(),
            )
        except NormalizationError as exc:
            raise MappingValidationError(
                code="MAPPING_INFERENCE_INVALID",
                message="推断规则输出无法按目标字段归一化",
            ) from exc
        if normalized is not None:
            values[rule.target_field] = normalized
            provenance[rule.target_field] = FieldProvenance(
                mode=ProvenanceMode.INFERRED,
                source_columns=source_columns,
                inference_rule_id=rule.rule_id,
            )


def _normalize_field(
    field: UnifiedField,
    value: object,
    *,
    uploaded_at: datetime,
    currency_aliases: dict[str, str],
    currency_markers: frozenset[str],
) -> str | None:
    if field is UnifiedField.AMOUNT:
        return normalize_amount(value, currency_markers=currency_markers)
    if field in {UnifiedField.EXPENSE_DATE, UnifiedField.SUBMISSION_DATE}:
        if _is_blank(value) and field is UnifiedField.SUBMISSION_DATE:
            return None
        return normalize_date(value, uploaded_at=uploaded_at)
    if field is UnifiedField.INVOICE_NO:
        return normalize_invoice_no(value)
    if field is UnifiedField.CURRENCY:
        return normalize_currency(value, aliases=currency_aliases)
    max_length = 2000 if field is UnifiedField.DESCRIPTION else 512
    return normalize_text(value, max_length=max_length)


def _validate_inference_output(
    field: UnifiedField,
    value: str,
    *,
    config: MappingVersionConfig,
    uploaded_at: datetime | None,
) -> None:
    if field is UnifiedField.SUBMISSION_DATE and uploaded_at is None:
        # 日期上限依赖不可变 uploaded_at；保存/解析服务应传入批次锚点。
        return
    try:
        _normalize_field(
            field,
            value,
            uploaded_at=cast("datetime", uploaded_at),
            currency_aliases=config.currency_aliases,
            currency_markers=frozenset(),
        )
    except NormalizationError as exc:
        raise MappingValidationError(
            code="MAPPING_INFERENCE_INVALID",
            message="推断规则输出无法按目标字段归一化",
        ) from exc


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _mapping_error(code: str, message: str) -> None:
    raise MappingValidationError(code=code, message=message)
