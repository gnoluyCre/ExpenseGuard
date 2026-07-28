"""显式 file_version 派生服务的 PostgreSQL 集成测试。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.batches.revisions as revision_service
from app.core.batches.revisions import (
    InvalidIdempotencyKeyError,
    RevisionError,
    RevisionRequest,
    RevisionRequestReason,
    create_file_revision,
    idempotency_key_hash,
    revision_request_fingerprint,
)
from app.core.errors import NotFoundError
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import (
    ExpenseRow,
    FieldAvailability,
    FieldStatus,
    FileVersion,
    ParseStatus,
    RowResult,
)
from app.db.models.config import SchemaMappingVersion
from app.db.models.findings import Finding
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_tenant(session: AsyncSession, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = Tenant(slug=slug, name=f"{slug} 租户")
    session.add(tenant)
    await session.flush()
    bind_tenant(session.sync_session, tenant.id)
    user = AppUser(
        tenant_id=tenant.id,
        username="auditor",
        password_hash="test-only",
        role=Role.AUDITOR,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return tenant.id, user.id


async def _seed_mapping(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    mapping = SchemaMappingVersion(
        tenant_id=tenant_id,
        header_signature="1" * 64,
        version=1,
        config_fingerprint="2" * 64,
        availability_thresholds={},
        currency_aliases={},
        inference_config={},
        created_by=actor_id,
    )
    session.add(mapping)
    await session.flush()
    return mapping.id


async def _seed_source(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    status: ParseStatus = ParseStatus.PARSED_WITH_ERRORS,
    rows: Sequence[tuple[dict[str, object], dict[str, object] | None]] | None = None,
) -> FileVersion:
    mapping_id = await _seed_mapping(session, tenant_id=tenant_id, actor_id=actor_id)
    source_rows = rows or (
        (
            {"金额": "100", "日期": "2026-07-01"},
            {
                "schema_version": 1,
                "mapping_version_id": str(mapping_id),
                "amount": "100",
                "expense_date": "2026-07-01",
                "field_provenance": {},
            },
        ),
        ({"金额": "bad", "日期": "2026-07-02"}, None),
    )
    source = FileVersion(
        tenant_id=tenant_id,
        filename="source.xlsx",
        content_hash=uuid.uuid4().hex * 2,
        row_count=len(source_rows),
        uploaded_by=actor_id,
        mapping_version_id=mapping_id if status is not ParseStatus.UNPARSED else None,
        parse_status=status,
    )
    session.add(source)
    await session.flush()
    for index, (raw, normalized) in enumerate(source_rows, start=2):
        failed = normalized is None and status in {
            ParseStatus.PARSED,
            ParseStatus.PARSED_WITH_ERRORS,
        }
        session.add(
            ExpenseRow(
                tenant_id=tenant_id,
                file_version_id=source.id,
                row_no=index,
                raw_json=dict(raw),
                normalized_json=dict(normalized) if normalized is not None else None,
                parse_error="解析失败" if failed else None,
                parse_error_code="ROW_VALIDATION_FAILED" if failed else None,
                parse_error_detail={"schema_version": 1, "errors": []} if failed else None,
            )
        )
    await session.flush()
    return source


async def _create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_id: uuid.UUID,
    reason: RevisionRequestReason,
    key: str,
):
    return await create_file_revision(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_file_version_id=source_id,
        reason=reason,
        idempotency_key=key,
    )


async def test_ruleset_change复制解析快照但不复制判定副作用(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-rules")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        session.add(
            FieldAvailability(
                tenant_id=tenant_id,
                file_version_id=source.id,
                field_name="amount",
                status=FieldStatus.AVAILABLE,
                evidence={"non_null_count": 1},
            )
        )
        session.add(
            RowResult(
                tenant_id=tenant_id,
                file_version_id=source.id,
                row_no=2,
                verdict="flagged",
                rule_version="old-ruleset",
            )
        )
        session.add(
            Finding(
                tenant_id=tenant_id,
                file_version_id=source.id,
                row_no=2,
                kind="legacy-test",
                severity_impact=1,
                severity_confidence=1,
            )
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.RULESET_CHANGE,
            key="ruleset-key-0001",
        )
        await session.commit()

    assert result.reused_existing is False
    assert result.revision_no == 2
    assert result.source_file_version_id == source.id
    assert result.root_file_version_id == source.id
    assert result.parse_status is ParseStatus.PARSED_WITH_ERRORS
    assert result.mapping_version_id == source.mapping_version_id

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        derived_rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == result.file_version_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
        availability = tuple(
            (
                await session.scalars(
                    select(FieldAvailability).where(
                        FieldAvailability.file_version_id == result.file_version_id
                    )
                )
            ).all()
        )
        row_results = await session.scalar(
            select(func.count())
            .select_from(RowResult)
            .where(RowResult.file_version_id == result.file_version_id)
        )
        findings = await session.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.file_version_id == result.file_version_id)
        )
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "batch.revision_create")
        )

    assert [row.raw_json for row in derived_rows] == [
        {"金额": "100", "日期": "2026-07-01"},
        {"金额": "bad", "日期": "2026-07-02"},
    ]
    assert derived_rows[0].normalized_json is not None
    assert derived_rows[1].parse_error_code == "ROW_VALIDATION_FAILED"
    assert len(availability) == 1
    assert availability[0].evidence == {"non_null_count": 1}
    assert row_results == 0
    assert findings == 0
    assert audit is not None
    assert audit.payload_json == {
        "source_file_version_id": str(source.id),
        "target_file_version_id": str(result.file_version_id),
        "reason": "ruleset_change",
        "revision_no": 2,
        "mapping_version_id": str(source.mapping_version_id),
    }


async def test_policy_change复制原始解析与字段快照但不复制_f3_副作用(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-policy")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        session.add(
            FieldAvailability(
                tenant_id=tenant_id,
                file_version_id=source.id,
                field_name="expense_date",
                status=FieldStatus.AVAILABLE,
                evidence={"non_null_count": 1},
            )
        )
        session.add(
            RowResult(
                tenant_id=tenant_id,
                file_version_id=source.id,
                row_no=2,
                verdict="flagged",
                rule_version="source-ruleset",
            )
        )
        session.add(
            Finding(
                tenant_id=tenant_id,
                file_version_id=source.id,
                row_no=2,
                kind="source-policy-finding",
                severity_impact=1,
                severity_confidence=1,
            )
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.POLICY_CHANGE,
            key="policy-change-key-0001",
        )
        await session.commit()

    assert result.reason is RevisionRequestReason.POLICY_CHANGE
    assert result.parse_status is ParseStatus.PARSED_WITH_ERRORS
    assert result.mapping_version_id == source.mapping_version_id
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        derived_rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == result.file_version_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
        availability_count = await session.scalar(
            select(func.count())
            .select_from(FieldAvailability)
            .where(FieldAvailability.file_version_id == result.file_version_id)
        )
        row_result_count = await session.scalar(
            select(func.count())
            .select_from(RowResult)
            .where(RowResult.file_version_id == result.file_version_id)
        )
        finding_count = await session.scalar(
            select(func.count())
            .select_from(Finding)
            .where(Finding.file_version_id == result.file_version_id)
        )

    assert [row.raw_json for row in derived_rows] == [
        {"金额": "100", "日期": "2026-07-01"},
        {"金额": "bad", "日期": "2026-07-02"},
    ]
    assert derived_rows[0].normalized_json is not None
    assert derived_rows[1].parse_error_code == "ROW_VALIDATION_FAILED"
    assert availability_count == 1
    assert row_result_count == 0
    assert finding_count == 0


async def test_mapping_change只复制原始证据并清空解析状态(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-mapping")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        session.add(
            FieldAvailability(
                tenant_id=tenant_id,
                file_version_id=source.id,
                field_name="amount",
                status=FieldStatus.AVAILABLE,
                evidence={},
            )
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.MAPPING_CHANGE,
            key="mapping-key-0001",
        )
        await session.commit()

    assert result.parse_status is ParseStatus.UNPARSED
    assert result.mapping_version_id is None
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == result.file_version_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
        availability_count = await session.scalar(
            select(func.count())
            .select_from(FieldAvailability)
            .where(FieldAvailability.file_version_id == result.file_version_id)
        )
    assert all(row.normalized_json is None for row in rows)
    assert all(row.parse_error is None for row in rows)
    assert all(row.parse_error_code is None for row in rows)
    assert all(row.parse_error_detail is None for row in rows)
    assert availability_count == 0


async def test_派生中途系统异常在外层提交后仍整批回滚(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-rollback")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        await session.commit()

    async def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected revision audit failure")

    monkeypatch.setattr(revision_service, "write_audit", _explode)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RuntimeError, match="injected revision audit failure"):
            await _create(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_id=source.id,
                reason=RevisionRequestReason.RULESET_CHANGE,
                key="rollback-key-0001",
            )
        # 故意提交外层事务，证明服务保存点而非测试 teardown 完成回滚。
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        versions = await session.scalar(select(func.count()).select_from(FileVersion))
        rows = await session.scalar(select(func.count()).select_from(ExpenseRow))
        audits = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "batch.revision_create")
        )
    assert versions == 1
    assert rows == 2
    assert audits == 0


async def test_派生链始终保留_revision1_root_和直接_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-lineage")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        second = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.RULESET_CHANGE,
            key="lineage-key-0002",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        third = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=second.file_version_id,
            reason=RevisionRequestReason.MAPPING_CHANGE,
            key="lineage-key-0003",
        )
        await session.commit()

    assert third.revision_no == 3
    assert third.source_file_version_id == second.file_version_id
    assert third.root_file_version_id == source.id


async def test_相同_key_同请求复用且不同请求冲突(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="revision-idempotency")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.RULESET_CHANGE,
            key="same-request-key",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        reused = await _create(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_id=source.id,
            reason=RevisionRequestReason.RULESET_CHANGE,
            key="same-request-key",
        )
        await session.commit()
    assert reused.file_version_id == first.file_version_id
    assert reused.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RevisionError) as exc:
            await _create(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_id=source.id,
                reason=RevisionRequestReason.MAPPING_CHANGE,
                key="same-request-key",
            )
        assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        audit_count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "batch.revision_create")
        )
        revisions = await session.scalar(select(func.count()).select_from(FileVersion))
    assert audit_count == 1
    assert revisions == 2


@pytest.mark.parametrize("key", ["short", "x" * 129])
async def test_非法_idempotency_key_稳定拒绝且不写库(
    session_factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug=f"bad-key-{uuid.uuid4().hex[:8]}")
        source = await _seed_source(session, tenant_id=tenant_id, actor_id=actor_id)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(InvalidIdempotencyKeyError) as exc:
            await _create(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_id=source.id,
                reason=RevisionRequestReason.MAPPING_CHANGE,
                key=key,
            )
        assert exc.value.code == "IDEMPOTENCY_KEY_INVALID"


@pytest.mark.parametrize("status", [ParseStatus.UNPARSED, ParseStatus.FAILED])
async def test_ruleset_change拒绝未成功解析来源(
    session_factory: async_sessionmaker[AsyncSession], status: ParseStatus
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(
            session, slug=f"bad-source-{status.value}-{uuid.uuid4().hex[:6]}"
        )
        source = await _seed_source(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            status=status,
            rows=(({"金额": "bad"}, None),),
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RevisionError) as exc:
            await _create(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_id=source.id,
                reason=RevisionRequestReason.RULESET_CHANGE,
                key="unparsed-source-key",
            )
        assert exc.value.code == "BATCH_NOT_PARSED"


async def test_ruleset_change拒绝零成功行(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, actor_id = await _seed_tenant(session, slug="zero-success")
        source = await _seed_source(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            rows=(({"金额": "bad"}, None),),
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RevisionError) as exc:
            await _create(
                session,
                tenant_id=tenant_id,
                actor_id=actor_id,
                source_id=source.id,
                reason=RevisionRequestReason.RULESET_CHANGE,
                key="zero-success-key",
            )
        assert exc.value.code == "BATCH_NOT_PARSED"


async def test_跨租户来源不可见且租户锁冲突不泄漏数据库异常(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_a, actor_a = await _seed_tenant(session, slug="revision-tenant-a")
        source = await _seed_source(session, tenant_id=tenant_a, actor_id=actor_a)
        await session.commit()
    async with session_factory() as session:
        tenant_b, actor_b = await _seed_tenant(session, slug="revision-tenant-b")
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_b)
        with pytest.raises(NotFoundError) as exc_info:
            await _create(
                session,
                tenant_id=tenant_b,
                actor_id=actor_b,
                source_id=source.id,
                reason=RevisionRequestReason.MAPPING_CHANGE,
                key="cross-tenant-key",
            )
        assert getattr(exc_info.value, "code", None) == "BATCH_NOT_FOUND"
        await session.rollback()

    async with session_factory() as lock_session, session_factory() as contender:
        bind_tenant(lock_session.sync_session, tenant_a)
        bind_tenant(contender.sync_session, tenant_a)
        await lock_tenant_nowait(lock_session, tenant_a)
        with pytest.raises(RevisionError) as conflict:
            await _create(
                contender,
                tenant_id=tenant_a,
                actor_id=actor_a,
                source_id=source.id,
                reason=RevisionRequestReason.MAPPING_CHANGE,
                key="concurrent-key-1",
            )
        assert conflict.value.code == "BATCH_VALIDATION_IN_PROGRESS"
        await contender.rollback()
        await lock_session.rollback()


async def test_key_hash与请求指纹稳定且区分请求() -> None:
    source_id = uuid.uuid4()
    assert idempotency_key_hash("12345678") == idempotency_key_hash("12345678")
    assert idempotency_key_hash("含中文字符12345") == idempotency_key_hash("含中文字符12345")
    ruleset = revision_request_fingerprint(
        source_file_version_id=source_id,
        reason=RevisionRequestReason.RULESET_CHANGE,
    )
    mapping = revision_request_fingerprint(
        source_file_version_id=source_id,
        reason=RevisionRequestReason.MAPPING_CHANGE,
    )
    policy = revision_request_fingerprint(
        source_file_version_id=source_id,
        reason=RevisionRequestReason.POLICY_CHANGE,
    )
    assert ruleset != mapping
    assert len({ruleset, mapping, policy}) == 3
    assert (
        RevisionRequest(
            reason=RevisionRequestReason.MAPPING_CHANGE,
            idempotency_key="bad key!",
        ).idempotency_key
        == "bad key!"
    )
