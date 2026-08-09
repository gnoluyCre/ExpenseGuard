"""CP-F6.4 detection API contract, RBAC, tenancy, and idempotency tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from inspect import getsource

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes import detection as detection_routes
from app.core.detection.models import DetectionProfileDefinition
from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.tenancy import AppUser, Role
from app.main import create_app
from tests.integration.test_detection_services import _seed_batch
from tests.unit.detection.helpers import profile_data

pytestmark = pytest.mark.integration
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


async def _prepare_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    auditor_id: uuid.UUID,
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        auditor = await db.get(AppUser, auditor_id)
        assert auditor is not None
        auditor.password_hash = hash_password(PASSWORD)
        db.add_all(
            [
                AppUser(
                    tenant_id=tenant_id,
                    username="configurator",
                    password_hash=hash_password(PASSWORD),
                    role=Role.CONFIGURATOR,
                    is_active=True,
                ),
                AppUser(
                    tenant_id=tenant_id,
                    username="viewer",
                    password_hash=hash_password(PASSWORD),
                    role=Role.VIEWER,
                    is_active=True,
                ),
            ]
        )
        await db.commit()


async def _login(client: AsyncClient, *, slug: str, username: str) -> None:
    client.cookies.clear()
    response = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": PASSWORD},
    )
    assert response.status_code == 200


def _config_payload() -> dict[str, object]:
    definition = DetectionProfileDefinition.model_validate(profile_data())
    return {
        "expected_current_version": 0,
        "definition": definition.model_dump(mode="json"),
        "change_reason": "initial deterministic profile",
    }


async def _create_config(
    client: AsyncClient, *, key: str = "f6-config-api-key-1"
) -> dict[str, object]:
    response = await client.put(
        "/api/detection/configs",
        headers={"Idempotency-Key": key},
        json=_config_payload(),
    )
    assert response.status_code == 201
    return response.json()


async def _run_batch(client: AsyncClient, batch_id: uuid.UUID, *, key: str) -> dict[str, object]:
    response = await client.post(
        f"/api/batches/{batch_id}/detect",
        headers={"Idempotency-Key": key},
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.usefixtures("clean_db")
async def test_detection_config_rbac_validation_history_replay_and_conflict(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f6-api-config-{uuid.uuid4().hex[:8]}"
    tenant_id, auditor_id, _batch_id = await _seed_batch(session_factory, slug=slug)
    await _prepare_users(session_factory, tenant_id=tenant_id, auditor_id=auditor_id)

    assert (await client.get("/api/detection/configs")).status_code == 401
    await _login(client, slug=slug, username="viewer")
    assert (await client.get("/api/detection/configs")).status_code == 403

    await _login(client, slug=slug, username="auditor")
    empty = await client.get("/api/detection/configs")
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "private, no-store"
    assert empty.json() == {"current": None, "history": [], "total": 0, "limit": 50, "offset": 0}
    assert (
        await client.put(
            "/api/detection/configs",
            headers={"Idempotency-Key": "f6-config-denied"},
            json=_config_payload(),
        )
    ).status_code == 403

    await _login(client, slug=slug, username="configurator")
    invalid_payload = _config_payload()
    invalid_definition = invalid_payload["definition"]
    assert isinstance(invalid_definition, dict)
    detectors = invalid_definition["detectors"]
    assert isinstance(detectors, list)
    assert isinstance(detectors[0], dict)
    detectors[0]["type"] = "unknown_detector"
    invalid = await client.put(
        "/api/detection/configs",
        headers={"Idempotency-Key": "f6-config-invalid"},
        json=invalid_payload,
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "DETECTION_CONFIG_INVALID"

    created = await _create_config(client)
    assert created["definition"]["detectors"][0]["type"] == "split_invoice"
    replay = await client.put(
        "/api/detection/configs",
        headers={"Idempotency-Key": "f6-config-api-key-1"},
        json=_config_payload(),
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created["id"]
    assert replay.json()["reused_existing"] is True

    changed = _config_payload()
    changed["change_reason"] = "different request"
    conflict = await client.put(
        "/api/detection/configs",
        headers={"Idempotency-Key": "f6-config-api-key-1"},
        json=changed,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    history = await client.get("/api/detection/configs", params={"limit": 1, "offset": 0})
    assert history.json()["total"] == 1
    assert history.json()["current"]["id"] == created["id"]
    assert len(history.json()["history"]) == 1


@pytest.mark.usefixtures("clean_db")
async def test_detection_run_findings_capability_filter_cache_and_tenant_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f6-api-run-{uuid.uuid4().hex[:8]}"
    tenant_id, auditor_id, batch_id = await _seed_batch(session_factory, slug=slug)
    await _prepare_users(session_factory, tenant_id=tenant_id, auditor_id=auditor_id)
    other_slug = f"f6-api-other-{uuid.uuid4().hex[:8]}"
    other_tenant, other_auditor, other_batch = await _seed_batch(session_factory, slug=other_slug)
    await _prepare_users(session_factory, tenant_id=other_tenant, auditor_id=other_auditor)

    await _login(client, slug=slug, username="configurator")
    await _create_config(client)
    await _login(client, slug=slug, username="auditor")
    run = await _run_batch(client, batch_id, key="f6-run-api-key-1")
    run_id = run["id"]
    replay = await client.post(
        f"/api/batches/{batch_id}/detect",
        headers={"Idempotency-Key": "f6-run-api-key-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == run_id
    assert replay.json()["reused_existing"] is True

    batch = await client.get(f"/api/batches/{batch_id}/detection")
    assert batch.status_code == 200
    assert batch.headers["cache-control"] == "private, no-store"
    assert len(batch.json()["capabilities"]) == 4
    assert "severity_impact" not in str(batch.json())
    assert (await client.get(f"/api/detection-runs/{run_id}")).status_code == 200

    findings = await client.get(
        f"/api/detection-runs/{run_id}/findings",
        params={"limit": 1, "capability_status": "enabled", "sort_by": "default"},
    )
    assert findings.status_code == 200
    assert findings.json()["total"] >= 1
    finding = findings.json()["items"][0]
    assert "evidence" not in finding
    detail = await client.get(
        f"/api/correlation-findings/{finding['id']}",
        params={"row_limit": 1, "row_offset": 0},
    )
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.json()["evidence"]["detector"] == detail.json()["detector"]
    assert detail.json()["completed"] == 1
    assert detail.json()["total"] >= 2
    assert "severity_confidence" not in detail.json()

    unavailable = await client.get(
        f"/api/detection-runs/{run_id}/findings",
        params={"capability_status": "unavailable"},
    )
    assert unavailable.status_code == 200
    assert unavailable.json()["total"] == 0
    await _login(client, slug=other_slug, username="configurator")
    await _create_config(client, key="f6-other-config-key")
    await _login(client, slug=other_slug, username="auditor")
    other_run = await _run_batch(client, other_batch, key="f6-other-run-key")
    other_findings = (await client.get(f"/api/detection-runs/{other_run['id']}/findings")).json()[
        "items"
    ]
    assert other_findings

    await _login(client, slug=slug, username="auditor")
    assert (await client.get(f"/api/batches/{other_batch}/detection")).status_code == 404
    assert (await client.get(f"/api/detection-runs/{other_run['id']}")).status_code == 404
    assert (
        await client.get(f"/api/correlation-findings/{other_findings[0]['id']}")
    ).status_code == 404

    await _login(client, slug=slug, username="viewer")
    assert (await client.get(f"/api/batches/{batch_id}/detection")).status_code == 200
    assert (
        await client.post(
            f"/api/batches/{batch_id}/detect",
            headers={"Idempotency-Key": "viewer-cannot-detect"},
        )
    ).status_code == 403


def test_detection_openapi_has_discriminators_and_no_severity(app: FastAPI) -> None:
    document = app.openapi()
    assert {
        "/api/detection/configs",
        "/api/batches/{file_version_id}/detect",
        "/api/batches/{file_version_id}/detection",
        "/api/detection-runs/{run_id}",
        "/api/detection-runs/{run_id}/findings",
        "/api/correlation-findings/{finding_id}",
    } <= set(document["paths"])
    serialized = str(document)
    assert "discriminator" in serialized
    assert "split_invoice" in serialized
    assert "spatiotemporal_tier0" in serialized
    assert "severity_impact" not in serialized
    assert "severity_confidence" not in serialized
    route_source = getsource(detection_routes)
    assert "sqlalchemy" not in route_source
    assert "select(" not in route_source
