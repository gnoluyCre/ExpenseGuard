"""F2 API 所需的映射版本管理与解析结果查询服务。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.core.parsing.mapping import compute_header_signature, validate_mapping
from app.core.parsing.models import (
    UNIFIED_FIELDS,
    AvailabilityEvidence,
    AvailabilityThresholds,
    BatchParseResult,
    InferenceConfig,
    MappingEntry,
    MappingVersionConfig,
    RowErrorDetail,
)
from app.core.parsing.service import BatchParsingError
from app.core.security.auth_service import write_audit
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow, FieldAvailability, FileVersion, ParseStatus
from app.db.models.config import SchemaMapping, SchemaMappingVersion
from app.db.models.tenancy import Tenant


@dataclass(frozen=True)
class MappingVersionView:
    """可安全返回给 API 的不可变映射版本。"""

    id: uuid.UUID
    version: int
    created_at: datetime
    created_by: uuid.UUID | None
    is_current_for_batch: bool
    mappings: tuple[MappingEntry, ...]
    availability_thresholds: AvailabilityThresholds
    currency_aliases: dict[str, str]
    inference_config: InferenceConfig


@dataclass(frozen=True)
class MappingVersionsView:
    """某批次表头及其可复用映射版本。"""

    file_version_id: uuid.UUID
    header_signature: str
    source_columns: tuple[str, ...]
    versions: tuple[MappingVersionView, ...]


@dataclass(frozen=True)
class SavedMappingVersion:
    """保存映射后的版本与幂等复用标记。"""

    version: MappingVersionView
    reused_existing: bool


@dataclass(frozen=True)
class ParseErrorItem:
    """一条带原始证据的行级解析错误。"""

    row_no: int
    raw_json: dict[str, Any]
    parse_error_code: str
    parse_error: str
    parse_error_detail: RowErrorDetail


@dataclass(frozen=True)
class ParseErrorsPage:
    """分页后的解析错误清单。"""

    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    total: int
    offset: int
    limit: int
    items: tuple[ParseErrorItem, ...]


@dataclass(frozen=True)
class FieldAvailabilityItem:
    """单个统一字段的可用性声明。"""

    field_name: str
    status: Literal["available", "inferred", "missing"]
    evidence: AvailabilityEvidence


@dataclass(frozen=True)
class FieldAvailabilityView:
    """当前批次解析版本的全字段可用性声明。"""

    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    items: tuple[FieldAvailabilityItem, ...]


async def list_mapping_versions(
    db: AsyncSession, *, file_version_id: uuid.UUID
) -> MappingVersionsView:
    """按版本降序返回与批次表头精确匹配的映射。"""
    batch, source_columns, header_signature = await _batch_header(db, file_version_id)
    versions = tuple(
        (
            await db.scalars(
                select(SchemaMappingVersion)
                .where(SchemaMappingVersion.header_signature == header_signature)
                .order_by(SchemaMappingVersion.version.desc())
            )
        ).all()
    )
    views = tuple(
        [
            await _mapping_view(
                db,
                version,
                current_mapping_version_id=batch.mapping_version_id,
            )
            for version in versions
        ]
    )
    return MappingVersionsView(
        file_version_id=batch.id,
        header_signature=header_signature,
        source_columns=source_columns,
        versions=views,
    )


async def save_mapping_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    mappings: tuple[MappingEntry, ...],
    availability_thresholds: AvailabilityThresholds,
    currency_aliases: dict[str, str],
    inference_config: InferenceConfig,
) -> SavedMappingVersion:
    """校验并追加一个映射版本；与同表头最新版本相同时幂等复用。"""
    batch, source_columns, header_signature = await _batch_header(db, file_version_id)
    canonical_mappings = tuple(sorted(mappings, key=lambda item: item.source_column))

    # 锁定租户这一稳定父行，覆盖首次创建和不同表头同时创建两种竞态。
    tenant = await db.get(Tenant, tenant_id, with_for_update=True)
    if tenant is None:  # pragma: no cover - 认证上下文保证租户存在
        raise RuntimeError("认证租户不存在")
    latest_tenant_version = int(
        await db.scalar(select(func.max(SchemaMappingVersion.version))) or 0
    )
    next_version = latest_tenant_version + 1
    candidate_id = uuid.uuid4()
    raw_candidate = MappingVersionConfig(
        id=candidate_id,
        version=next_version,
        header_signature=header_signature,
        mappings=canonical_mappings,
        availability_thresholds=availability_thresholds,
        currency_aliases=currency_aliases,
        inference_config=inference_config,
    )
    validate_mapping(raw_candidate, source_columns, uploaded_at=batch.uploaded_at)
    canonical_aliases = {
        unicodedata.normalize("NFKC", alias).strip().upper(): target.strip().upper()
        for alias, target in currency_aliases.items()
    }
    candidate = raw_candidate.model_copy(update={"currency_aliases": canonical_aliases})
    fingerprint = _config_fingerprint(candidate)

    latest_for_header = await db.scalar(
        select(SchemaMappingVersion)
        .where(SchemaMappingVersion.header_signature == header_signature)
        .order_by(SchemaMappingVersion.version.desc())
        .limit(1)
    )
    if latest_for_header is not None and latest_for_header.config_fingerprint == fingerprint:
        return SavedMappingVersion(
            version=await _mapping_view(
                db,
                latest_for_header,
                current_mapping_version_id=batch.mapping_version_id,
            ),
            reused_existing=True,
        )

    version = SchemaMappingVersion(
        id=candidate_id,
        tenant_id=tenant_id,
        header_signature=header_signature,
        version=next_version,
        config_fingerprint=fingerprint,
        availability_thresholds=_threshold_json(availability_thresholds),
        currency_aliases=canonical_aliases,
        inference_config=inference_config.model_dump(mode="json"),
        backfilled_legacy=False,
        created_by=actor_id,
    )
    db.add(version)
    await db.flush()
    db.add_all(
        SchemaMapping(
            tenant_id=tenant_id,
            mapping_version_id=version.id,
            source_column=entry.source_column,
            target_field=entry.target_field.value,
            version=next_version,
            confidence=None,
        )
        for entry in canonical_mappings
    )
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="schema_mapping_version.create",
        target_type="schema_mapping_version",
        target_id=str(version.id),
        payload={
            "mapping_version_id": str(version.id),
            "version": version.version,
            "header_signature": version.header_signature,
            "target_fields": sorted(entry.target_field.value for entry in canonical_mappings),
        },
    )
    return SavedMappingVersion(
        version=await _mapping_view(
            db,
            version,
            current_mapping_version_id=batch.mapping_version_id,
        ),
        reused_existing=False,
    )


async def list_parse_errors(
    db: AsyncSession,
    *,
    file_version_id: uuid.UUID,
    offset: int,
    limit: int,
) -> ParseErrorsPage:
    """按原始行号分页读取当前解析版本的错误行。"""
    batch = await _parsed_batch(db, file_version_id)
    mapping_version_id = cast("uuid.UUID", batch.mapping_version_id)
    filters = (
        ExpenseRow.file_version_id == file_version_id,
        ExpenseRow.parse_error_code.is_not(None),
    )
    total = int(await db.scalar(select(func.count()).select_from(ExpenseRow).where(*filters)) or 0)
    rows = tuple(
        (
            await db.scalars(
                select(ExpenseRow)
                .where(*filters)
                .order_by(ExpenseRow.row_no)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    items: list[ParseErrorItem] = []
    for row in rows:
        if (
            row.parse_error_code is None
            or row.parse_error is None
            or row.parse_error_detail is None
        ):
            raise RuntimeError("解析错误行缺少结构化错误信息")
        items.append(
            ParseErrorItem(
                row_no=row.row_no,
                raw_json=row.raw_json,
                parse_error_code=row.parse_error_code,
                parse_error=row.parse_error,
                parse_error_detail=RowErrorDetail.model_validate(row.parse_error_detail),
            )
        )
    return ParseErrorsPage(
        file_version_id=batch.id,
        mapping_version_id=mapping_version_id,
        total=total,
        offset=offset,
        limit=limit,
        items=tuple(items),
    )


async def get_field_availability(
    db: AsyncSession, *, file_version_id: uuid.UUID
) -> FieldAvailabilityView:
    """按统一字段固定顺序返回当前批次的 12 项可用性结果。"""
    batch = await _parsed_batch(db, file_version_id)
    mapping_version_id = cast("uuid.UUID", batch.mapping_version_id)
    rows = tuple(
        (
            await db.scalars(
                select(FieldAvailability).where(
                    FieldAvailability.file_version_id == file_version_id
                )
            )
        ).all()
    )
    by_field = {row.field_name: row for row in rows}
    if set(by_field) != {field.value for field in UNIFIED_FIELDS}:
        raise RuntimeError("字段可用性结果不完整")
    return FieldAvailabilityView(
        file_version_id=batch.id,
        mapping_version_id=mapping_version_id,
        items=tuple(
            FieldAvailabilityItem(
                field_name=field.value,
                status=by_field[field.value].status.value,
                evidence=AvailabilityEvidence.model_validate(by_field[field.value].evidence),
            )
            for field in UNIFIED_FIELDS
        ),
    )


async def record_parse_success(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    result: BatchParseResult,
) -> None:
    """为首次成功应用某映射版本的解析追加无 PII 审计。"""
    if result.reused_existing:
        return
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="batch.parse",
        target_type="file_version",
        target_id=str(result.file_version_id),
        payload={
            "file_version_id": str(result.file_version_id),
            "mapping_version_id": str(result.mapping_version_id),
            "mapping_version": result.mapping_version,
            "status": result.status,
            "total_rows": result.total_rows,
            "success_count": result.success_count,
            "error_count": result.error_count,
        },
    )


async def record_parse_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    mapping_version_id: uuid.UUID,
) -> None:
    """在独立短事务中记录系统失败，避免请求回滚吞掉审计。"""
    async with session_factory() as audit_db:
        bind_tenant(audit_db.sync_session, tenant_id)
        await write_audit(
            audit_db,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="batch.parse_failed",
            target_type="file_version",
            target_id=str(file_version_id),
            payload={
                "file_version_id": str(file_version_id),
                "mapping_version_id": str(mapping_version_id),
                "error_category": "internal_error",
            },
        )
        await audit_db.commit()


async def _batch_header(
    db: AsyncSession, file_version_id: uuid.UUID
) -> tuple[FileVersion, tuple[str, ...], str]:
    batch = await db.get(FileVersion, file_version_id)
    if batch is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    first_row = await db.scalar(
        select(ExpenseRow)
        .where(ExpenseRow.file_version_id == file_version_id)
        .order_by(ExpenseRow.row_no)
        .limit(1)
    )
    if first_row is None:
        raise RuntimeError("批次没有原始行")
    source_columns = tuple(sorted(first_row.raw_json))
    if not source_columns:
        raise RuntimeError("批次表头为空")
    return batch, source_columns, compute_header_signature(source_columns)


async def _parsed_batch(db: AsyncSession, file_version_id: uuid.UUID) -> FileVersion:
    batch = await db.get(FileVersion, file_version_id)
    if batch is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    if batch.parse_status not in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS} or (
        batch.mapping_version_id is None
    ):
        raise BatchParsingError(code="BATCH_NOT_PARSED", message="批次尚未成功解析")
    return batch


async def _mapping_view(
    db: AsyncSession,
    version: SchemaMappingVersion,
    *,
    current_mapping_version_id: uuid.UUID | None,
) -> MappingVersionView:
    entries = tuple(
        (
            await db.scalars(
                select(SchemaMapping)
                .where(SchemaMapping.mapping_version_id == version.id)
                .order_by(SchemaMapping.source_column)
            )
        ).all()
    )
    config = MappingVersionConfig.model_validate(
        {
            "id": version.id,
            "version": version.version,
            "header_signature": version.header_signature,
            "mappings": [
                {"source_column": item.source_column, "target_field": item.target_field}
                for item in entries
            ],
            "availability_thresholds": version.availability_thresholds,
            "currency_aliases": version.currency_aliases,
            "inference_config": version.inference_config,
        }
    )
    return MappingVersionView(
        id=version.id,
        version=version.version,
        created_at=version.created_at,
        created_by=version.created_by,
        is_current_for_batch=version.id == current_mapping_version_id,
        mappings=config.mappings,
        availability_thresholds=config.availability_thresholds,
        currency_aliases=config.currency_aliases,
        inference_config=config.inference_config,
    )


def _threshold_json(thresholds: AvailabilityThresholds) -> dict[str, str]:
    quantum = Decimal("0.0001")
    return {
        "available_min_non_null_rate": format(
            thresholds.available_min_non_null_rate.quantize(quantum), "f"
        ),
        "inferred_min_success_rate": format(
            thresholds.inferred_min_success_rate.quantize(quantum), "f"
        ),
    }


def _config_fingerprint(config: MappingVersionConfig) -> str:
    payload = {
        "mappings": [item.model_dump(mode="json") for item in config.mappings],
        "availability_thresholds": _threshold_json(config.availability_thresholds),
        "currency_aliases": config.currency_aliases,
        "inference_config": config.inference_config.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
