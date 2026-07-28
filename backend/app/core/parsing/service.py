"""批次结构化解析编排服务。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.parsing.availability import detect_field_availability
from app.core.parsing.mapping import (
    MappingValidationError,
    compute_header_signature,
    parse_expense_row,
    validate_mapping,
)
from app.core.parsing.models import (
    AvailabilityResult,
    BatchParseResult,
    MappingVersionConfig,
    RowParseResult,
)
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import current_tenant
from app.db.base import utc_now
from app.db.models.batch import (
    ExpenseRow,
    FieldAvailability,
    FieldStatus,
    FileVersion,
    ParseStatus,
)
from app.db.models.config import SchemaMapping, SchemaMappingVersion
from app.db.models.validation import ValidationDependency, ValidationRun

ROW_VALIDATION_FAILED = "ROW_VALIDATION_FAILED"
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class BatchParsingError(ExpenseGuardError):
    """可稳定映射到 API 的批次解析领域错误。"""

    status_code = 409


class BatchParseInternalError(BatchParsingError):
    """未分类系统异常；响应与审计都不得暴露异常详情。"""

    status_code = 500


async def parse_batch(
    db: AsyncSession,
    *,
    file_version_id: uuid.UUID,
    mapping_version_id: uuid.UUID,
) -> BatchParseResult:
    """原子解析一个批次。

    调用方拥有外层事务；保存点保证即使调用方捕获系统异常，本次行、
    可用性与批次状态写入也会一起回滚。最终提交仍由请求依赖统一完成。
    """
    try:
        async with db.begin_nested():
            tenant_id = current_tenant(db.sync_session)
            if tenant_id is None:
                raise RuntimeError("批次解析缺少租户上下文")
            await lock_tenant_nowait(db, tenant_id)
            file_version = await _lock_file_version(db, file_version_id)
            await _validate_not_frozen_by_validation(db, file_version_id)
            mapping_version = await db.get(SchemaMappingVersion, mapping_version_id)
            if mapping_version is None:
                raise NotFoundError(code="MAPPING_VERSION_NOT_FOUND", message="映射版本不存在")

            rows = tuple(
                (
                    await db.scalars(
                        select(ExpenseRow)
                        .where(ExpenseRow.file_version_id == file_version_id)
                        .order_by(ExpenseRow.row_no)
                    )
                ).all()
            )
            _validate_batch_invariants(file_version, rows)
            source_columns = _source_columns(rows)
            if compute_header_signature(source_columns) != mapping_version.header_signature:
                raise BatchParsingError(
                    code="MAPPING_HEADER_MISMATCH",
                    message="映射版本与批次表头不匹配",
                )

            config = await _load_mapping_config(db, mapping_version)
            validate_mapping(config, source_columns, uploaded_at=file_version.uploaded_at)

            if (
                file_version.mapping_version_id == mapping_version_id
                and file_version.parse_status
                in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS}
            ):
                return await _reused_result(db, file_version, mapping_version)

            parsed_rows = tuple(
                parse_expense_row(
                    row.raw_json,
                    config=config,
                    uploaded_at=file_version.uploaded_at,
                )
                for row in rows
            )
            _replace_row_results(rows, parsed_rows)
            availability = detect_field_availability(
                parsed_rows,
                config=config,
                total_rows=cast("int", file_version.row_count),
            )
            await _replace_availability(
                db,
                file_version=file_version,
                results=availability,
            )

            error_count = sum(result.error_detail is not None for result in parsed_rows)
            parsed_at = utc_now()
            file_version.mapping_version_id = mapping_version.id
            file_version.parse_status = (
                ParseStatus.PARSED_WITH_ERRORS if error_count else ParseStatus.PARSED
            )
            file_version.parsed_at = parsed_at
            await db.flush()

            return _result(
                file_version=file_version,
                mapping_version=mapping_version,
                error_count=error_count,
                parsed_at=parsed_at,
                reused_existing=False,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise BatchParsingError(
                code="BATCH_PARSE_IN_PROGRESS",
                message="该批次正在解析，请稍后重试",
            ) from exc
        raise


async def _lock_file_version(db: AsyncSession, file_version_id: uuid.UUID) -> FileVersion:
    file_version = await db.scalar(
        select(FileVersion).where(FileVersion.id == file_version_id).with_for_update(nowait=True)
    )
    if file_version is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    return file_version


async def _validate_not_frozen_by_validation(db: AsyncSession, file_version_id: uuid.UUID) -> None:
    validation_run_id = await db.scalar(
        select(ValidationRun.id).where(ValidationRun.file_version_id == file_version_id).limit(1)
    )
    if validation_run_id is not None:
        raise BatchParsingError(
            code="BATCH_ALREADY_VALIDATED",
            message="该批次已完成或正在进行确定性校验，不能原地重解析",
        )

    dependency_id = await db.scalar(
        select(ValidationDependency.id)
        .where(ValidationDependency.depended_file_version_id == file_version_id)
        .limit(1)
    )
    if dependency_id is not None:
        raise BatchParsingError(
            code="BATCH_USED_BY_VALIDATION",
            message="该批次已被确定性校验引用，不能原地重解析",
        )


async def _load_mapping_config(
    db: AsyncSession, mapping_version: SchemaMappingVersion
) -> MappingVersionConfig:
    entries = tuple(
        (
            await db.scalars(
                select(SchemaMapping)
                .where(SchemaMapping.mapping_version_id == mapping_version.id)
                .order_by(SchemaMapping.source_column)
            )
        ).all()
    )
    raw_config = {
        "id": mapping_version.id,
        "version": mapping_version.version,
        "header_signature": mapping_version.header_signature,
        "mappings": [
            {"source_column": entry.source_column, "target_field": entry.target_field}
            for entry in entries
        ],
        "availability_thresholds": mapping_version.availability_thresholds,
        "currency_aliases": mapping_version.currency_aliases,
        "inference_config": mapping_version.inference_config,
    }
    try:
        return MappingVersionConfig.model_validate(raw_config)
    except ValidationError as exc:
        locations = {str(part) for error in exc.errors() for part in error["loc"]}
        if "availability_thresholds" in locations:
            code = "MAPPING_THRESHOLD_INVALID"
            message = "字段可用性阈值必须位于 0 到 1 之间"
        elif "target_field" in locations:
            code = "MAPPING_TARGET_FIELD_UNKNOWN"
            message = "映射包含未知目标字段"
        else:
            code = "MAPPING_INFERENCE_INVALID"
            message = "映射推断配置无效"
        raise MappingValidationError(code=code, message=message) from exc


def _replace_row_results(
    rows: tuple[ExpenseRow, ...], parsed_rows: tuple[RowParseResult, ...]
) -> None:
    for row, result in zip(rows, parsed_rows, strict=True):
        if result.normalized is not None:
            row.normalized_json = result.normalized.model_dump(mode="json")
            row.parse_error_code = None
            row.parse_error = None
            row.parse_error_detail = None
            continue
        if result.error_detail is None:  # pragma: no cover - Pydantic model invariant
            raise RuntimeError("失败行缺少结构化错误详情")
        error_count = len(result.error_detail.errors)
        row.normalized_json = None
        row.parse_error_code = ROW_VALIDATION_FAILED
        row.parse_error = f"该行有 {error_count} 个字段无法解析"
        row.parse_error_detail = result.error_detail.model_dump(mode="json")


async def _replace_availability(
    db: AsyncSession,
    *,
    file_version: FileVersion,
    results: tuple[AvailabilityResult, ...],
) -> None:
    existing = tuple(
        (
            await db.scalars(
                select(FieldAvailability).where(
                    FieldAvailability.file_version_id == file_version.id
                )
            )
        ).all()
    )
    by_field = {item.field_name: item for item in existing}
    expected = {result.field_name.value for result in results}
    for stale in (item for item in existing if item.field_name not in expected):
        await db.delete(stale)
    for result in results:
        item = by_field.get(result.field_name.value)
        if item is None:
            item = FieldAvailability(
                tenant_id=file_version.tenant_id,
                file_version_id=file_version.id,
                field_name=result.field_name.value,
                status=FieldStatus(result.status),
                evidence=result.evidence.model_dump(mode="json"),
            )
            db.add(item)
        else:
            item.status = FieldStatus(result.status)
            item.evidence = result.evidence.model_dump(mode="json")


async def _reused_result(
    db: AsyncSession,
    file_version: FileVersion,
    mapping_version: SchemaMappingVersion,
) -> BatchParseResult:
    if file_version.parsed_at is None:
        raise RuntimeError("成功解析状态缺少 parsed_at")
    error_count = int(
        await db.scalar(
            select(func.count())
            .select_from(ExpenseRow)
            .where(
                ExpenseRow.file_version_id == file_version.id,
                ExpenseRow.parse_error_code == ROW_VALIDATION_FAILED,
            )
        )
        or 0
    )
    return _result(
        file_version=file_version,
        mapping_version=mapping_version,
        error_count=error_count,
        parsed_at=file_version.parsed_at,
        reused_existing=True,
    )


def _result(
    *,
    file_version: FileVersion,
    mapping_version: SchemaMappingVersion,
    error_count: int,
    parsed_at: datetime,
    reused_existing: bool,
) -> BatchParseResult:
    total_rows = cast("int", file_version.row_count)
    status = (
        "parsed_with_errors"
        if file_version.parse_status is ParseStatus.PARSED_WITH_ERRORS
        else "parsed"
    )
    return BatchParseResult(
        file_version_id=file_version.id,
        mapping_version_id=mapping_version.id,
        mapping_version=mapping_version.version,
        status=status,
        total_rows=total_rows,
        success_count=total_rows - error_count,
        error_count=error_count,
        parsed_at=parsed_at.isoformat().replace("+00:00", "Z"),
        reused_existing=reused_existing,
    )


def _validate_batch_invariants(file_version: FileVersion, rows: tuple[ExpenseRow, ...]) -> None:
    if file_version.row_count is None or file_version.row_count <= 0:
        raise RuntimeError("批次缺少有效 row_count")
    if len(rows) != file_version.row_count:
        raise RuntimeError("批次 row_count 与 expense_row 数量不一致")


def _source_columns(rows: tuple[ExpenseRow, ...]) -> tuple[str, ...]:
    if not rows:
        raise RuntimeError("批次没有原始行")
    columns = tuple(rows[0].raw_json)
    if not columns or any(not column for column in columns):
        raise RuntimeError("批次表头为空")
    expected = set(columns)
    if any(set(row.raw_json) != expected for row in rows[1:]):
        raise RuntimeError("批次原始行的表头不一致")
    return columns


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
