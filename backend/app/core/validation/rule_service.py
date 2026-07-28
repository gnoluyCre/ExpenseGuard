"""确定性规则配置的追加版本保存服务。"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.rules import rule_config_fingerprint, validate_rule_definition
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.db.models.config import RuleConfig

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class RuleServiceError(ExpenseGuardError):
    """可稳定映射到 API 的规则保存领域错误。"""

    status_code = 409


class SavedRuleVersion(BaseModel):
    """一次规则保存的结果。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    rule_config: RuleConfig
    created: bool


async def list_rule_versions(
    db: AsyncSession,
    *,
    rule_id: str | None = None,
    latest_only: bool = True,
) -> tuple[RuleConfig, ...]:
    """稳定列出当前租户的 F3 强类型规则版本。"""
    statement = (
        select(RuleConfig)
        .where(RuleConfig.backfilled_legacy.is_(False))
        .order_by(RuleConfig.rule_id, RuleConfig.version, RuleConfig.id)
    )
    if rule_id is not None:
        statement = statement.where(RuleConfig.rule_id == rule_id)
    versions = tuple((await db.scalars(statement)).all())
    if rule_id is not None and not versions:
        raise NotFoundError(code="RULE_NOT_FOUND", message="规则不存在")
    if not latest_only:
        return versions
    latest: dict[str, RuleConfig] = {}
    for version in versions:
        latest[version.rule_id] = version
    return tuple(latest[key] for key in sorted(latest))


async def save_rule_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    rule_id: str,
    effective_from: date,
    definition: object,
) -> SavedRuleVersion:
    """校验并追加一个不可变规则版本，或幂等复用最新版本。

    调用方拥有事务边界；本函数不会 commit 或 rollback。租户锁覆盖最新
    版本读取、版本号分配、规则写入和审计追加，避免并发分配相同版本号。
    """
    validated = validate_rule_definition(definition)
    fingerprint = rule_config_fingerprint(
        rule_id=rule_id,
        effective_from=effective_from,
        definition=validated,
    )

    try:
        await lock_tenant_nowait(db, tenant_id=tenant_id)
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise RuleServiceError(
                code="RULE_SAVE_IN_PROGRESS",
                message="该租户的规则配置正在变更，请稍后重试",
            ) from exc
        raise

    latest = await db.scalar(
        select(RuleConfig)
        .where(RuleConfig.rule_id == rule_id)
        .order_by(RuleConfig.version.desc(), RuleConfig.id.desc())
        .limit(1)
    )
    if (
        latest is not None
        and not latest.backfilled_legacy
        and latest.config_fingerprint == fingerprint
    ):
        return SavedRuleVersion(rule_config=latest, created=False)

    version = 1 if latest is None else latest.version + 1
    saved = RuleConfig(
        tenant_id=tenant_id,
        rule_id=rule_id,
        definition=validated.model_dump(mode="json"),
        version=version,
        effective_from=effective_from,
        is_active=True,
        config_fingerprint=fingerprint,
        created_by=created_by,
        backfilled_legacy=False,
    )
    db.add(saved)
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="rule_config.create",
        actor_id=created_by,
        target_type="rule_config",
        target_id=str(saved.id),
        payload={
            "rule_config_id": str(saved.id),
            "rule_id": rule_id,
            "version": version,
            "kind": validated.kind.value,
            "config_fingerprint": fingerprint,
            "created_by": str(created_by),
        },
    )
    return SavedRuleVersion(rule_config=saved, created=True)


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
