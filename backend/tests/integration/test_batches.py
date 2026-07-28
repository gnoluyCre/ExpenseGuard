"""批次导入 API 集成测试。"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from io import BytesIO

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security.password import hash_password
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow, FileVersion, RevisionReason
from app.db.models.tenancy import AppUser, Role, Tenant
from app.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """接到测试库的应用实例。"""
    instance = create_app()
    instance.state.session_factory = session_factory
    return instance


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """ASGI 客户端。"""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        yield ac


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
    roles: tuple[Role, ...] = (Role.AUDITOR,),
) -> tuple[uuid.UUID, dict[Role, uuid.UUID]]:
    tenant = Tenant(slug=slug, name=f"租户 {slug}")
    session.add(tenant)
    await session.flush()

    users: dict[Role, uuid.UUID] = {}
    for role in roles:
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
    await session.commit()
    return tenant.id, users


async def _login(client: AsyncClient, *, slug: str, username: str) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"tenant_slug": slug, "username": username, "password": PASSWORD},
    )
    assert response.status_code == 200


def _xlsx_bytes(*, rows: int = 500) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(("员工", "金额"))
    for index in range(rows):
        sheet.append((f"员工{index}", index))
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


async def _upload(client: AsyncClient, content: bytes, filename: str = "报销.xlsx"):
    return await client.post(
        "/api/batches",
        files={
            "file": (
                filename,
                content,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


async def test_上传成功写入文件版本与原始行(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, users = await _seed_tenant(session, slug="acme")

    await _login(client, slug="acme", username="auditor")
    response = await _upload(client, _xlsx_bytes())

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "报销.xlsx"
    assert body["row_count"] == 500
    assert body["stored_rows"] == 500
    assert body["reused_existing"] is False
    assert body["uploaded_by"] == str(users[Role.AUDITOR])

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        file_version = await session.get(FileVersion, uuid.UUID(body["file_version_id"]))
        assert file_version is not None
        assert file_version.row_count == 500
        count = await session.scalar(
            select(func.count())
            .select_from(ExpenseRow)
            .where(ExpenseRow.file_version_id == file_version.id)
        )
        assert count == 500
        first_row = await session.scalar(
            select(ExpenseRow)
            .where(ExpenseRow.file_version_id == file_version.id)
            .order_by(ExpenseRow.row_no)
        )
        assert first_row is not None
        assert first_row.row_no == 2
        assert first_row.raw_json == {"员工": "员工0", "金额": 0}


async def test_重复上传复用既有批次且不重复插入行(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, slug="acme")

    await _login(client, slug="acme", username="auditor")
    content = _xlsx_bytes()
    first = await _upload(client, content)
    second = await _upload(client, content)

    assert first.status_code == second.status_code == 200
    assert second.json()["file_version_id"] == first.json()["file_version_id"]
    assert second.json()["reused_existing"] is True
    assert second.json()["stored_rows"] == 500

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        file_count = await session.scalar(select(func.count()).select_from(FileVersion))
        row_count = await session.scalar(select(func.count()).select_from(ExpenseRow))
    assert file_count == 1
    assert row_count == 500


async def test_普通重复上传只复用_revision_1_而不返回派生版本(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, _ = await _seed_tenant(session, slug="acme")

    await _login(client, slug="acme", username="auditor")
    content = _xlsx_bytes()
    first = await _upload(client, content)
    root_id = uuid.UUID(first.json()["file_version_id"])

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        root = await session.get(FileVersion, root_id)
        assert root is not None
        derived = FileVersion(
            tenant_id=tenant_id,
            filename=root.filename,
            content_hash=root.content_hash,
            row_count=root.row_count,
            uploaded_by=root.uploaded_by,
            revision_no=2,
            source_file_version_id=root.id,
            root_file_version_id=root.id,
            revision_reason=RevisionReason.MAPPING_CHANGE,
            revision_request_key_hash="a" * 64,
            revision_request_fingerprint="b" * 64,
        )
        session.add(derived)
        await session.commit()
        derived_id = derived.id

    repeated = await _upload(client, content)

    assert repeated.status_code == 200
    assert repeated.json()["file_version_id"] == str(root_id)
    assert repeated.json()["file_version_id"] != str(derived_id)
    assert repeated.json()["reused_existing"] is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        file_count = await session.scalar(select(func.count()).select_from(FileVersion))
    assert file_count == 2


async def test_跨租户同文件各自生成批次(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_tenant(session, slug="a")
        await _seed_tenant(session, slug="b")

    content = _xlsx_bytes()
    await _login(client, slug="a", username="auditor")
    first = await _upload(client, content)
    await client.post("/api/auth/logout")
    await _login(client, slug="b", username="auditor")
    second = await _upload(client, content)

    assert first.status_code == second.status_code == 200
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert first.json()["file_version_id"] != second.json()["file_version_id"]
    assert second.json()["reused_existing"] is False


async def test_viewer_不能上传但可以读取批次(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_tenant(session, slug="acme", roles=(Role.VIEWER,))

    await _login(client, slug="acme", username="viewer")
    upload = await _upload(client, _xlsx_bytes())
    batches = await client.get("/api/batches")

    assert upload.status_code == 403
    assert batches.status_code == 200
    assert batches.json() == []


async def test_列表与详情返回当前租户批次(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await _seed_tenant(session, slug="acme")

    await _login(client, slug="acme", username="auditor")
    uploaded = await _upload(client, _xlsx_bytes())
    file_version_id = uploaded.json()["file_version_id"]

    listed = await client.get("/api/batches")
    detail = await client.get(f"/api/batches/{file_version_id}")

    assert listed.status_code == 200
    assert [row["file_version_id"] for row in listed.json()] == [file_version_id]
    assert detail.status_code == 200
    body = detail.json()
    assert body["file_version_id"] == file_version_id
    assert len(body["rows"]) == 500
    assert body["rows"][0] == {
        "row_no": 2,
        "raw_json": {"员工": "员工0", "金额": 0},
        "parse_error": None,
    }
