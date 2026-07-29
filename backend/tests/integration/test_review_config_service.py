"""F5 append-only sampling-config service tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.reviews.config_service import create_sampling_config
from app.core.reviews.errors import ReviewError, ReviewInputError
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.findings import ReviewSamplingConfig
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_actor(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        session.add(tenant)
        await session.flush()
        bind_tenant(session.sync_session, tenant.id)
        actor = AppUser(
            tenant_id=tenant.id,
            username="configurator",
            password_hash="test-only",
            role=Role.CONFIGURATOR,
            is_active=True,
        )
        session.add(actor)
        await session.commit()
        return tenant.id, actor.id


async def test_config_versions_idempotency_and_audit_are_atomic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = await _seed_actor(
        session_factory, slug=f"review-config-{uuid.uuid4().hex[:8]}"
    )
    arguments = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "expected_current_version": 0,
        "rate_bps": 500,
        "min_sample_size": 2,
        "max_sample_size": 20,
        "change_reason": "initial sampling policy",
        "idempotency_key": "review-config-key-1",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await create_sampling_config(session, **arguments)
        await session.commit()
    assert created.version == 1
    assert created.reused_existing is False

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replayed = await create_sampling_config(session, **arguments)
        await session.commit()
    assert replayed.id == created.id
    assert replayed.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingConfig)) == 1
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "sampling.config_create")
                )
            ).all()
        )
    assert len(audits) == 1
    assert audits[0].payload_json is not None
    assert "initial sampling policy" not in str(audits[0].payload_json)
    assert "change_reason_sha256" in audits[0].payload_json


async def test_config_rejects_key_reuse_stale_version_and_invalid_input(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = await _seed_actor(
        session_factory, slug=f"review-config-conflict-{uuid.uuid4().hex[:8]}"
    )
    base = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "expected_current_version": 0,
        "rate_bps": 1_000,
        "min_sample_size": 1,
        "max_sample_size": 10,
        "change_reason": "initial",
        "idempotency_key": "review-config-conflict-key",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await create_sampling_config(session, **base)
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as reused:
            await create_sampling_config(session, **(base | {"rate_bps": 1_001}))
        assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as stale:
            await create_sampling_config(
                session,
                **(base | {"idempotency_key": "review-config-new-key"}),
            )
        assert stale.value.code == "SAMPLING_CONFIG_VERSION_CONFLICT"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewInputError) as invalid:
            await create_sampling_config(
                session,
                **(
                    base
                    | {
                        "idempotency_key": "valid-new-key",
                        "max_sample_size": 0,
                    }
                ),
            )
        assert invalid.value.code == "SAMPLING_CONFIG_INVALID"


async def test_config_rolls_back_record_and_success_audit_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = await _seed_actor(
        session_factory, slug=f"review-config-rollback-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await create_sampling_config(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=0,
            rate_bps=100,
            min_sample_size=1,
            max_sample_size=5,
            change_reason="rollback",
            idempotency_key="review-config-rollback-key",
        )
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingConfig)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "sampling.config_create")
            )
            == 0
        )
