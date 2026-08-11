"""CP-F8.4 API contract, RBAC, tenancy, idempotency, and query tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from inspect import getsource

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.routes import grading as grading_routes
from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.tenancy import AppUser, Role
from app.main import create_app
from tests.integration.test_grading_run_service import (
    SeededRun,
    seed_grading_run_sources,
)
from tests.unit.grading.helpers import config_payload

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
    session_factory: async_sessionmaker[AsyncSession], seed: SeededRun
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        auditor = await db.get(AppUser, seed.actor_id)
        assert auditor is not None
        auditor.password_hash = hash_password(PASSWORD)
        db.add_all(
            [
                AppUser(
                    tenant_id=seed.tenant_id,
                    username="configurator",
                    password_hash=hash_password(PASSWORD),
                    role=Role.CONFIGURATOR,
                    is_active=True,
                ),
                AppUser(
                    tenant_id=seed.tenant_id,
                    username="viewer",
                    password_hash=hash_password(PASSWORD),
                    role=Role.VIEWER,
                    is_active=True,
                ),
            ]
        )
        await db.commit()


async def _seed(session_factory: async_sessionmaker[AsyncSession], *, slug: str) -> SeededRun:
    result = await seed_grading_run_sources(session_factory, slug=slug)
    await _prepare_users(session_factory, result)
    return result


async def _login(client: AsyncClient, *, slug: str, username: str) -> None:
    client.cookies.clear()
    result = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": PASSWORD},
    )
    assert result.status_code == 200


def _run_payload(seed: SeededRun) -> dict[str, object]:
    return {
        "validation_run_id": str(seed.validation_run_id),
        "detection_run_id": str(seed.detection_run_id),
        "grading_config_id": str(seed.grading_config_id),
        "f7_manifest": [item.model_dump(mode="json") for item in seed.f7_requests],
    }


@pytest.mark.usefixtures("clean_db")
async def test_config_contract_permissions_validation_create_and_replay(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f8-api-config-{uuid.uuid4().hex[:8]}"
    seed = await _seed(session_factory, slug=slug)
    path = "/api/v1/grading-configs"

    anonymous = await client.get(path)
    assert anonymous.status_code == 401
    assert anonymous.headers["cache-control"] == "private, no-store"
    await _login(client, slug=slug, username="viewer")
    assert (await client.get(path)).status_code == 403

    await _login(client, slug=slug, username="auditor")
    page = await client.get(path, params={"limit": 1, "offset": 0})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "private, no-store"
    assert page.json()["total"] == 1
    assert (await client.get(f"{path}/current")).json()["id"] == str(seed.grading_config_id)
    denied = await client.post(
        path,
        headers={"Idempotency-Key": "f8-config-denied"},
        json={
            "expected_current_version": 1,
            "definition": config_payload(manual_review_cost_units=21),
            "change_reason": "permission gate",
        },
    )
    assert denied.status_code == 403

    await _login(client, slug=slug, username="configurator")
    invalid = await client.post(
        path,
        headers={"Idempotency-Key": "short"},
        json={"expected_current_version": True, "definition": {}, "change_reason": ""},
    )
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "private, no-store"
    assert invalid.json()["error"]["code"] == "GRADING_CONFIG_INVALID"
    strict_version = await client.post(
        path,
        headers={"Idempotency-Key": "f8-config-strict-version"},
        json={
            "expected_current_version": True,
            "definition": config_payload(manual_review_cost_units=21),
            "change_reason": "strict expected version",
        },
    )
    assert strict_version.status_code == 422
    assert strict_version.json()["error"]["code"] == "GRADING_CONFIG_INVALID"

    payload = {
        "expected_current_version": 1,
        "definition": config_payload(manual_review_cost_units=21),
        "change_reason": "F8 API version two",
    }
    created = await client.post(
        path,
        headers={"Idempotency-Key": "f8-config-create-0001"},
        json=payload,
    )
    assert created.status_code == 201
    assert created.json()["version"] == 2
    replay = await client.post(
        path,
        headers={"Idempotency-Key": "f8-config-create-0001"},
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["reused_existing"] is True


@pytest.mark.usefixtures("clean_db")
async def test_run_queries_filters_replay_and_cross_tenant_scope(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f8-api-run-{uuid.uuid4().hex[:8]}"
    seed = await _seed(session_factory, slug=slug)
    other_slug = f"f8-api-other-{uuid.uuid4().hex[:8]}"
    other = await _seed(session_factory, slug=other_slug)
    run_path = f"/api/v1/files/{seed.file_version_id}/grading-runs"
    payload = _run_payload(seed)

    await _login(client, slug=slug, username="viewer")
    assert (
        await client.post(
            run_path,
            headers={"Idempotency-Key": "f8-viewer-run-denied"},
            json=payload,
        )
    ).status_code == 403

    await _login(client, slug=slug, username="auditor")
    invalid = await client.post(
        run_path,
        headers={"Idempotency-Key": "f8-invalid-run-key"},
        json={**payload, "f7_manifest": [{"kind": "unknown"}]},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "GRADING_RUN_INVALID"
    created = await client.post(
        run_path,
        headers={"Idempotency-Key": "f8-run-create-0001"},
        json=payload,
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "private, no-store"
    run = created.json()
    replay = await client.post(
        run_path,
        headers={"Idempotency-Key": "f8-run-create-0001"},
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == run["id"]
    assert replay.json()["reused_existing"] is True

    current = await client.get(f"{run_path}/current")
    assert current.status_code == 200
    assert current.json()["run"]["id"] == run["id"]
    assert current.json()["config_stale"] is False
    assert (await client.get(f"/api/v1/grading-runs/{run['id']}")).status_code == 200
    page = await client.get(
        f"/api/v1/grading-runs/{run['id']}/items",
        params={
            "source_kind": "correlation",
            "severity_impact": 0,
            "severity_confidence": 0,
            "disposition": "manual_attention",
            "f7_outcome": "not_run",
            "sort_by": "default",
            "limit": 1,
            "offset": 0,
        },
    )
    assert page.status_code == 200
    assert page.json()["total"] >= 1
    item_id = page.json()["items"][0]["id"]
    assert (await client.get(f"/api/v1/grading-items/{item_id}")).status_code == 200
    rows = await client.get(
        f"/api/v1/grading-items/{item_id}/rows", params={"limit": 1, "offset": 0}
    )
    assert rows.status_code == 200
    assert rows.json()["total"] >= 2
    assert len(rows.json()["items"]) == 1
    bad_sort = await client.get(
        f"/api/v1/grading-runs/{run['id']}/items", params={"sort_by": "impact"}
    )
    assert bad_sort.status_code == 422

    await _login(client, slug=other_slug, username="auditor")
    assert (await client.get(f"{run_path}/current")).status_code == 404
    assert (await client.get(f"/api/v1/grading-runs/{run['id']}")).status_code == 404
    assert (await client.get(f"/api/v1/grading-items/{item_id}")).status_code == 404
    assert (
        await client.post(
            run_path,
            headers={"Idempotency-Key": "f8-cross-tenant-run"},
            json={**payload, "grading_config_id": str(other.grading_config_id)},
        )
    ).status_code == 404


def test_openapi_has_exact_f8_paths_discriminators_and_bounded_contracts(app: FastAPI) -> None:
    document = app.openapi()
    expected = {
        "/api/v1/grading-configs/current",
        "/api/v1/grading-configs",
        "/api/v1/files/{file_version_id}/grading-runs",
        "/api/v1/files/{file_version_id}/grading-runs/current",
        "/api/v1/grading-runs/{grading_run_id}",
        "/api/v1/grading-runs/{grading_run_id}/items",
        "/api/v1/grading-items/{grading_item_id}",
        "/api/v1/grading-items/{grading_item_id}/rows",
    }
    # Nine endpoint operations live on eight paths because config GET/POST share one path.
    assert expected <= set(document["paths"])
    operations = sum(
        method in document["paths"][path] for path in expected for method in ("get", "post")
    )
    assert operations == 9
    serialized = str(document)
    assert "discriminator" in serialized
    assert "F7RunManifestRequest" in serialized
    assert "F7NotRunManifestRequest" in serialized
    assert "InvestigationCitationResponse" in serialized
    assert "validation_run_id" in serialized
    route_source = getsource(grading_routes)
    assert "sqlalchemy" not in route_source
    assert "select(" not in route_source
