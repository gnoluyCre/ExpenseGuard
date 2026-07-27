"""CP-F2.3 Schema 映射、解析、权限、租户与审计 API 集成测试。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.parsing.service as parsing_service
from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import ExpenseRow, FieldAvailability, FileVersion, ParseStatus
from app.db.models.config import SchemaMappingVersion
from app.db.models.tenancy import AppUser, Role, Tenant
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

PASSWORD = "correct-horse-battery-staple"
HEADERS = ("金额", "日期", "商户", "备用金额")
EXPECTED_FIELDS = [
    "amount",
    "expense_date",
    "employee",
    "expense_type",
    "invoice_type",
    "invoice_no",
    "merchant",
    "invoice_title",
    "submission_date",
    "location",
    "currency",
    "description",
]


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    instance = create_app()
    instance.state.session_factory = session_factory
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as value:
        yield value


async def _seed_tenant_and_batch(
    session: AsyncSession,
    *,
    slug: str,
    raw_rows: Sequence[dict[str, object]] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, dict[Role, uuid.UUID]]:
    tenant = Tenant(slug=slug, name=f"租户 {slug}")
    session.add(tenant)
    await session.flush()
    bind_tenant(session.sync_session, tenant.id)
    users: dict[Role, uuid.UUID] = {}
    for role in Role:
        user = AppUser(
            tenant_id=tenant.id,
            username=role.value,
            password_hash=hash_password(PASSWORD),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        users[role] = user.id
    rows = tuple(raw_rows or _rows())
    batch = FileVersion(
        tenant_id=tenant.id,
        filename="报销.xlsx",
        content_hash=uuid.uuid4().hex.ljust(64, "0")[:64],
        row_count=len(rows),
        uploaded_by=users[Role.AUDITOR],
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        ExpenseRow(
            tenant_id=tenant.id,
            file_version_id=batch.id,
            row_no=index + 2,
            raw_json=row,
        )
        for index, row in enumerate(rows)
    )
    await session.commit()
    return tenant.id, batch.id, users


def _rows() -> tuple[dict[str, object], ...]:
    return (
        {"金额": "100", "日期": "2026-07-01", "商户": "上海酒店", "备用金额": "10"},
        {"金额": "bad", "日期": "2026-07-02", "商户": "上海酒店", "备用金额": "20"},
        {"金额": "300", "日期": "2026-07-03", "商户": "普通商店", "备用金额": "30"},
    )


async def _login(client: AsyncClient, *, slug: str, role: Role) -> None:
    client.cookies.clear()
    response = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": role.value, "password": PASSWORD},
    )
    assert response.status_code == 200


def _mapping_payload(batch_id: uuid.UUID, *, amount_source: str = "金额") -> dict[str, object]:
    return {
        "file_version_id": str(batch_id),
        "mappings": [
            {"source_column": amount_source, "target_field": "amount"},
            {"source_column": "日期", "target_field": "expense_date"},
            {"source_column": "商户", "target_field": "merchant"},
        ],
        "availability_thresholds": {
            "available_min_non_null_rate": "0.8000",
            "inferred_min_success_rate": "0.8000",
        },
        "currency_aliases": {"RMB": "CNY"},
        "inference_rules": [
            {
                "rule_id": "location-from-merchant-v1",
                "type": "literal_lookup",
                "target_field": "location",
                "source_fields": ["merchant"],
                "cases": [{"literal": "酒店", "value": "上海"}],
            }
        ],
    }


async def _create_mapping(
    client: AsyncClient,
    batch_id: uuid.UUID,
    *,
    amount_source: str = "金额",
) -> dict[str, object]:
    response = await client.put(
        "/api/schema-mappings",
        json=_mapping_payload(batch_id, amount_source=amount_source),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _audit_rows(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    action: str,
) -> tuple[AuditLog, ...]:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        return tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.at)
                )
            ).all()
        )


async def test_映射创建复用新版本查询与审计(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, users = await _seed_tenant_and_batch(session, slug="acme")
    await _login(client, slug="acme", role=Role.CONFIGURATOR)

    first = await client.put("/api/schema-mappings", json=_mapping_payload(batch_id))
    repeated = await client.put("/api/schema-mappings", json=_mapping_payload(batch_id))
    second = await client.put(
        "/api/schema-mappings",
        json=_mapping_payload(batch_id, amount_source="备用金额"),
    )
    listed = await client.get(
        "/api/schema-mappings",
        params={"file_version_id": str(batch_id)},
    )

    assert first.status_code == 201
    assert repeated.status_code == 200
    assert repeated.json()["id"] == first.json()["id"]
    assert repeated.json()["reused_existing"] is True
    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert listed.status_code == 200
    assert listed.json()["source_columns"] == sorted(HEADERS)
    assert [item["version"] for item in listed.json()["versions"]] == [2, 1]
    assert all(item["is_current_for_batch"] is False for item in listed.json()["versions"])

    audits = await _audit_rows(
        session_factory,
        tenant_id=tenant_id,
        action="schema_mapping_version.create",
    )
    assert len(audits) == 2
    assert all(item.actor_id == users[Role.CONFIGURATOR] for item in audits)
    assert set(audits[0].payload_json or {}) == {
        "mapping_version_id",
        "version",
        "header_signature",
        "target_fields",
    }


async def test_并发保存相同映射只创建一个版本(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, _ = await _seed_tenant_and_batch(session, slug="put-lock")
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as first_client,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as second_client,
    ):
        await asyncio.gather(
            _login(first_client, slug="put-lock", role=Role.CONFIGURATOR),
            _login(second_client, slug="put-lock", role=Role.CONFIGURATOR),
        )
        responses = await asyncio.gather(
            first_client.put("/api/schema-mappings", json=_mapping_payload(batch_id)),
            second_client.put("/api/schema-mappings", json=_mapping_payload(batch_id)),
        )

    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["id"] == responses[1].json()["id"]
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        version_count = await session.scalar(select(func.count()).select_from(SchemaMappingVersion))
    assert version_count == 1
    assert (
        len(
            await _audit_rows(
                session_factory,
                tenant_id=tenant_id,
                action="schema_mapping_version.create",
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda body: body["mappings"].pop(0), "MAPPING_REQUIRED_FIELD_MISSING"),
        (
            lambda body: body["mappings"].append(
                {"source_column": "不存在", "target_field": "employee"}
            ),
            "MAPPING_SOURCE_COLUMN_UNKNOWN",
        ),
        (
            lambda body: body["mappings"].__setitem__(
                0, {"source_column": "金额", "target_field": "unknown"}
            ),
            "MAPPING_TARGET_FIELD_UNKNOWN",
        ),
        (
            lambda body: body["availability_thresholds"].__setitem__(
                "available_min_non_null_rate", "1.1"
            ),
            "MAPPING_THRESHOLD_INVALID",
        ),
    ],
)
async def test_映射保存返回稳定校验错误码(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    mutate,
    code: str,
) -> None:
    async with session_factory() as session:
        _, batch_id, _ = await _seed_tenant_and_batch(session, slug="validation")
    await _login(client, slug="validation", role=Role.CONFIGURATOR)
    body = _mapping_payload(batch_id)
    mutate(body)

    response = await client.put("/api/schema-mappings", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


async def test_部分失败错误清单可用性重复解析与审计(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, _ = await _seed_tenant_and_batch(session, slug="parse")
    await _login(client, slug="parse", role=Role.CONFIGURATOR)
    mapping = await _create_mapping(client, batch_id)

    first = await client.post(
        f"/api/batches/{batch_id}/parse",
        json={"mapping_version_id": mapping["id"]},
    )
    repeated = await client.post(
        f"/api/batches/{batch_id}/parse",
        json={"mapping_version_id": mapping["id"]},
    )
    errors = await client.get(f"/api/batches/{batch_id}/parse-errors", params={"limit": 1})
    availability = await client.get(f"/api/batches/{batch_id}/field-availability")

    assert first.status_code == repeated.status_code == 200
    assert first.json()["status"] == "parsed_with_errors"
    assert (first.json()["success_count"], first.json()["error_count"]) == (2, 1)
    assert repeated.json()["reused_existing"] is True
    assert repeated.json()["parsed_at"] == first.json()["parsed_at"]
    assert errors.status_code == 200
    assert errors.json()["total"] == 1
    assert errors.json()["items"][0]["row_no"] == 3
    assert errors.json()["items"][0]["raw_json"]["金额"] == "bad"
    assert availability.status_code == 200
    assert [item["field_name"] for item in availability.json()["items"]] == EXPECTED_FIELDS

    audits = await _audit_rows(session_factory, tenant_id=tenant_id, action="batch.parse")
    assert len(audits) == 1
    assert set(audits[0].payload_json or {}) == {
        "file_version_id",
        "mapping_version_id",
        "mapping_version",
        "status",
        "total_rows",
        "success_count",
        "error_count",
    }


async def test_换版本重解析清除旧错误并切换当前版本(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, _ = await _seed_tenant_and_batch(session, slug="reparse")
    await _login(client, slug="reparse", role=Role.CONFIGURATOR)
    first_mapping = await _create_mapping(client, batch_id)
    first = await client.post(
        f"/api/batches/{batch_id}/parse",
        json={"mapping_version_id": first_mapping["id"]},
    )
    second_mapping = await _create_mapping(client, batch_id, amount_source="备用金额")
    second = await client.post(
        f"/api/batches/{batch_id}/parse",
        json={"mapping_version_id": second_mapping["id"]},
    )
    errors = await client.get(f"/api/batches/{batch_id}/parse-errors")
    mappings = await client.get("/api/schema-mappings", params={"file_version_id": str(batch_id)})

    assert first.json()["error_count"] == 1
    assert second.json()["status"] == "parsed"
    assert second.json()["error_count"] == 0
    assert errors.json()["items"] == []
    current = [item for item in mappings.json()["versions"] if item["is_current_for_batch"]]
    assert [item["id"] for item in current] == [second_mapping["id"]]
    assert len(await _audit_rows(session_factory, tenant_id=tenant_id, action="batch.parse")) == 2


async def test_权限与未认证边界(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, batch_id, _ = await _seed_tenant_and_batch(session, slug="roles")

    anonymous = await client.get("/api/schema-mappings", params={"file_version_id": str(batch_id)})
    await _login(client, slug="roles", role=Role.AUDITOR)
    auditor_get = await client.get(
        "/api/schema-mappings", params={"file_version_id": str(batch_id)}
    )
    auditor_put = await client.put("/api/schema-mappings", json=_mapping_payload(batch_id))
    await _login(client, slug="roles", role=Role.VIEWER)
    viewer_get_mapping = await client.get(
        "/api/schema-mappings", params={"file_version_id": str(batch_id)}
    )
    viewer_parse = await client.post(
        f"/api/batches/{batch_id}/parse", json={"mapping_version_id": str(uuid.uuid4())}
    )
    viewer_errors = await client.get(f"/api/batches/{batch_id}/parse-errors")

    assert anonymous.status_code == 401
    assert auditor_get.status_code == 200
    assert auditor_put.status_code == 403
    assert viewer_get_mapping.status_code == 403
    assert viewer_parse.status_code == 403
    assert viewer_errors.status_code == 409
    assert viewer_errors.json()["error"]["code"] == "BATCH_NOT_PARSED"


async def test_跨租户资源统一不可见(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, batch_a, _ = await _seed_tenant_and_batch(session, slug="tenant-a")
    async with session_factory() as session:
        _, batch_b, _ = await _seed_tenant_and_batch(session, slug="tenant-b")
    await _login(client, slug="tenant-b", role=Role.CONFIGURATOR)
    mapping_b = await _create_mapping(client, batch_b)
    await _login(client, slug="tenant-a", role=Role.CONFIGURATOR)

    responses = [
        await client.get("/api/schema-mappings", params={"file_version_id": str(batch_b)}),
        await client.put("/api/schema-mappings", json=_mapping_payload(batch_b)),
        await client.post(
            f"/api/batches/{batch_b}/parse", json={"mapping_version_id": mapping_b["id"]}
        ),
        await client.get(f"/api/batches/{batch_b}/parse-errors"),
        await client.get(f"/api/batches/{batch_b}/field-availability"),
    ]
    foreign_mapping = await client.post(
        f"/api/batches/{batch_a}/parse", json={"mapping_version_id": mapping_b["id"]}
    )

    assert all(response.status_code == 404 for response in responses)
    assert all(response.json()["error"]["code"] == "BATCH_NOT_FOUND" for response in responses)
    assert foreign_mapping.status_code == 404
    assert foreign_mapping.json()["error"]["code"] == "MAPPING_VERSION_NOT_FOUND"


async def test_API并发解析锁冲突返回409且无半批(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, _ = await _seed_tenant_and_batch(session, slug="lock")
    await _login(client, slug="lock", role=Role.CONFIGURATOR)
    mapping = await _create_mapping(client, batch_id)

    async with session_factory() as lock_session:
        bind_tenant(lock_session.sync_session, tenant_id)
        locked = await lock_session.scalar(
            select(FileVersion).where(FileVersion.id == batch_id).with_for_update(nowait=True)
        )
        assert locked is not None
        response = await client.post(
            f"/api/batches/{batch_id}/parse", json={"mapping_version_id": mapping["id"]}
        )
        await lock_session.rollback()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "BATCH_PARSE_IN_PROGRESS"
    assert len(await _audit_rows(session_factory, tenant_id=tenant_id, action="batch.parse")) == 0


async def test_API系统异常回滚整批并独立记录失败审计(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, _ = await _seed_tenant_and_batch(session, slug="failure")
    await _login(client, slug="failure", role=Role.CONFIGURATOR)
    mapping = await _create_mapping(client, batch_id)

    async def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected private details")

    monkeypatch.setattr(parsing_service, "_replace_availability", _explode)
    response = await client.post(
        f"/api/batches/{batch_id}/parse", json={"mapping_version_id": mapping["id"]}
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "BATCH_PARSE_INTERNAL_ERROR"
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        batch = await session.get(FileVersion, batch_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow).where(ExpenseRow.file_version_id == batch_id)
                )
            ).all()
        )
        availability_count = await session.scalar(
            select(func.count())
            .select_from(FieldAvailability)
            .where(FieldAvailability.file_version_id == batch_id)
        )
    assert batch is not None
    assert batch.parse_status is ParseStatus.UNPARSED
    assert batch.mapping_version_id is None
    assert all(row.normalized_json is None for row in rows)
    assert availability_count == 0
    failures = await _audit_rows(session_factory, tenant_id=tenant_id, action="batch.parse_failed")
    assert len(failures) == 1
    assert set(failures[0].payload_json or {}) == {
        "file_version_id",
        "mapping_version_id",
        "error_category",
    }
    assert "injected private details" not in str(failures[0].payload_json)


async def test_请求体不接受客户端租户或版本(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, batch_id, _ = await _seed_tenant_and_batch(session, slug="strict")
    await _login(client, slug="strict", role=Role.CONFIGURATOR)
    body = _mapping_payload(batch_id)
    body["tenant_id"] = str(uuid.uuid4())
    body["version"] = 99

    response = await client.put("/api/schema-mappings", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"


async def test_分页边界由API校验(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        _, batch_id, _ = await _seed_tenant_and_batch(session, slug="pagination")
    await _login(client, slug="pagination", role=Role.VIEWER)

    invalid = await client.get(
        f"/api/batches/{batch_id}/parse-errors", params={"offset": -1, "limit": 201}
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
