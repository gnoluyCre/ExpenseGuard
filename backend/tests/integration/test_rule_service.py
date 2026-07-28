"""规则版本追加保存服务的 PostgreSQL 集成测试。"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.core.validation.rule_service import RuleServiceError, save_rule_version
from app.db.models.audit import AuditLog
from app.db.models.config import RuleConfig
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


def _limit_definition(*, max_amount: str = "100") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "limit",
        "enabled": True,
        "require_direct": False,
        "exemptions": [],
        "thresholds": [
            {"expense_type": "差旅", "currency": "CNY", "max_amount": max_amount},
            {"expense_type": "餐饮", "currency": "CNY", "max_amount": "200"},
        ],
    }


async def _seed_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            username=f"{slug}-configurator",
            password_hash="test",
            role=Role.CONFIGURATOR,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return tenant.id, user.id


async def test_save_rule_version_appends_canonical_rule_and_pii_free_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-save")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition=_limit_definition(),
        )
        await session.commit()

        assert result.created is True
        assert result.rule_config.version == 1
        assert result.rule_config.backfilled_legacy is False
        assert result.rule_config.is_active is True
        assert result.rule_config.config_fingerprint is not None
        assert [item["expense_type"] for item in result.rule_config.definition["thresholds"]] == [
            "差旅",
            "餐饮",
        ]

        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "rule_config.create")
        )
        assert audit is not None
        assert audit.actor_id == user_id
        assert audit.target_id == str(result.rule_config.id)
        assert audit.payload_json == {
            "rule_config_id": str(result.rule_config.id),
            "rule_id": "expense.limit",
            "version": 1,
            "kind": "limit",
            "config_fingerprint": result.rule_config.config_fingerprint,
            "created_by": str(user_id),
        }
        assert "definition" not in audit.payload_json


async def test_latest_canonical_fingerprint_is_reused_without_new_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-reuse")
    reordered = _limit_definition()
    reordered["thresholds"] = list(reversed(reordered["thresholds"]))

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition=_limit_definition(),
        )
        await session.commit()
        second = await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition=reordered,
        )
        await session.commit()

        assert second.created is False
        assert second.rule_config.id == first.rule_config.id
        assert await session.scalar(select(func.count()).select_from(RuleConfig)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1


async def test_changed_definition_and_effective_date_append_versions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-append")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        results = []
        for effective_from, amount in (
            (date(2026, 1, 1), "100"),
            (date(2026, 1, 1), "101"),
            (date(2026, 2, 1), "101"),
        ):
            results.append(
                await save_rule_version(
                    session,
                    tenant_id=tenant_id,
                    created_by=user_id,
                    rule_id="expense.limit",
                    effective_from=effective_from,
                    definition=_limit_definition(max_amount=amount),
                )
            )
            await session.commit()

        assert [result.rule_config.version for result in results] == [1, 2, 3]
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 3


async def test_legacy_row_is_not_parsed_or_reused_but_sets_version_floor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-legacy")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        legacy = RuleConfig(
            tenant_id=tenant_id,
            rule_id="expense.limit",
            definition={"legacy_json_logic": {">": [{"var": "amount"}, 100]}},
            version=8,
            effective_from=None,
            is_active=True,
            config_fingerprint=None,
            created_by=None,
            backfilled_legacy=True,
        )
        session.add(legacy)
        await session.commit()

        created = await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition=_limit_definition(),
        )
        await session.commit()
        reused = await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition=_limit_definition(),
        )
        await session.commit()

        assert created.rule_config.version == 9
        assert reused.created is False
        assert reused.rule_config.id == created.rule_config.id
        await session.refresh(legacy)
        assert legacy.definition == {"legacy_json_logic": {">": [{"var": "amount"}, 100]}}
        assert legacy.config_fingerprint is None
        assert legacy.created_by is None
        assert legacy.backfilled_legacy is True


async def test_versions_and_audits_are_tenant_isolated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a, user_a = await _seed_tenant(session_factory, slug="rule-tenant-a")
    tenant_b, user_b = await _seed_tenant(session_factory, slug="rule-tenant-b")

    for tenant_id, user_id in ((tenant_a, user_a), (tenant_b, user_b)):
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            result = await save_rule_version(
                session,
                tenant_id=tenant_id,
                created_by=user_id,
                rule_id="expense.limit",
                effective_from=date(2026, 1, 1),
                definition=_limit_definition(),
            )
            await session.commit()
            assert result.rule_config.version == 1
            assert await session.scalar(select(func.count()).select_from(RuleConfig)) == 1
            assert await session.scalar(select(func.count()).select_from(AuditLog)) == 1

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_a)
        with pytest.raises(IntegrityError):
            await save_rule_version(
                session,
                tenant_id=tenant_a,
                created_by=user_b,
                rule_id="expense.title",
                effective_from=date(2026, 1, 1),
                definition={
                    "schema_version": 1,
                    "kind": "invoice_title",
                    "allowed_titles": ["甲公司"],
                },
            )
        await session.rollback()


async def test_tenant_lock_conflict_maps_to_stable_error_without_side_effects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-lock")

    async with session_factory() as holder, session_factory() as contender:
        bind_tenant(holder.sync_session, tenant_id)
        bind_tenant(contender.sync_session, tenant_id)
        await lock_tenant_nowait(holder, tenant_id=tenant_id)

        with pytest.raises(RuleServiceError) as exc_info:
            await save_rule_version(
                contender,
                tenant_id=tenant_id,
                created_by=user_id,
                rule_id="expense.limit",
                effective_from=date(2026, 1, 1),
                definition=_limit_definition(),
            )
        assert exc_info.value.code == "RULE_SAVE_IN_PROGRESS"
        assert exc_info.value.status_code == 409
        await contender.rollback()
        await holder.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(RuleConfig)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_invalid_definition_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="rule-invalid")
    invalid = _limit_definition()
    invalid["legacy_operator"] = {">": ["amount", 100]}

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ValidationError):
            await save_rule_version(
                session,
                tenant_id=tenant_id,
                created_by=user_id,
                rule_id="expense.limit",
                effective_from=date(2026, 1, 1),
                definition=invalid,
            )
        assert await session.scalar(select(func.count()).select_from(RuleConfig)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditLog)) == 0
