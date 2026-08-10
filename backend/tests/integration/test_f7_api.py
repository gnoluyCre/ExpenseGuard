"""CP-F7.4 API, RBAC, tenancy, cache, pagination, and replay tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.agent.checkpoint_reconciler import LangGraphCheckpointReconciler
from app.core.detection.models import DetectorKind
from app.core.detection.run_service import run_detection
from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.findings import CorrelationFinding
from app.db.models.tenancy import AppUser, Role
from app.main import create_app
from app.settings import Settings, get_settings
from tests.integration.test_detection_services import _create_profile, _seed_batch

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]
PASSWORD = "correct-horse-battery-staple"
PII_SECRET = "f7-api-redaction-secret-at-least-32-bytes"


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    settings = Settings(pii_tokenization_key=PII_SECRET)
    instance = create_app(settings)
    instance.dependency_overrides[get_settings] = lambda: settings
    instance.state.session_factory = session_factory
    instance.state.investigation_checkpoint_reconciler = LangGraphCheckpointReconciler(
        InMemorySaver()
    )
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def _seed_candidate(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, actor_id, file_id = await _seed_batch(session_factory, slug=slug)
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        actor = await db.get(AppUser, actor_id)
        assert actor is not None
        actor.password_hash = hash_password(PASSWORD)
        db.add_all(
            [
                AppUser(
                    tenant_id=tenant_id,
                    username="viewer",
                    password_hash=hash_password(PASSWORD),
                    role=Role.VIEWER,
                    is_active=True,
                ),
            ]
        )
        run = await run_detection(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key=f"f7-api-detection-{slug}",
        )
        finding = await db.scalar(
            select(CorrelationFinding).where(
                CorrelationFinding.detection_run_id == run.id,
                CorrelationFinding.detector == DetectorKind.SPLIT_INVOICE,
            )
        )
        assert finding is not None
        await db.commit()
        return tenant_id, actor_id, file_id, run.id, finding.id


async def _login(client: AsyncClient, *, slug: str, username: str) -> None:
    client.cookies.clear()
    result = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": PASSWORD},
    )
    assert result.status_code == 200


async def test_capability_unavailable_without_real_network_and_rbac(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f7-api-cap-{uuid.uuid4().hex[:8]}"
    _tenant, _actor, _file, run_id, finding_id = await _seed_candidate(session_factory, slug=slug)
    assert (await client.get("/api/v1/investigations/capability")).status_code == 401

    await _login(client, slug=slug, username="viewer")
    capability = await client.get("/api/v1/investigations/capability")
    assert capability.status_code == 200
    assert capability.headers["cache-control"] == "private, no-store"
    assert capability.json()["status"] == "unavailable"
    assert capability.json()["reason_code"] == "PROVIDER_DISABLED"
    denied = await client.post(
        f"/api/v1/detection-runs/{run_id}/findings/{finding_id}/investigations",
        headers={"Idempotency-Key": "viewer-f7-denied"},
    )
    assert denied.status_code == 403


async def test_unavailable_run_replay_history_detail_steps_and_tenant_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f7-api-run-{uuid.uuid4().hex[:8]}"
    _tenant, _actor, _file, run_id, finding_id = await _seed_candidate(session_factory, slug=slug)
    other_slug = f"f7-api-other-{uuid.uuid4().hex[:8]}"
    await _seed_candidate(session_factory, slug=other_slug)
    await _login(client, slug=slug, username="auditor")
    path = f"/api/v1/detection-runs/{run_id}/findings/{finding_id}/investigations"

    created = await client.post(path, headers={"Idempotency-Key": "f7-api-run-key"})
    assert created.status_code == 201
    assert created.headers["cache-control"] == "private, no-store"
    payload = created.json()
    assert payload["result"]["outcome"] == "unavailable"
    assert payload["result"]["reason_code"] == "PROVIDER_DISABLED"
    assert payload["steps"] == []
    assert "source_hmac" not in created.text
    investigation_id = payload["run"]["id"]

    replay = await client.post(path, headers={"Idempotency-Key": "f7-api-run-key"})
    assert replay.status_code == 200
    assert replay.json()["run"]["id"] == investigation_id
    assert replay.json()["reused_existing"] is True

    history = await client.get(path, params={"limit": 1, "offset": 0})
    assert history.status_code == 200
    assert history.headers["cache-control"] == "private, no-store"
    assert history.json()["total"] == 1
    detail = await client.get(f"/api/v1/investigations/{investigation_id}")
    assert detail.status_code == 200
    steps = await client.get(f"/api/v1/investigations/{investigation_id}/steps")
    assert steps.status_code == 200
    assert steps.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}

    await _login(client, slug=other_slug, username="auditor")
    assert (await client.get(f"/api/v1/investigations/{investigation_id}")).status_code == 404
    assert (await client.get(path)).json()["total"] == 0
    assert (
        await client.post(path, headers={"Idempotency-Key": "cross-tenant-f7"})
    ).status_code == 404
