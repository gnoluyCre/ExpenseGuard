"""F2 解析服务的真实 PostgreSQL 原子性与幂等测试。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.parsing.service as parsing_service
from app.core.parsing.mapping import compute_header_signature
from app.core.parsing.service import BatchParsingError, parse_batch
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.batch import ExpenseRow, FieldAvailability, FileVersion, ParseStatus
from app.db.models.config import SchemaMapping, SchemaMappingVersion
from app.db.models.tenancy import AppUser, Role, Tenant
from app.db.models.validation import (
    ValidationDependency,
    ValidationRun,
    ValidationRunStatus,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

HEADERS = ("金额", "日期", "商户", "备用金额")


async def _seed_tenant_and_batch(
    session: AsyncSession,
    *,
    raw_rows: Sequence[dict[str, object]],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex[:8]}", name="解析测试租户")
    session.add(tenant)
    await session.flush()
    bind_tenant(session.sync_session, tenant.id)

    user = AppUser(
        tenant_id=tenant.id,
        username="auditor",
        password_hash="test-only-hash",
        role=Role.AUDITOR,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    batch = FileVersion(
        tenant_id=tenant.id,
        filename="报销.xlsx",
        content_hash=uuid.uuid4().hex.ljust(64, "0")[:64],
        row_count=len(raw_rows),
        uploaded_by=user.id,
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        ExpenseRow(
            tenant_id=tenant.id,
            file_version_id=batch.id,
            row_no=index + 2,
            raw_json=raw,
        )
        for index, raw in enumerate(raw_rows)
    )
    mapping_id = await _add_mapping_version(
        session,
        tenant_id=tenant.id,
        created_by=user.id,
        version=1,
        amount_source="金额",
    )
    await session.commit()
    return tenant.id, batch.id, mapping_id


async def _add_mapping_version(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    version: int,
    amount_source: str,
) -> uuid.UUID:
    mapping_version = SchemaMappingVersion(
        tenant_id=tenant_id,
        header_signature=compute_header_signature(HEADERS),
        version=version,
        config_fingerprint=str(version).zfill(64),
        availability_thresholds={
            "available_min_non_null_rate": "0.8000",
            "inferred_min_success_rate": "0.8000",
        },
        currency_aliases={"RMB": "CNY"},
        inference_config={
            "rules": [
                {
                    "rule_id": f"location-v{version}",
                    "type": "literal_lookup",
                    "target_field": "location",
                    "source_fields": ["merchant"],
                    "cases": [{"literal": "酒店", "value": "上海"}],
                }
            ]
        },
        created_by=created_by,
    )
    session.add(mapping_version)
    await session.flush()
    entries = (
        (amount_source, "amount"),
        ("日期", "expense_date"),
        ("商户", "merchant"),
    )
    session.add_all(
        SchemaMapping(
            tenant_id=tenant_id,
            mapping_version_id=mapping_version.id,
            source_column=source,
            target_field=target,
            version=version,
        )
        for source, target in entries
    )
    await session.flush()
    return mapping_version.id


def _rows() -> tuple[dict[str, object], ...]:
    return (
        {"金额": "100.00", "日期": "2026-07-01", "商户": "上海酒店", "备用金额": "10"},
        {"金额": "bad", "日期": "2026-07-02", "商户": "上海酒店", "备用金额": "20"},
        {"金额": "300", "日期": "2026-07-03", "商户": "普通商店", "备用金额": "30"},
    )


async def _add_validation_run(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    mapping_version_id: uuid.UUID,
    triggered_by: uuid.UUID,
    total_rows: int,
) -> ValidationRun:
    run = ValidationRun(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        mapping_version_id=mapping_version_id,
        ruleset_fingerprint="f" * 64,
        ruleset_manifest={"schema_version": 1},
        status=ValidationRunStatus.COMPLETED,
        total_row_count=total_rows,
        evaluated_row_count=total_rows,
        passed_count=total_rows,
        flagged_count=0,
        manual_review_count=0,
        parse_failed_count=0,
        completed_at=utc_now(),
        triggered_by=triggered_by,
    )
    session.add(run)
    await session.flush()
    return run


async def test_批次解析持久化成功行失败行与十二项可用性(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await parse_batch(session, file_version_id=batch_id, mapping_version_id=mapping_id)
        await session.commit()

    assert result.status == "parsed_with_errors"
    assert (result.total_rows, result.success_count, result.error_count) == (3, 2, 1)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        batch = await session.get(FileVersion, batch_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == batch_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
        availability = tuple(
            (
                await session.scalars(
                    select(FieldAvailability).where(FieldAvailability.file_version_id == batch_id)
                )
            ).all()
        )
    assert batch is not None
    assert batch.parse_status is ParseStatus.PARSED_WITH_ERRORS
    assert rows[0].normalized_json is not None
    assert rows[0].normalized_json["amount"] == "100"
    assert rows[1].normalized_json is None
    assert rows[1].parse_error_code == "ROW_VALIDATION_FAILED"
    assert rows[1].raw_json["金额"] == "bad"
    assert rows[1].parse_error_detail["errors"][0]["code"] == "AMOUNT_INVALID_FORMAT"
    assert len(availability) == 12
    assert {item.field_name for item in availability} == {
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
    }


async def test_同版本重复解析直接复用且不刷新时间(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await parse_batch(session, file_version_id=batch_id, mapping_version_id=mapping_id)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        second = await parse_batch(session, file_version_id=batch_id, mapping_version_id=mapping_id)
        await session.commit()

    assert first.reused_existing is False
    assert second.reused_existing is True
    assert second.model_dump(exclude={"reused_existing"}) == first.model_dump(
        exclude={"reused_existing"}
    )


async def test_新映射版本从_raw_json_重算并清除旧错误(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, first_mapping_id = await _seed_tenant_and_batch(
            session, raw_rows=_rows()
        )
        user_id = await session.scalar(select(AppUser.id))
        assert user_id is not None
        second_mapping_id = await _add_mapping_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            version=2,
            amount_source="备用金额",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await parse_batch(session, file_version_id=batch_id, mapping_version_id=first_mapping_id)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await parse_batch(
            session, file_version_id=batch_id, mapping_version_id=second_mapping_id
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == batch_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )

    assert result.status == "parsed"
    assert result.error_count == 0
    assert rows[1].normalized_json["amount"] == "20"
    assert rows[1].parse_error is None
    assert rows[1].parse_error_code is None
    assert rows[1].parse_error_detail is None


async def test_新版本系统异常回滚并保留旧成功结果(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_rows = tuple({**row, "金额": "100"} for row in _rows())
    async with session_factory() as session:
        tenant_id, batch_id, first_mapping_id = await _seed_tenant_and_batch(
            session, raw_rows=valid_rows
        )
        user_id = await session.scalar(select(AppUser.id))
        assert user_id is not None
        second_mapping_id = await _add_mapping_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            version=2,
            amount_source="备用金额",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await parse_batch(
            session, file_version_id=batch_id, mapping_version_id=first_mapping_id
        )
        await session.commit()

    async def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected availability failure")

    monkeypatch.setattr(parsing_service, "_replace_availability", _explode)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RuntimeError, match="injected availability failure"):
            await parse_batch(
                session,
                file_version_id=batch_id,
                mapping_version_id=second_mapping_id,
            )
        # 故意提交外层事务：旧结果仍完整，才能证明是 service 保存点在回滚，
        # 而不是测试末尾整事务 rollback 掩盖了半批写入。
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        batch = await session.get(FileVersion, batch_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == batch_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
    assert batch is not None
    assert batch.mapping_version_id == first_mapping_id
    assert batch.parsed_at is not None
    assert batch.parsed_at.isoformat().replace("+00:00", "Z") == first.parsed_at
    assert [row.normalized_json["amount"] for row in rows] == ["100", "100", "100"]


async def test_初次解析系统异常在提交外层事务后仍无半批数据(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())

    async def _explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected first-parse failure")

    monkeypatch.setattr(parsing_service, "_replace_availability", _explode)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RuntimeError, match="injected first-parse failure"):
            await parse_batch(
                session,
                file_version_id=batch_id,
                mapping_version_id=mapping_id,
            )
        await session.commit()

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
        availability = tuple(
            (
                await session.scalars(
                    select(FieldAvailability).where(FieldAvailability.file_version_id == batch_id)
                )
            ).all()
        )
    assert batch is not None
    assert batch.parse_status is ParseStatus.UNPARSED
    assert batch.mapping_version_id is None
    assert batch.parsed_at is None
    assert all(row.normalized_json is None for row in rows)
    assert all(row.parse_error_code is None for row in rows)
    assert availability == ()


async def test_并发解析在租户行锁冲突时立即失败(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())

    async with session_factory() as lock_session, session_factory() as parse_session:
        bind_tenant(lock_session.sync_session, tenant_id)
        bind_tenant(parse_session.sync_session, tenant_id)
        locked = await lock_session.scalar(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update(nowait=True)
        )
        assert locked is not None

        with pytest.raises(BatchParsingError) as exc:
            await parse_batch(
                parse_session,
                file_version_id=batch_id,
                mapping_version_id=mapping_id,
            )
        assert exc.value.code == "BATCH_PARSE_IN_PROGRESS"
        await parse_session.rollback()
        await lock_session.rollback()


async def test_并发解析在批次行锁冲突时立即失败(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())

    async with session_factory() as lock_session, session_factory() as parse_session:
        bind_tenant(lock_session.sync_session, tenant_id)
        bind_tenant(parse_session.sync_session, tenant_id)
        locked = await lock_session.scalar(
            select(FileVersion).where(FileVersion.id == batch_id).with_for_update(nowait=True)
        )
        assert locked is not None

        with pytest.raises(BatchParsingError) as exc:
            await parse_batch(
                parse_session,
                file_version_id=batch_id,
                mapping_version_id=mapping_id,
            )
        assert exc.value.code == "BATCH_PARSE_IN_PROGRESS"
        await parse_session.rollback()
        await lock_session.rollback()


async def test_已有校验运行时拒绝原地重解析(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())
        user_id = await session.scalar(select(AppUser.id))
        assert user_id is not None
        await _add_validation_run(
            session,
            tenant_id=tenant_id,
            file_version_id=batch_id,
            mapping_version_id=mapping_id,
            triggered_by=user_id,
            total_rows=len(_rows()),
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(BatchParsingError) as exc:
            await parse_batch(
                session,
                file_version_id=batch_id,
                mapping_version_id=mapping_id,
            )
        assert exc.value.code == "BATCH_ALREADY_VALIDATED"


async def test_被校验依赖引用时拒绝原地重解析(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, batch_id, mapping_id = await _seed_tenant_and_batch(session, raw_rows=_rows())
        user_id = await session.scalar(select(AppUser.id))
        assert user_id is not None
        validating_batch = FileVersion(
            tenant_id=tenant_id,
            filename="校验来源.xlsx",
            content_hash=uuid.uuid4().hex.ljust(64, "0")[:64],
            row_count=1,
            uploaded_by=user_id,
            mapping_version_id=mapping_id,
            parse_status=ParseStatus.PARSED,
            parsed_at=utc_now(),
        )
        session.add(validating_batch)
        await session.flush()
        run = await _add_validation_run(
            session,
            tenant_id=tenant_id,
            file_version_id=validating_batch.id,
            mapping_version_id=mapping_id,
            triggered_by=user_id,
            total_rows=1,
        )
        session.add(
            ValidationDependency(
                tenant_id=tenant_id,
                validation_run_id=run.id,
                depended_file_version_id=batch_id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(BatchParsingError) as exc:
            await parse_batch(
                session,
                file_version_id=batch_id,
                mapping_version_id=mapping_id,
            )
        assert exc.value.code == "BATCH_USED_BY_VALIDATION"


async def test_租户锁拒绝与会话上下文不一致(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, _, _ = await _seed_tenant_and_batch(session, raw_rows=_rows())
        other_tenant = Tenant(slug=f"other-{uuid.uuid4().hex[:8]}", name="其他租户")
        session.add(other_tenant)
        await session.flush()

        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RuntimeError, match="租户锁请求与会话租户上下文不一致"):
            await lock_tenant_nowait(session, other_tenant.id)
        await session.rollback()
