"""CP-F4.4 policy/report API contract, RBAC, and tenant-boundary tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.policy import PolicyFamily
from app.db.models.tenancy import AppUser, Role
from app.main import create_app
from tests.integration.test_report_service import _seed_validated_batch

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    instance = create_app()
    instance.state.session_factory = session_factory
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def _login(client: AsyncClient, slug: str, username: str) -> None:
    client.cookies.clear()
    response = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": PASSWORD},
    )
    assert response.status_code == 200


async def _make_users_login_ready(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        actor = await db.get(AppUser, actor_id)
        assert actor is not None
        actor.password_hash = hash_password(PASSWORD)
        db.add(
            AppUser(
                tenant_id=tenant_id,
                username="viewer",
                password_hash=hash_password(PASSWORD),
                role=Role.VIEWER,
                is_active=True,
            )
        )
        await db.commit()


async def test_policy_family_api_permission_and_tenant_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_id, owner_actor, _ = await _seed_validated_batch(session_factory, slug="policy-api-owner")
    other_id, other_actor, _ = await _seed_validated_batch(session_factory, slug="policy-api-other")
    await _make_users_login_ready(session_factory, tenant_id=owner_id, actor_id=owner_actor)
    await _make_users_login_ready(session_factory, tenant_id=other_id, actor_id=other_actor)

    assert (await client.get("/api/policies/families")).status_code == 401
    await _login(client, "policy-api-owner", "viewer")
    assert (
        await client.post(
            "/api/policies/families",
            json={"stable_key": "travel", "display_name": "差旅制度"},
        )
    ).status_code == 403

    await _login(client, "policy-api-owner", "configurator")
    created = await client.post(
        "/api/policies/families",
        json={"stable_key": "travel", "display_name": "差旅制度"},
    )
    assert created.status_code == 201
    reused = await client.post(
        "/api/policies/families",
        json={"stable_key": "travel", "display_name": "差旅制度"},
    )
    assert reused.status_code == 200
    assert reused.json()["reused_existing"] is True

    await _login(client, "policy-api-other", "configurator")
    assert (await client.get("/api/policies/families")).json() == []


async def test_report_api_generation_replay_pagination_and_rbac(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug="report-api")
    await _make_users_login_ready(session_factory, tenant_id=tenant_id, actor_id=actor_id)
    await _login(client, "report-api", "viewer")
    denied = await client.post(
        f"/api/batches/{batch_id}/reports",
        headers={"Idempotency-Key": "report-api-key"},
    )
    assert denied.status_code == 403
    missing = await client.get(f"/api/batches/{batch_id}/report")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "REPORT_NOT_FOUND"

    await _login(client, "report-api", "configurator")
    generated = await client.post(
        f"/api/batches/{batch_id}/reports",
        headers={"Idempotency-Key": "report-api-key"},
    )
    assert generated.status_code == 201
    replay = await client.post(
        f"/api/batches/{batch_id}/reports",
        headers={"Idempotency-Key": "report-api-key"},
    )
    assert replay.status_code == 200
    report_id = generated.json()["report_run_id"]
    overview = await client.get(f"/api/batches/{batch_id}/report")
    assert overview.status_code == 200
    assert overview.json()["summary"]["report_run_id"] == report_id
    page = await client.get(
        f"/api/reports/{report_id}/items",
        params={"limit": 1, "offset": 0, "citation_status": "unavailable"},
    )
    assert page.status_code == 200
    assert page.json()["limit"] == 1
    errors = await client.get(f"/api/reports/{report_id}/parse-errors")
    assert errors.status_code == 200
    assert errors.json()["total"] == 1


async def test_policy_list_hides_cross_tenant_seeded_family(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _ = await _seed_validated_batch(session_factory, slug="policy-visible")
    other_id, other_actor, _ = await _seed_validated_batch(session_factory, slug="policy-hidden")
    await _make_users_login_ready(session_factory, tenant_id=tenant_id, actor_id=actor_id)
    await _make_users_login_ready(session_factory, tenant_id=other_id, actor_id=other_actor)
    async with session_factory() as db:
        bind_tenant(db.sync_session, other_id)
        db.add(
            PolicyFamily(
                tenant_id=other_id,
                stable_key="private-policy",
                display_name="其他租户制度",
                created_by=other_actor,
            )
        )
        await db.commit()
    await _login(client, "policy-visible", "configurator")
    assert (await client.get("/api/policies/families")).json() == []
