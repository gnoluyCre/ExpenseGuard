"""显式派生文件版本服务。

派生版本只复制重放所需的不可变证据或已冻结解析结果，不复制任何历史判定
副作用。普通上传仍只查询/创建 revision 1，本模块不会改变 F1 的入口语义。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.db.models.batch import (
    ExpenseRow,
    FieldAvailability,
    FileVersion,
    ParseStatus,
    RevisionReason,
)

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class RevisionError(ExpenseGuardError):
    """可稳定映射到传输层的派生版本领域错误。"""

    status_code = 409


class InvalidIdempotencyKeyError(RevisionError):
    """Idempotency-Key 不满足服务层固定边界。"""

    status_code = 422

    def __init__(self) -> None:
        super().__init__(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 长度必须为 8 到 128 个字符",
        )


class RevisionRequestReason(StrEnum):
    """服务层接受的派生原因。"""

    RULESET_CHANGE = "ruleset_change"
    MAPPING_CHANGE = "mapping_change"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RevisionRequest(_StrictModel):
    """经过服务层强校验的派生请求。"""

    reason: RevisionRequestReason
    idempotency_key: str = Field(min_length=8, max_length=128)


class RevisionResult(_StrictModel):
    """派生创建或幂等复用结果。"""

    file_version_id: uuid.UUID
    source_file_version_id: uuid.UUID
    root_file_version_id: uuid.UUID
    revision_no: int = Field(gt=1)
    reason: RevisionRequestReason
    parse_status: ParseStatus
    mapping_version_id: uuid.UUID | None
    reused_existing: bool


def idempotency_key_hash(idempotency_key: str) -> str:
    """对校验后的 Idempotency-Key 计算 SHA-256。"""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def revision_request_fingerprint(
    *, source_file_version_id: uuid.UUID, reason: RevisionRequestReason
) -> str:
    """计算包含来源与请求语义的 canonical 指纹。"""
    canonical = json.dumps(
        {
            "reason": reason.value,
            "schema_version": 1,
            "source_file_version_id": str(source_file_version_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def create_file_revision(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_file_version_id: uuid.UUID,
    reason: RevisionRequestReason | RevisionReason | str,
    idempotency_key: str,
) -> RevisionResult:
    """在租户锁内创建或幂等复用一个显式派生版本。

    调用方拥有外层事务。新 file_version、复制数据和审计使用同一 session，
    因而只能整批提交或整批回滚。
    """
    request = _validate_request(reason=reason, idempotency_key=idempotency_key)
    key_hash = idempotency_key_hash(request.idempotency_key)
    fingerprint = revision_request_fingerprint(
        source_file_version_id=source_file_version_id,
        reason=request.reason,
    )

    try:
        await lock_tenant_nowait(db, tenant_id)
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise RevisionError(
                code="BATCH_VALIDATION_IN_PROGRESS",
                message="该租户正在处理批次，请稍后重试",
            ) from exc
        raise

    source = await db.get(FileVersion, source_file_version_id)
    if source is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")

    existing = await _find_by_request_key(
        db,
        source_file_version_id=source.id,
        key_hash=key_hash,
    )
    if existing is not None:
        return _reuse_or_conflict(existing, fingerprint=fingerprint)

    if request.reason is RevisionRequestReason.RULESET_CHANGE:
        await _require_successfully_parsed_source(db, source)

    root_id = source.root_file_version_id or source.id
    next_revision_no = (
        int(
            await db.scalar(
                select(func.max(FileVersion.revision_no)).where(
                    FileVersion.content_hash == source.content_hash
                )
            )
            or 0
        )
        + 1
    )
    derived = FileVersion(
        tenant_id=tenant_id,
        filename=source.filename,
        content_hash=source.content_hash,
        row_count=source.row_count,
        uploaded_by=actor_id,
        mapping_version_id=(
            source.mapping_version_id
            if request.reason is RevisionRequestReason.RULESET_CHANGE
            else None
        ),
        parse_status=(
            source.parse_status
            if request.reason is RevisionRequestReason.RULESET_CHANGE
            else ParseStatus.UNPARSED
        ),
        parsed_at=(
            source.parsed_at if request.reason is RevisionRequestReason.RULESET_CHANGE else None
        ),
        revision_no=next_revision_no,
        source_file_version_id=source.id,
        root_file_version_id=root_id,
        revision_reason=RevisionReason(request.reason.value),
        revision_request_key_hash=key_hash,
        revision_request_fingerprint=fingerprint,
    )

    try:
        async with db.begin_nested():
            db.add(derived)
            await db.flush()
            await _copy_rows(db, source=source, derived=derived, reason=request.reason)
            if request.reason is RevisionRequestReason.RULESET_CHANGE:
                await _copy_field_availability(db, source=source, derived=derived)

            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="batch.revision_create",
                target_type="file_version",
                target_id=str(derived.id),
                payload={
                    "source_file_version_id": str(source.id),
                    "target_file_version_id": str(derived.id),
                    "reason": request.reason.value,
                    "revision_no": derived.revision_no,
                    "mapping_version_id": (
                        str(derived.mapping_version_id)
                        if derived.mapping_version_id is not None
                        else None
                    ),
                },
            )
    except IntegrityError as exc:
        concurrent = await _find_by_request_key(
            db,
            source_file_version_id=source.id,
            key_hash=key_hash,
        )
        if concurrent is not None:
            return _reuse_or_conflict(concurrent, fingerprint=fingerprint)
        raise RevisionError(
            code="BATCH_REVISION_CONFLICT",
            message="派生版本并发创建冲突，请重试",
        ) from exc

    return _to_result(derived, reused_existing=False)


def _validate_request(
    *, reason: RevisionRequestReason | RevisionReason | str, idempotency_key: str
) -> RevisionRequest:
    try:
        return RevisionRequest(reason=reason, idempotency_key=idempotency_key)
    except ValidationError as exc:
        locations = {str(part) for error in exc.errors() for part in error["loc"]}
        if "idempotency_key" in locations:
            raise InvalidIdempotencyKeyError from exc
        raise RevisionError(code="REVISION_REASON_INVALID", message="派生原因无效") from exc


async def _find_by_request_key(
    db: AsyncSession,
    *,
    source_file_version_id: uuid.UUID,
    key_hash: str,
) -> FileVersion | None:
    result: FileVersion | None = await db.scalar(
        select(FileVersion).where(
            FileVersion.source_file_version_id == source_file_version_id,
            FileVersion.revision_request_key_hash == key_hash,
        )
    )
    return result


def _reuse_or_conflict(existing: FileVersion, *, fingerprint: str) -> RevisionResult:
    if existing.revision_request_fingerprint != fingerprint:
        raise RevisionError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="该 Idempotency-Key 已绑定其他派生请求",
        )
    return _to_result(existing, reused_existing=True)


async def _require_successfully_parsed_source(db: AsyncSession, source: FileVersion) -> None:
    if source.parse_status not in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS}:
        raise RevisionError(code="BATCH_NOT_PARSED", message="批次尚未完成结构化解析")
    parsed_rows = tuple(
        (await db.scalars(select(ExpenseRow).where(ExpenseRow.file_version_id == source.id))).all()
    )
    if not any(
        row.normalized_json is not None and row.parse_error_code is None for row in parsed_rows
    ):
        raise RevisionError(code="BATCH_NOT_PARSED", message="批次没有成功解析的行")


async def _copy_rows(
    db: AsyncSession,
    *,
    source: FileVersion,
    derived: FileVersion,
    reason: RevisionRequestReason,
) -> None:
    rows = tuple(
        (
            await db.scalars(
                select(ExpenseRow)
                .where(ExpenseRow.file_version_id == source.id)
                .order_by(ExpenseRow.row_no)
            )
        ).all()
    )
    copy_parsed = reason is RevisionRequestReason.RULESET_CHANGE
    db.add_all(
        ExpenseRow(
            tenant_id=derived.tenant_id,
            file_version_id=derived.id,
            row_no=row.row_no,
            raw_json=dict(row.raw_json),
            normalized_json=(
                dict(row.normalized_json)
                if copy_parsed and row.normalized_json is not None
                else None
            ),
            parse_error=row.parse_error if copy_parsed else None,
            parse_error_code=row.parse_error_code if copy_parsed else None,
            parse_error_detail=(
                dict(row.parse_error_detail)
                if copy_parsed and row.parse_error_detail is not None
                else None
            ),
        )
        for row in rows
    )
    await db.flush()


async def _copy_field_availability(
    db: AsyncSession, *, source: FileVersion, derived: FileVersion
) -> None:
    availability = tuple(
        (
            await db.scalars(
                select(FieldAvailability)
                .where(FieldAvailability.file_version_id == source.id)
                .order_by(FieldAvailability.field_name)
            )
        ).all()
    )
    db.add_all(
        FieldAvailability(
            tenant_id=derived.tenant_id,
            file_version_id=derived.id,
            field_name=item.field_name,
            status=item.status,
            evidence=dict(item.evidence) if item.evidence is not None else None,
        )
        for item in availability
    )
    await db.flush()


def _to_result(file_version: FileVersion, *, reused_existing: bool) -> RevisionResult:
    source_id = file_version.source_file_version_id
    root_id = file_version.root_file_version_id
    reason = file_version.revision_reason
    if source_id is None or root_id is None or reason is None:
        raise RuntimeError("派生版本缺少 lineage 字段")
    return RevisionResult(
        file_version_id=file_version.id,
        source_file_version_id=source_id,
        root_file_version_id=root_id,
        revision_no=file_version.revision_no,
        reason=RevisionRequestReason(reason.value),
        parse_status=file_version.parse_status,
        mapping_version_id=file_version.mapping_version_id,
        reused_existing=reused_existing,
    )


def _sqlstate(exc: OperationalError) -> str | None:
    value: Any = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
