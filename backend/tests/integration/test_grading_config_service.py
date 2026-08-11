"""PostgreSQL integration coverage for the CP-F8.3 grading config service."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.grading.config_service import (
    config_from_record,
    create_grading_config,
    get_current_grading_config,
    list_grading_configs,
    require_current_grading_config,
)
from app.core.grading.errors import GradingError, GradingInternalError, GradingUnavailableError
from app.core.grading.models import GradingConfigV1
from app.core.grading.service_models import GradingConfigView
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.grading import GradingConfig
from app.db.models.tenancy import AppUser, Role, Tenant
from tests.unit.grading.helpers import config_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_actor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    active: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(slug=f"f8-config-{uuid.uuid4().hex}", name="F8 config tenant")
        session.add(tenant)
        await session.flush()
        bind_tenant(session.sync_session, tenant.id)
        actor = AppUser(
            tenant_id=tenant.id,
            username="configurator",
            password_hash="test-only",
            role=Role.CONFIGURATOR,
            is_active=active,
        )
        session.add(actor)
        await session.commit()
        return tenant.id, actor.id


def _changed_config() -> GradingConfigV1:
    return GradingConfigV1.model_validate(
        config_payload(
            impact_cost_units=[0, 20, 200, 2000],
            false_positive_cost_units=200,
        )
    )


async def _create(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_current_version: int = 0,
    definition: GradingConfigV1 | dict[str, object] | None = None,
    change_reason: str = "initial two-dimensional grading policy",
    idempotency_key: str = "grading-config-key-1",
) -> GradingConfigView:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await create_grading_config(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=expected_current_version,
            definition=definition or GradingConfigV1.model_validate(config_payload()),
            change_reason=change_reason,
            idempotency_key=idempotency_key,
        )
        await session.commit()
        return result


async def test_create_replay_conflict_duplicate_and_atomic_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = await _seed_actor(session_factory)
    definition = GradingConfigV1.model_validate(config_payload())
    created = await _create(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        definition=definition,
    )
    replayed = await _create(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        definition=definition,
    )
    assert replayed.id == created.id
    assert replayed.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(GradingError) as reused_error:
            await create_grading_config(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=0,
                definition=_changed_config(),
                change_reason="different request",
                idempotency_key="grading-config-key-1",
            )
        assert reused_error.value.code == "IDEMPOTENCY_KEY_REUSED"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(GradingError) as duplicate_error:
            await create_grading_config(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=1,
                definition=definition,
                change_reason="same definition under a new key",
                idempotency_key="grading-config-key-2",
            )
        assert duplicate_error.value.code == "GRADING_CONFIG_DUPLICATE"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(GradingConfig)) == 1
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "grading.config_create")
                )
            ).all()
        )
    assert len(audits) == 1
    assert "initial two-dimensional grading policy" not in str(audits[0].payload_json)


async def test_expected_version_actor_lock_and_failure_are_fail_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id = await _seed_actor(session_factory)
    definition = GradingConfigV1.model_validate(config_payload())
    await _create(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        definition=definition,
    )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(GradingError) as cas_error:
            await create_grading_config(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=0,
                definition=_changed_config(),
                change_reason="second version",
                idempotency_key="grading-config-key-2",
            )
        assert cas_error.value.code == "GRADING_CONFIG_VERSION_CONFLICT"
        await session.rollback()

    inactive_tenant, inactive_actor = await _seed_actor(session_factory, active=False)
    async with session_factory() as session:
        bind_tenant(session.sync_session, inactive_tenant)
        with pytest.raises(GradingError) as actor_error:
            await create_grading_config(
                session,
                session_factory,
                tenant_id=inactive_tenant,
                actor_id=inactive_actor,
                expected_current_version=0,
                definition=definition,
                change_reason="inactive actor request",
                idempotency_key="grading-config-key-inactive",
            )
        assert actor_error.value.code == "GRADING_ACTOR_INVALID"
        await session.rollback()

    async with session_factory() as holder:
        bind_tenant(holder.sync_session, tenant_id)
        await lock_tenant_nowait(holder, tenant_id)
        async with session_factory() as contender:
            bind_tenant(contender.sync_session, tenant_id)
            with pytest.raises(GradingError) as lock_error:
                await create_grading_config(
                    contender,
                    session_factory,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    expected_current_version=1,
                    definition=_changed_config(),
                    change_reason="lock contention",
                    idempotency_key="grading-config-key-lock",
                )
            assert lock_error.value.code == "GRADING_LOCK_CONFLICT"
            await contender.rollback()
        await holder.rollback()

    def fail_after_config(stage: str) -> None:
        if stage == "config_written":
            raise RuntimeError("injected config failure")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(GradingInternalError) as failure:
            await create_grading_config(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=1,
                definition=_changed_config(),
                change_reason="fault injected",
                idempotency_key="grading-config-key-fault",
                fault_hook=fail_after_config,
            )
        assert failure.value.code == "GRADING_CONFIG_CREATE_FAILED"

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(GradingConfig)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.config_failed")
            )
            == 1
        )


async def test_current_list_pagination_missing_and_corruption_validation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    empty_tenant, empty_actor = await _seed_actor(session_factory)
    async with session_factory() as session:
        bind_tenant(session.sync_session, empty_tenant)
        assert await get_current_grading_config(session, tenant_id=empty_tenant) is None
        with pytest.raises(GradingUnavailableError) as missing:
            await require_current_grading_config(session, tenant_id=empty_tenant)
        assert missing.value.code == "GRADING_CONFIG_MISSING"

    first = await _create(
        session_factory,
        tenant_id=empty_tenant,
        actor_id=empty_actor,
    )
    second = await _create(
        session_factory,
        tenant_id=empty_tenant,
        actor_id=empty_actor,
        expected_current_version=1,
        definition=_changed_config(),
        change_reason="second version",
        idempotency_key="grading-config-key-2",
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, empty_tenant)
        current = await get_current_grading_config(session, tenant_id=empty_tenant)
        assert current is not None and current.id == second.id and current.version == 2
        page = await list_grading_configs(session, tenant_id=empty_tenant, limit=1, offset=1)
    assert page.total == 2
    assert tuple(item.id for item in page.items) == (first.id,)

    corrupt = GradingConfig(
        tenant_id=empty_tenant,
        version=9,
        schema_version=1,
        definition_json=GradingConfigV1.model_validate(config_payload()).model_dump(mode="json"),
        canonical_definition="{}",
        config_fingerprint="0" * 64,
        algorithm_version="cost-matrix-v1",
        created_by=empty_actor,
        change_reason="corrupt fixture",
        idempotency_key_hash="1" * 64,
        request_fingerprint="2" * 64,
    )
    with pytest.raises(GradingInternalError) as corruption:
        config_from_record(corrupt)
    assert corruption.value.code == "GRADING_CONFIG_CORRUPT"
