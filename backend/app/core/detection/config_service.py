"""Append-only tenant detection-profile service."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.detection.canonical import (
    canonical_sha256,
    profile_canonical_json,
    profile_fingerprint,
)
from app.core.detection.errors import DetectionError, DetectionInputError, DetectionInternalError
from app.core.detection.models import DetectionProfileDefinition
from app.core.detection.service_models import DetectionConfigPage, DetectionConfigResult
from app.core.errors import ExpenseGuardError
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.detection import DetectionConfig
from app.db.models.tenancy import AppUser

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
FaultHook = Callable[[str], None]


async def create_detection_config(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int,
    definition: DetectionProfileDefinition | dict[str, object],
    change_reason: str,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> DetectionConfigResult:
    """Create or replay one immutable profile version."""
    profile = _validate_profile(definition)
    reason = _validate_text(change_reason, code="DETECTION_CONFIG_INVALID", maximum=500)
    key_hash = _idempotency_key_hash(idempotency_key)
    canonical = profile_canonical_json(profile)
    fingerprint = profile_fingerprint(profile)
    reason_hash = _sha256(reason)
    request_fingerprint = canonical_sha256(
        {
            "change_reason_sha256": reason_hash,
            "definition": profile,
            "expected_current_version": expected_current_version,
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )
    try:
        async with db.begin_nested():
            return await _create_detection_config(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=expected_current_version,
                profile=profile,
                canonical=canonical,
                fingerprint=fingerprint,
                reason=reason,
                reason_hash=reason_hash,
                key_hash=key_hash,
                request_fingerprint=request_fingerprint,
                fault_hook=fault_hook,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise DetectionError(
                code="DETECTION_CONFLICT", message="该租户正在执行关联检测变更"
            ) from exc
        await _rollback_and_record_config_failure(
            db, session_factory, tenant_id=tenant_id, actor_id=actor_id, fingerprint=fingerprint
        )
        raise DetectionInternalError(code="DETECTION_CONFIG_FAILED") from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_config_failure(
            db, session_factory, tenant_id=tenant_id, actor_id=actor_id, fingerprint=fingerprint
        )
        raise DetectionInternalError(code="DETECTION_CONFIG_FAILED") from exc


async def _create_detection_config(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int,
    profile: DetectionProfileDefinition,
    canonical: str,
    fingerprint: str,
    reason: str,
    reason_hash: str,
    key_hash: str,
    request_fingerprint: str,
    fault_hook: FaultHook | None,
) -> DetectionConfigResult:
    await lock_tenant_nowait(db, tenant_id)
    existing = await db.scalar(
        select(DetectionConfig).where(
            DetectionConfig.tenant_id == tenant_id,
            DetectionConfig.idempotency_key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise DetectionError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他关联检测配置请求",
            )
        return _config_result(existing, reused_existing=True)
    current_version = int(
        await db.scalar(
            select(func.max(DetectionConfig.version)).where(DetectionConfig.tenant_id == tenant_id)
        )
        or 0
    )
    if expected_current_version < 0:
        raise DetectionInputError(
            code="DETECTION_CONFIG_INVALID", message="expected version 必须非负"
        )
    if current_version != expected_current_version:
        raise DetectionError(
            code="DETECTION_CONFIG_VERSION_CONFLICT",
            message="关联检测配置版本已变化，请刷新后重试",
        )
    config = DetectionConfig(
        tenant_id=tenant_id,
        version=current_version + 1,
        definition=profile.model_dump(mode="json"),
        definition_canonical=canonical,
        schema_version=profile.schema_version,
        algorithm_bundle_version=profile.algorithm_bundle_version,
        config_fingerprint=fingerprint,
        created_by=actor_id,
        change_reason=reason,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    try:
        async with db.begin_nested():
            db.add(config)
            await db.flush()
            _fault(fault_hook, "config_written")
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="detection.config_create",
                target_type="detection_config",
                target_id=str(config.id),
                payload={
                    "algorithm_bundle_version": config.algorithm_bundle_version,
                    "change_reason_sha256": reason_hash,
                    "config_fingerprint": config.config_fingerprint,
                    "version": config.version,
                },
            )
            _fault(fault_hook, "success_audit_written")
    except IntegrityError as exc:
        if _sqlstate(exc) != "23505":
            raise
        raise DetectionError(
            code="DETECTION_CONFLICT", message="关联检测配置并发创建冲突，请重试"
        ) from exc
    return _config_result(config, reused_existing=False)


async def get_latest_detection_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> DetectionConfig | None:
    config: DetectionConfig | None = await db.scalar(
        select(DetectionConfig)
        .where(DetectionConfig.tenant_id == tenant_id)
        .order_by(DetectionConfig.version.desc())
        .limit(1)
    )
    return config


async def require_latest_detection_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> DetectionConfig:
    config = await get_latest_detection_config(db, tenant_id=tenant_id)
    if config is None:
        raise DetectionError(
            code="DETECTION_CONFIG_REQUIRED",
            message="必须先创建关联检测配置",
        )
    return config


async def list_detection_configs(
    db: AsyncSession, *, tenant_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> DetectionConfigPage:
    if not 1 <= limit <= 200 or offset < 0:
        raise DetectionInputError(code="DETECTION_QUERY_INVALID", message="分页参数无效")
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(DetectionConfig)
            .where(DetectionConfig.tenant_id == tenant_id)
        )
        or 0
    )
    configs = tuple(
        (
            await db.scalars(
                select(DetectionConfig)
                .where(DetectionConfig.tenant_id == tenant_id)
                .order_by(DetectionConfig.version.desc(), DetectionConfig.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return DetectionConfigPage(
        items=tuple(_config_result(item, reused_existing=True) for item in configs),
        total=total,
        limit=limit,
        offset=offset,
    )


def profile_from_config(config: DetectionConfig) -> DetectionProfileDefinition:
    try:
        profile = DetectionProfileDefinition.model_validate(config.definition)
    except ValidationError as exc:
        raise DetectionInternalError(
            code="DETECTION_CONFIG_CORRUPT", message="关联检测配置快照无效"
        ) from exc
    if profile_canonical_json(profile) != config.definition_canonical or (
        profile_fingerprint(profile) != config.config_fingerprint
    ):
        raise DetectionInternalError(
            code="DETECTION_CONFIG_CORRUPT", message="关联检测配置快照身份不一致"
        )
    return profile


def _validate_profile(
    definition: DetectionProfileDefinition | dict[str, object],
) -> DetectionProfileDefinition:
    try:
        return DetectionProfileDefinition.model_validate(definition)
    except (ValidationError, TypeError, ValueError) as exc:
        raise DetectionInputError(
            code="DETECTION_CONFIG_INVALID", message="关联检测配置无效"
        ) from exc


def _validate_text(value: str, *, code: str, maximum: int) -> str:
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DetectionInputError(code=code, message="变更原因无效")
    return value


def _idempotency_key_hash(value: str) -> str:
    if not 8 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise DetectionInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须为 8 到 128 个可见 ASCII 字符",
        )
    return _sha256(value)


def _config_result(config: DetectionConfig, *, reused_existing: bool) -> DetectionConfigResult:
    return DetectionConfigResult(
        id=config.id,
        tenant_id=config.tenant_id,
        version=config.version,
        definition=config.definition,
        config_fingerprint=config.config_fingerprint,
        algorithm_bundle_version=config.algorithm_bundle_version,
        created_by=config.created_by,
        created_at=config.created_at,
        change_reason=config.change_reason,
        reused_existing=reused_existing,
    )


async def _rollback_and_record_config_failure(
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
            action="detection.config_failed",
            target_type="detection_config",
            payload={"config_fingerprint": fingerprint, "reason_code": "INTERNAL_ERROR"},
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
