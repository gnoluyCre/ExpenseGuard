"""Append-only configuration service for F8 two-dimensional grading."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError
from app.core.grading.canonical import canonical_json, config_fingerprint
from app.core.grading.errors import (
    GradingError,
    GradingInputError,
    GradingInternalError,
    GradingUnavailableError,
)
from app.core.grading.models import GradingConfigV1
from app.core.grading.service_models import GradingConfigPage, GradingConfigView
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.grading import GradingConfig
from app.db.models.tenancy import AppUser

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UNIQUE_VIOLATION_SQLSTATE = "23505"
FaultHook = Callable[[str], None]


async def create_grading_config(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int,
    definition: GradingConfigV1 | dict[str, object],
    change_reason: str,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> GradingConfigView:
    """Create or replay one immutable, tenant-scoped grading configuration."""
    config_definition = _validate_definition(definition)
    expected_version = _validate_expected_version(expected_current_version)
    reason = _validate_change_reason(change_reason)
    key_hash = _idempotency_key_hash(idempotency_key)
    canonical_definition = canonical_json(config_definition)
    fingerprint = config_fingerprint(config_definition)
    reason_hash = _sha256(reason)
    request_fingerprint = _sha256(
        canonical_json(
            {
                "change_reason_sha256": reason_hash,
                "config_fingerprint": fingerprint,
                "expected_current_version": expected_version,
                "schema_version": 1,
                "tenant_id": str(tenant_id),
            }
        )
    )

    try:
        async with db.begin_nested():
            return await _create_grading_config(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=expected_version,
                definition=config_definition,
                canonical_definition=canonical_definition,
                fingerprint=fingerprint,
                reason=reason,
                reason_hash=reason_hash,
                key_hash=key_hash,
                request_fingerprint=request_fingerprint,
                fault_hook=fault_hook,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise GradingError(
                code="GRADING_LOCK_CONFLICT",
                message="该租户正在执行二维分级配置变更",
            ) from exc
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            fingerprint=fingerprint,
        )
        raise GradingInternalError(code="GRADING_CONFIG_CREATE_FAILED") from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            fingerprint=fingerprint,
        )
        raise GradingInternalError(code="GRADING_CONFIG_CREATE_FAILED") from exc


async def _create_grading_config(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int,
    definition: GradingConfigV1,
    canonical_definition: str,
    fingerprint: str,
    reason: str,
    reason_hash: str,
    key_hash: str,
    request_fingerprint: str,
    fault_hook: FaultHook | None,
) -> GradingConfigView:
    await lock_tenant_nowait(db, tenant_id)
    await _require_active_actor(db, tenant_id=tenant_id, actor_id=actor_id)

    existing = await db.scalar(
        select(GradingConfig).where(
            GradingConfig.tenant_id == tenant_id,
            GradingConfig.idempotency_key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise GradingError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他二维分级配置请求",
            )
        return _config_view(existing, reused_existing=True)

    duplicate = await db.scalar(
        select(GradingConfig).where(
            GradingConfig.tenant_id == tenant_id,
            GradingConfig.config_fingerprint == fingerprint,
        )
    )
    if duplicate is not None:
        # Validate the immutable snapshot before returning a semantic duplicate.
        config_from_record(duplicate)
        raise GradingError(
            code="GRADING_CONFIG_DUPLICATE",
            message="相同二维分级配置已存在，请使用已有版本",
        )

    current_version = int(
        await db.scalar(
            select(func.max(GradingConfig.version)).where(GradingConfig.tenant_id == tenant_id)
        )
        or 0
    )
    if current_version != expected_current_version:
        raise GradingError(
            code="GRADING_CONFIG_VERSION_CONFLICT",
            message="二维分级配置版本已变化，请刷新后重试",
        )

    record = GradingConfig(
        tenant_id=tenant_id,
        version=current_version + 1,
        schema_version=definition.schema_version,
        definition_json=definition.model_dump(mode="json"),
        canonical_definition=canonical_definition,
        config_fingerprint=fingerprint,
        algorithm_version=definition.algorithm_version,
        created_by=actor_id,
        change_reason=reason,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    try:
        async with db.begin_nested():
            db.add(record)
            await db.flush()
            _fault(fault_hook, "config_written")
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="grading.config_create",
                target_type="grading_config",
                target_id=str(record.id),
                payload={
                    "algorithm_version": record.algorithm_version,
                    "change_reason_sha256": reason_hash,
                    "config_fingerprint": record.config_fingerprint,
                    "version": record.version,
                },
            )
            _fault(fault_hook, "success_audit_written")
    except IntegrityError as exc:
        if _sqlstate(exc) != UNIQUE_VIOLATION_SQLSTATE:
            raise
        raise GradingError(
            code="GRADING_CONFIG_CONFLICT",
            message="二维分级配置并发创建冲突，请重试",
        ) from exc
    return _config_view(record, reused_existing=False)


async def get_current_grading_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> GradingConfigView | None:
    record = await _get_current_record(db, tenant_id=tenant_id)
    return None if record is None else _config_view(record, reused_existing=True)


async def require_current_grading_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> GradingConfig:
    record = await _get_current_record(db, tenant_id=tenant_id)
    if record is None:
        raise GradingUnavailableError(
            code="GRADING_CONFIG_MISSING",
            message="必须先创建二维分级配置",
        )
    config_from_record(record)
    return record


async def list_grading_configs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> GradingConfigPage:
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 200 or offset < 0:
        raise GradingInputError(code="GRADING_QUERY_INVALID", message="分页参数无效")
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(GradingConfig)
            .where(GradingConfig.tenant_id == tenant_id)
        )
        or 0
    )
    records = tuple(
        (
            await db.scalars(
                select(GradingConfig)
                .where(GradingConfig.tenant_id == tenant_id)
                .order_by(GradingConfig.version.desc(), GradingConfig.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return GradingConfigPage(
        items=tuple(_config_view(record, reused_existing=True) for record in records),
        total=total,
        limit=limit,
        offset=offset,
    )


def config_from_record(record: GradingConfig) -> GradingConfigV1:
    try:
        definition = GradingConfigV1.model_validate(record.definition_json)
        canonical_definition = canonical_json(definition)
        fingerprint = config_fingerprint(definition)
    except (ValidationError, TypeError, ValueError) as exc:
        raise GradingInternalError(
            code="GRADING_CONFIG_CORRUPT",
            message="二维分级配置快照无效",
        ) from exc
    if (
        definition.schema_version != record.schema_version
        or definition.algorithm_version != record.algorithm_version
        or canonical_definition != record.canonical_definition
        or fingerprint != record.config_fingerprint
    ):
        raise GradingInternalError(
            code="GRADING_CONFIG_CORRUPT",
            message="二维分级配置快照身份不一致",
        )
    return definition


async def _get_current_record(db: AsyncSession, *, tenant_id: uuid.UUID) -> GradingConfig | None:
    record: GradingConfig | None = await db.scalar(
        select(GradingConfig)
        .where(GradingConfig.tenant_id == tenant_id)
        .order_by(GradingConfig.version.desc(), GradingConfig.id)
        .limit(1)
    )
    return record


async def _require_active_actor(
    db: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    actor = await db.scalar(
        select(AppUser).where(
            AppUser.id == actor_id,
            AppUser.tenant_id == tenant_id,
            AppUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise GradingError(code="GRADING_ACTOR_INVALID", message="操作用户无效")


def _config_view(record: GradingConfig, *, reused_existing: bool) -> GradingConfigView:
    definition = config_from_record(record)
    return GradingConfigView(
        id=record.id,
        tenant_id=record.tenant_id,
        version=record.version,
        schema_version=record.schema_version,
        definition=definition,
        config_fingerprint=record.config_fingerprint,
        algorithm_version=record.algorithm_version,
        created_by=record.created_by,
        change_reason=record.change_reason,
        created_at=record.created_at,
        reused_existing=reused_existing,
    )


def _validate_definition(
    definition: GradingConfigV1 | dict[str, object],
) -> GradingConfigV1:
    try:
        return GradingConfigV1.model_validate(definition)
    except (ValidationError, TypeError, ValueError) as exc:
        raise GradingInputError(
            code="GRADING_CONFIG_INVALID",
            message="二维分级配置无效",
        ) from exc


def _validate_expected_version(value: int) -> int:
    if type(value) is not int or value < 0:
        raise GradingInputError(
            code="GRADING_CONFIG_INVALID",
            message="expected version 必须为非负整数",
        )
    return value


def _validate_change_reason(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GradingInputError(code="GRADING_CONFIG_INVALID", message="变更原因无效")
    return value


def _idempotency_key_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise GradingInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须为 8 到 128 个可见 ASCII 字符",
        )
    return _sha256(value)


async def _rollback_and_record_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    fingerprint: str,
) -> None:
    await db.rollback()
    async with session_factory() as audit_db:
        bind_tenant(audit_db.sync_session, tenant_id)
        actor = await audit_db.scalar(
            select(AppUser).where(AppUser.id == actor_id, AppUser.tenant_id == tenant_id)
        )
        await write_audit(
            audit_db,
            tenant_id=tenant_id,
            actor_id=actor.id if actor is not None else None,
            action="grading.config_failed",
            target_type="grading_config",
            payload={
                "config_fingerprint": fingerprint,
                "reason_code": "INTERNAL_ERROR",
            },
        )
        await audit_db.commit()


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlstate(exc: IntegrityError | OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
