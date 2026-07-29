"""Append-only tenant sampling-config service."""

from __future__ import annotations

import hashlib
import uuid

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policies.canonical import canonical_sha256
from app.core.reviews.errors import ReviewError, ReviewInputError
from app.core.reviews.models import (
    SamplingConfigCreateCommand,
    SamplingConfigParameters,
    SamplingConfigResult,
)
from app.core.reviews.sampling import canonical_sampling_config_fingerprint
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.db.models.findings import ReviewSamplingConfig

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


async def create_sampling_config(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int,
    rate_bps: int,
    min_sample_size: int,
    max_sample_size: int,
    change_reason: str,
    idempotency_key: str,
) -> SamplingConfigResult:
    """Create or replay one immutable config version while holding the tenant lock."""
    command = _validate_command(
        expected_current_version=expected_current_version,
        rate_bps=rate_bps,
        min_sample_size=min_sample_size,
        max_sample_size=max_sample_size,
        change_reason=change_reason,
        idempotency_key=idempotency_key,
    )
    key_hash = _sha256(command.idempotency_key)
    parameters = SamplingConfigParameters.model_validate(
        command.model_dump(
            include={
                "algorithm_version",
                "max_sample_size",
                "min_sample_size",
                "rate_bps",
            }
        )
    )
    config_fingerprint = canonical_sampling_config_fingerprint(parameters)
    reason_hash = _sha256(command.change_reason)
    request_fingerprint = canonical_sha256(
        {
            "change_reason_sha256": reason_hash,
            "expected_current_version": command.expected_current_version,
            "parameters": command.model_dump(
                include={
                    "algorithm_version",
                    "max_sample_size",
                    "min_sample_size",
                    "rate_bps",
                }
            ),
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )

    try:
        await lock_tenant_nowait(db, tenant_id)
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise ReviewError(code="REVIEW_CONFLICT", message="该租户正在执行复核变更") from exc
        raise

    existing = await db.scalar(
        select(ReviewSamplingConfig).where(
            ReviewSamplingConfig.tenant_id == tenant_id,
            ReviewSamplingConfig.idempotency_key_hash == key_hash,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise ReviewError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他抽样配置请求",
            )
        return _to_result(existing, reused_existing=True)

    current_version = int(
        await db.scalar(
            select(func.max(ReviewSamplingConfig.version)).where(
                ReviewSamplingConfig.tenant_id == tenant_id
            )
        )
        or 0
    )
    if current_version != command.expected_current_version:
        raise ReviewError(
            code="SAMPLING_CONFIG_VERSION_CONFLICT",
            message="抽样配置版本已变化，请刷新后重试",
        )

    config = ReviewSamplingConfig(
        tenant_id=tenant_id,
        version=current_version + 1,
        rate_bps=command.rate_bps,
        min_sample_size=command.min_sample_size,
        max_sample_size=command.max_sample_size,
        algorithm_version=command.algorithm_version,
        config_fingerprint=config_fingerprint,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        created_by=actor_id,
        change_reason=command.change_reason,
    )
    db.add(config)
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="sampling.config_create",
        target_type="review_sampling_config",
        target_id=str(config.id),
        payload={
            "algorithm_version": config.algorithm_version,
            "change_reason_sha256": reason_hash,
            "config_fingerprint": config.config_fingerprint,
            "max_sample_size": config.max_sample_size,
            "min_sample_size": config.min_sample_size,
            "rate_bps": config.rate_bps,
            "version": config.version,
        },
    )
    await db.flush()
    return _to_result(config, reused_existing=False)


async def get_latest_sampling_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> ReviewSamplingConfig | None:
    config: ReviewSamplingConfig | None = await db.scalar(
        select(ReviewSamplingConfig)
        .where(ReviewSamplingConfig.tenant_id == tenant_id)
        .order_by(ReviewSamplingConfig.version.desc())
        .limit(1)
    )
    return config


async def require_latest_sampling_config(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> ReviewSamplingConfig:
    config = await get_latest_sampling_config(db, tenant_id=tenant_id)
    if config is None:
        raise ReviewError(
            code="SAMPLING_CONFIG_REQUIRED",
            message="必须先创建抽样配置才能生成报告",
        )
    return config


def _validate_command(**values: object) -> SamplingConfigCreateCommand:
    try:
        return SamplingConfigCreateCommand.model_validate(values)
    except ValidationError as exc:
        raise ReviewInputError(code="SAMPLING_CONFIG_INVALID", message="抽样配置无效") from exc


def _to_result(config: ReviewSamplingConfig, *, reused_existing: bool) -> SamplingConfigResult:
    return SamplingConfigResult(
        id=config.id,
        tenant_id=config.tenant_id,
        version=config.version,
        rate_bps=config.rate_bps,
        min_sample_size=config.min_sample_size,
        max_sample_size=config.max_sample_size,
        algorithm_version=config.algorithm_version,
        config_fingerprint=config.config_fingerprint,
        created_by=config.created_by,
        created_at=config.created_at,
        change_reason=config.change_reason,
        reused_existing=reused_existing,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
