"""CP-F5.3 review API contract, RBAC, tenancy, and idempotency tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.tenancy import AppUser, Role
from app.main import create_app
from tests.integration.test_review_services import _create_legacy_report, _create_report

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


async def _prepare_review_users(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    configurator_id: uuid.UUID,
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        configurator = await db.get(AppUser, configurator_id)
        assert configurator is not None
        configurator.password_hash = hash_password(PASSWORD)
        db.add_all(
            [
                AppUser(
                    tenant_id=tenant_id,
                    username="auditor",
                    password_hash=hash_password(PASSWORD),
                    role=Role.AUDITOR,
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


async def test_review_config_api_rbac_history_idempotency_and_cache(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f5-api-config-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, _batch_id, _report_id = await _create_report(
        session_factory,
        slug=slug,
    )
    await _prepare_review_users(
        session_factory,
        tenant_id=tenant_id,
        configurator_id=actor_id,
    )

    assert (await client.get("/api/review/sampling-config")).status_code == 401
    await _login(client, slug=slug, username="viewer")
    assert (await client.get("/api/review/sampling-config")).status_code == 403

    await _login(client, slug=slug, username="auditor")
    history = await client.get("/api/review/sampling-config")
    assert history.status_code == 200
    assert history.headers["cache-control"] == "private, no-store"
    assert history.json()["current"]["version"] == 1
    assert len(history.json()["history"]) == 1
    denied = await client.put(
        "/api/review/sampling-config",
        headers={"Idempotency-Key": "config-api-key-1"},
        json={
            "expected_current_version": 1,
            "rate_bps": 5000,
            "min_sample_size": 1,
            "max_sample_size": 10,
            "change_reason": "adjust sampling",
        },
    )
    assert denied.status_code == 403

    await _login(client, slug=slug, username="configurator")
    payload = {
        "expected_current_version": 1,
        "rate_bps": 5000,
        "min_sample_size": 1,
        "max_sample_size": 10,
        "change_reason": "adjust sampling",
    }
    created = await client.put(
        "/api/review/sampling-config",
        headers={"Idempotency-Key": "config-api-key-1"},
        json=payload,
    )
    assert created.status_code == 201
    replay = await client.put(
        "/api/review/sampling-config",
        headers={"Idempotency-Key": "config-api-key-1"},
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    assert replay.json()["reused_existing"] is True
    invalid = await client.put(
        "/api/review/sampling-config",
        headers={"Idempotency-Key": "config-api-key-2"},
        json=payload | {"max_sample_size": 0},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "SAMPLING_CONFIG_INVALID"
    missing_key = await client.put("/api/review/sampling-config", json=payload)
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"


async def test_review_queue_details_summary_tenant_scope_and_cors(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f5-api-read-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, batch_id, report_id = await _create_report(
        session_factory,
        slug=slug,
    )
    other_slug = f"f5-api-other-{uuid.uuid4().hex[:8]}"
    other_tenant, other_actor, _other_batch, other_report = await _create_report(
        session_factory,
        slug=other_slug,
    )
    await _prepare_review_users(
        session_factory,
        tenant_id=tenant_id,
        configurator_id=actor_id,
    )
    await _prepare_review_users(
        session_factory,
        tenant_id=other_tenant,
        configurator_id=other_actor,
    )

    await _login(client, slug=slug, username="auditor")
    findings = await client.get(
        "/api/reviews/queue",
        params={"kind": "finding", "report_id": str(report_id), "limit": 1},
    )
    assert findings.status_code == 200
    finding = findings.json()["items"][0]
    assert finding["kind"] == "finding"
    assert finding["sampling_status"] == "completed"
    assert "selection_rank" not in finding

    samples = await client.get(
        "/api/reviews/queue",
        params={
            "kind": "clearance_sample",
            "file_version_id": str(batch_id),
            "limit": 200,
        },
    )
    assert samples.status_code == 200
    sample = samples.json()["items"][0]
    assert sample["kind"] == "clearance_sample"
    assert "finding_id" not in sample

    finding_detail = await client.get(f"/api/reviews/findings/{finding['target_id']}")
    sample_detail = await client.get(f"/api/reviews/samples/{sample['target_id']}")
    assert finding_detail.status_code == sample_detail.status_code == 200
    assert finding_detail.headers["cache-control"] == "private, no-store"
    assert sample_detail.json()["ruleset_fingerprint"]

    summary = await client.get("/api/reviews/summary", params={"report_id": str(report_id)})
    assert summary.status_code == 200
    assert summary.json()["finding_review_coverage"]["completed"] == 0
    assert summary.json()["sample_review_coverage"] == {"completed": 0, "total": 1}
    assert (
        await client.get("/api/reviews/summary", params={"report_id": str(other_report)})
    ).status_code == 404
    assert (await client.get("/api/reports/not-a-uuid/review-plan")).json()["error"][
        "code"
    ] == "REQUEST_VALIDATION_ERROR"
    bad_page = await client.get("/api/reviews/queue", params={"limit": 201})
    assert bad_page.status_code == 422
    assert set(bad_page.json()) == {"error"}

    preflight = await client.options(
        "/api/reviews/queue",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Idempotency-Key",
        },
    )
    assert preflight.status_code == 200
    assert "Idempotency-Key" in preflight.headers["access-control-allow-headers"]

    await _login(client, slug=slug, username="viewer")
    assert (await client.get(f"/api/reviews/findings/{finding['target_id']}")).status_code == 403


async def test_review_decision_api_stable_errors_replay_and_audit_privacy(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f5-api-decision-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, _batch_id, report_id = await _create_report(
        session_factory,
        slug=slug,
    )
    await _prepare_review_users(
        session_factory,
        tenant_id=tenant_id,
        configurator_id=actor_id,
    )
    await _login(client, slug=slug, username="auditor")
    queue = (
        await client.get(
            "/api/reviews/queue",
            params={"report_id": str(report_id), "limit": 200},
        )
    ).json()["items"]
    finding_id = next(item["target_id"] for item in queue if item["kind"] == "finding")
    sample_id = next(item["target_id"] for item in queue if item["kind"] == "clearance_sample")

    missing_note = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        headers={"Idempotency-Key": "finding-api-key-1"},
        json={"kind": "finding", "decision": "false_positive", "note": None},
    )
    assert missing_note.status_code == 422
    assert missing_note.json()["error"]["code"] == "REVIEW_NOTE_REQUIRED"
    wrong_kind = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        headers={"Idempotency-Key": "finding-api-key-2"},
        json={"kind": "clearance_sample", "decision": "clearance_confirmed"},
    )
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()["error"]["code"] == "REVIEW_DECISION_INVALID"
    missing_key = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        json={"kind": "finding", "decision": "confirmed"},
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"

    private_note = "manual exception includes private merchant evidence"
    created = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        headers={"Idempotency-Key": "finding-api-key-3"},
        json={"kind": "finding", "decision": "false_positive", "note": private_note},
    )
    assert created.status_code == 201
    assert created.headers["cache-control"] == "private, no-store"
    replay = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        headers={"Idempotency-Key": "finding-api-key-3"},
        json={"kind": "finding", "decision": "false_positive", "note": private_note},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == created.json()["id"]
    duplicate = await client.post(
        f"/api/reviews/findings/{finding_id}/decision",
        headers={"Idempotency-Key": "finding-api-key-4"},
        json={"kind": "finding", "decision": "confirmed"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "REVIEW_ALREADY_COMPLETED"

    sample_note = "missing required original receipt"
    sampled = await client.post(
        f"/api/reviews/samples/{sample_id}/decision",
        headers={"Idempotency-Key": "sample-api-key-1"},
        json={"kind": "clearance_sample", "decision": "missed_issue", "note": sample_note},
    )
    assert sampled.status_code == 201

    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        audits = tuple(
            (
                await db.scalars(
                    select(AuditLog).where(
                        AuditLog.action.in_(["review.submit", "sampling.review_submit"])
                    )
                )
            ).all()
        )
    payloads = str([audit.payload_json for audit in audits])
    assert len(audits) == 2
    assert private_note not in payloads
    assert sample_note not in payloads
    assert "finding-api-key-3" not in payloads
    assert "sample-api-key-1" not in payloads
    assert "seed" not in payloads


async def test_legacy_plan_api_status_creation_replay_and_tenant_404(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f5-api-plan-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, report_id = await _create_legacy_report(
        session_factory,
        slug=slug,
    )
    await _prepare_review_users(
        session_factory,
        tenant_id=tenant_id,
        configurator_id=actor_id,
    )
    _other_tenant, _other_actor, other_report = await _create_legacy_report(
        session_factory,
        slug=f"f5-api-plan-other-{uuid.uuid4().hex[:8]}",
    )

    await _login(client, slug=slug, username="auditor")
    legacy = await client.get(f"/api/reports/{report_id}/review-plan")
    assert legacy.status_code == 200
    assert legacy.json()["status"] == "legacy_not_initialized"
    created = await client.post(
        f"/api/reports/{report_id}/review-plan",
        headers={"Idempotency-Key": "plan-api-key-1"},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "completed"
    replay = await client.post(
        f"/api/reports/{report_id}/review-plan",
        headers={"Idempotency-Key": "plan-api-key-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["plan"]["id"] == created.json()["plan"]["id"]
    assert (await client.get(f"/api/reports/{other_report}/review-plan")).status_code == 404


async def test_review_openapi_uses_discriminated_unions() -> None:
    schema = create_app().openapi()
    decision_schema = schema["paths"]["/api/reviews/findings/{report_item_id}/decision"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]
    assert decision_schema["discriminator"]["propertyName"] == "kind"
    assert len(decision_schema["oneOf"]) == 2
    queue_items = schema["components"]["schemas"]["ReviewQueuePage"]["properties"]["items"]["items"]
    assert queue_items["discriminator"]["propertyName"] == "kind"
    assert len(queue_items["oneOf"]) == 2
