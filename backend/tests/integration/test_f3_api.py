"""CP-F3.4 确定性规则与校验查询 API 集成测试。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.batch import ExpenseRow, FileVersion, ParseStatus, RowResult
from app.db.models.config import RuleConfig, SchemaMappingVersion
from app.db.models.findings import Finding, RuleKind
from app.db.models.tenancy import AppUser, Role, Tenant
from app.db.models.validation import ValidationRun, ValidationRunStatus
from app.main import create_app

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


def _definition(kind: str = "invoice_duplicate") -> dict[str, object]:
    return {"schema_version": 1, "kind": kind, "enabled": True, "require_direct": False}


async def _seed(
    factory: async_sessionmaker[AsyncSession], slug: str
) -> tuple[uuid.UUID, uuid.UUID, dict[Role, uuid.UUID]]:
    async with factory() as db:
        tenant = Tenant(slug=slug, name=slug)
        db.add(tenant)
        await db.flush()
        bind_tenant(db.sync_session, tenant.id)
        users: dict[Role, uuid.UUID] = {}
        for role in Role:
            user = AppUser(
                tenant_id=tenant.id,
                username=role.value,
                password_hash=hash_password(PASSWORD),
                role=role,
                is_active=True,
            )
            db.add(user)
            await db.flush()
            users[role] = user.id
        batch = FileVersion(
            tenant_id=tenant.id,
            filename="batch.xlsx",
            content_hash=uuid.uuid4().hex.ljust(64, "0"),
            row_count=1,
            uploaded_by=users[Role.AUDITOR],
        )
        db.add(batch)
        await db.commit()
        return tenant.id, batch.id, users


async def _login(client: AsyncClient, slug: str, role: Role) -> None:
    client.cookies.clear()
    response = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": role.value, "password": PASSWORD},
    )
    assert response.status_code == 200


async def test_rules_rbac_create_reuse_history_and_validation_error(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(session_factory, "rules-api")
    payload = {
        "rule_id": "invoice.duplicate",
        "effective_from": "2026-01-01",
        "definition": _definition(),
    }
    assert (await client.get("/api/rules")).status_code == 401
    await _login(client, "rules-api", Role.VIEWER)
    assert (await client.put("/api/rules", json=payload)).status_code == 403
    await _login(client, "rules-api", Role.CONFIGURATOR)
    created = await client.put("/api/rules", json=payload)
    assert created.status_code == 201
    assert created.json()["reused_existing"] is False
    reused = await client.put("/api/rules", json=payload)
    assert reused.status_code == 200
    assert reused.json()["reused_existing"] is True
    invalid = {**payload, "definition": {**_definition(), "legacy": True}}
    rejected = await client.put("/api/rules", json=invalid)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "RULE_CONFIG_INVALID"
    listed = await client.get("/api/rules?latest_only=false")
    assert [(item["rule_id"], item["version"]) for item in listed.json()] == [
        ("invoice.duplicate", 1)
    ]


async def test_rules_cross_tenant_is_not_found(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _seed(session_factory, "rules-owner")
    await _seed(session_factory, "rules-other")
    await _login(client, "rules-owner", Role.CONFIGURATOR)
    await client.put(
        "/api/rules",
        json={
            "rule_id": "private.rule",
            "effective_from": "2026-01-01",
            "definition": _definition(),
        },
    )
    await _login(client, "rules-other", Role.CONFIGURATOR)
    response = await client.get("/api/rules?rule_id=private.rule")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RULE_NOT_FOUND"


async def test_validation_summary_findings_pagination_sort_and_tenant_boundary(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant_id, batch_id, users = await _seed(session_factory, "validation-api")
    _, other_batch_id, _ = await _seed(session_factory, "validation-other")
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        mapping = SchemaMappingVersion(
            tenant_id=tenant_id,
            header_signature="a" * 64,
            version=1,
            config_fingerprint="b" * 64,
            availability_thresholds={},
            currency_aliases={},
            inference_config={"rules": []},
        )
        db.add(mapping)
        await db.flush()
        batch = await db.get(FileVersion, batch_id)
        assert batch is not None
        batch.mapping_version_id = mapping.id
        batch.parse_status = ParseStatus.PARSED
        db.add(ExpenseRow(tenant_id=tenant_id, file_version_id=batch_id, row_no=2, raw_json={}))
        rule = RuleConfig(
            tenant_id=tenant_id,
            rule_id="z.rule",
            definition=_definition(),
            version=1,
            effective_from=utc_now().date(),
            is_active=True,
            config_fingerprint="c" * 64,
            created_by=users[Role.CONFIGURATOR],
            backfilled_legacy=False,
        )
        db.add(rule)
        await db.flush()
        run = ValidationRun(
            tenant_id=tenant_id,
            file_version_id=batch_id,
            mapping_version_id=mapping.id,
            ruleset_fingerprint="d" * 64,
            ruleset_manifest={},
            status=ValidationRunStatus.COMPLETED,
            total_row_count=1,
            evaluated_row_count=1,
            passed_count=0,
            flagged_count=1,
            manual_review_count=0,
            parse_failed_count=0,
            completed_at=utc_now(),
            triggered_by=users[Role.AUDITOR],
        )
        db.add(run)
        await db.flush()
        db.add(
            RowResult(
                tenant_id=tenant_id,
                file_version_id=batch_id,
                row_no=2,
                verdict="flagged",
                rule_version="d" * 64,
            )
        )
        for rule_id in ("z.rule", "a.rule"):
            db.add(
                Finding(
                    tenant_id=tenant_id,
                    file_version_id=batch_id,
                    row_no=2,
                    kind="invoice_duplicate",
                    severity_impact=0,
                    severity_confidence=0,
                    rule_id=rule_id,
                    rule_version="1",
                    reasoning="stable",
                    validation_run_id=run.id,
                    rule_kind=RuleKind.INVOICE_DUPLICATE,
                    rule_config_id=rule.id,
                    evidence_json={
                        "schema_version": 1,
                        "outcome": "flagged",
                        "rule_kind": "invoice_duplicate",
                        "reason_code": "invoice_duplicate",
                        "required_fields": ["invoice_no"],
                        "provenance": {
                            "invoice_no": {
                                "mode": "mapped",
                                "source_columns": ["发票号"],
                                "inference_rule_id": None,
                            }
                        },
                        "exemption_id": None,
                        "invoice_no": "INV-001",
                        "duplicate_of_file_version_id": str(batch_id),
                        "duplicate_of_root_file_version_id": str(batch_id),
                        "duplicate_of_row_no": 1,
                    },
                )
            )
        await db.commit()
    await _login(client, "validation-api", Role.VIEWER)
    summary = await client.get(f"/api/batches/{batch_id}/validation")
    assert summary.status_code == 200
    page = await client.get(f"/api/batches/{batch_id}/findings?page=1&page_size=1&verdict=flagged")
    assert page.status_code == 200
    assert page.json()["total"] == 2
    assert page.json()["items"][0]["rule_id"] == "a.rule"
    hidden = await client.get(f"/api/batches/{other_batch_id}/validation")
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "BATCH_NOT_FOUND"


async def test_missing_validation_and_revision_header_have_stable_errors(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _, batch_id, _ = await _seed(session_factory, "errors-api")
    await _login(client, "errors-api", Role.AUDITOR)
    missing = await client.get(f"/api/batches/{batch_id}/validation")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "VALIDATION_NOT_FOUND"
    invalid_key = await client.post(
        f"/api/batches/{batch_id}/revisions", json={"reason": "mapping_change"}
    )
    assert invalid_key.status_code == 422
    assert invalid_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"
