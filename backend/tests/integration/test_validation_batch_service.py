"""CP-F3.3 快照、整批编排、幂等与失败审计的 PostgreSQL 测试。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.validation.batch_service as batch_service
from app.core.rules import rule_config_fingerprint, validate_rule_definition
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.core.validation.batch_service import (
    BatchValidationError,
    BatchValidationInternalError,
    validate_batch,
)
from app.core.validation.rule_service import save_rule_version
from app.db.base import utc_now
from app.db.models.audit import AuditLog
from app.db.models.batch import ExpenseRow, FileVersion, ParseStatus, RevisionReason, RowResult
from app.db.models.config import RuleConfig, SchemaMappingVersion
from app.db.models.findings import Finding
from app.db.models.tenancy import AppUser, Role, Tenant
from app.db.models.validation import ValidationDependency, ValidationRun

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_tenant(
    session: AsyncSession,
    *,
    slug: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = Tenant(slug=slug, name=f"{slug} 租户")
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
    mapping = SchemaMappingVersion(
        tenant_id=tenant.id,
        header_signature=uuid.uuid4().hex.ljust(64, "0")[:64],
        version=1,
        config_fingerprint=uuid.uuid4().hex.ljust(64, "0")[:64],
        availability_thresholds={
            "available_min_non_null_rate": "0.8000",
            "inferred_min_success_rate": "0.8000",
        },
        currency_aliases={},
        inference_config={"rules": []},
        backfilled_legacy=False,
        created_by=user.id,
    )
    session.add(mapping)
    await session.flush()
    await _seed_rules(session, tenant_id=tenant.id, user_id=user.id)
    return tenant.id, user.id, mapping.id


async def _seed_rules(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    definitions: tuple[tuple[str, dict[str, object]], ...] = (
        (
            "expense.limit",
            {
                "schema_version": 1,
                "kind": "limit",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
                "thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "1000"}],
            },
        ),
        (
            "expense.invoice_type",
            {
                "schema_version": 1,
                "kind": "invoice_type",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
                "allowances": [
                    {
                        "expense_type": "差旅",
                        "allowed_invoice_types": ["增值税电子普通发票"],
                    }
                ],
            },
        ),
        (
            "expense.timeliness",
            {
                "schema_version": 1,
                "kind": "timeliness",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
                "policies": [{"expense_type": "差旅", "max_calendar_days": 30}],
            },
        ),
        (
            "expense.invoice_title",
            {
                "schema_version": 1,
                "kind": "invoice_title",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
                "allowed_titles": ["示例公司"],
            },
        ),
        (
            "expense.invoice_duplicate",
            {
                "schema_version": 1,
                "kind": "invoice_duplicate",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
            },
        ),
    )
    effective_from = date(2020, 1, 1)
    for rule_id, raw_definition in definitions:
        definition = validate_rule_definition(raw_definition)
        session.add(
            RuleConfig(
                tenant_id=tenant_id,
                rule_id=rule_id,
                definition=definition.model_dump(mode="json"),
                version=1,
                effective_from=effective_from,
                is_active=True,
                config_fingerprint=rule_config_fingerprint(
                    rule_id=rule_id,
                    effective_from=effective_from,
                    definition=definition,
                ),
                created_by=user_id,
                backfilled_legacy=False,
            )
        )
    await session.flush()


def _normalized(
    mapping_id: uuid.UUID,
    *,
    amount: str = "100",
    invoice_no: str | None = "INV-001",
) -> dict[str, object]:
    values: dict[str, object] = {
        "amount": amount,
        "expense_date": "2026-07-01",
        "expense_type": "差旅",
        "invoice_type": "增值税电子普通发票",
        "invoice_title": "示例公司",
        "submission_date": "2026-07-15",
        "currency": "CNY",
    }
    if invoice_no is not None:
        values["invoice_no"] = invoice_no
    provenance = {
        field: {"mode": "mapped", "source_columns": [field], "inference_rule_id": None}
        for field in values
    }
    return {
        "schema_version": 1,
        "mapping_version_id": str(mapping_id),
        **values,
        "field_provenance": provenance,
    }


async def _seed_batch(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    mapping_id: uuid.UUID,
    content_hash: str,
    rows: Sequence[dict[str, object]],
    revision_no: int = 1,
    source_id: uuid.UUID | None = None,
    root_id: uuid.UUID | None = None,
) -> FileVersion:
    derived = revision_no > 1
    batch = FileVersion(
        tenant_id=tenant_id,
        filename=f"batch-r{revision_no}.xlsx",
        content_hash=content_hash,
        row_count=len(rows),
        uploaded_by=user_id,
        mapping_version_id=mapping_id,
        parse_status=ParseStatus.PARSED,
        parsed_at=utc_now(),
        revision_no=revision_no,
        source_file_version_id=source_id,
        root_file_version_id=root_id,
        revision_reason=RevisionReason.RULESET_CHANGE if derived else None,
        revision_request_key_hash="a" * 64 if derived else None,
        revision_request_fingerprint="b" * 64 if derived else None,
    )
    session.add(batch)
    await session.flush()
    session.add_all(
        ExpenseRow(
            tenant_id=tenant_id,
            file_version_id=batch.id,
            row_no=index + 2,
            raw_json={"row": index + 2},
            normalized_json=normalized,
        )
        for index, normalized in enumerate(rows)
    )
    await session.flush()
    return batch


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_validate使用纯核心写入快照finding与row_result且重复调用无副作用(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(session, slug="validate-main")
        batch = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="1" * 64,
            rows=(
                _normalized(mapping_id, amount="100", invoice_no="INV-001"),
                _normalized(mapping_id, amount="2000", invoice_no="INV-001"),
            ),
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await validate_batch(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=user_id,
            file_version_id=batch.id,
        )
        await session.commit()
    assert first.reused_existing is False
    assert (first.passed_count, first.flagged_count, first.manual_review_count) == (1, 1, 0)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        row_results = tuple(
            (
                await session.scalars(
                    select(RowResult)
                    .where(RowResult.file_version_id == batch.id)
                    .order_by(RowResult.row_no)
                )
            ).all()
        )
        findings = tuple(
            (
                await session.scalars(
                    select(Finding)
                    .where(Finding.file_version_id == batch.id)
                    .order_by(Finding.rule_id)
                )
            ).all()
        )
        assert [item.verdict for item in row_results] == ["passed", "flagged"]
        assert all(item.rule_version == first.ruleset_fingerprint for item in row_results)
        assert {item.kind for item in findings} == {"limit_exceeded", "invoice_duplicate"}
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await save_rule_version(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            rule_id="expense.limit",
            effective_from=date(2026, 1, 1),
            definition={
                "schema_version": 1,
                "kind": "limit",
                "enabled": True,
                "require_direct": False,
                "exemptions": [],
                "thresholds": [{"expense_type": "差旅", "currency": "CNY", "max_amount": "50"}],
            },
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        before = (
            await _count(session, ValidationRun),
            await _count(session, RowResult),
            await _count(session, Finding),
            await _count(session, AuditLog),
        )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        second = await validate_batch(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=user_id,
            file_version_id=batch.id,
        )
        await session.commit()
        after = (
            await _count(session, ValidationRun),
            await _count(session, RowResult),
            await _count(session, Finding),
            await _count(session, AuditLog),
        )
    assert second.reused_existing is True
    assert second.ruleset_fingerprint == first.ruleset_fingerprint
    assert before == after


async def test_规则family缺失时整批拒绝且不产生结果(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(session, slug="validate-ruleset")
        duplicate_rule = await session.scalar(
            select(RuleConfig).where(RuleConfig.rule_id == "expense.invoice_duplicate")
        )
        assert duplicate_rule is not None
        await session.delete(duplicate_rule)
        batch = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="5" * 64,
            rows=(_normalized(mapping_id),),
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(BatchValidationError) as exc:
            await validate_batch(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=user_id,
                file_version_id=batch.id,
            )
        assert exc.value.code == "RULESET_INVALID"
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await _count(session, ValidationRun) == 0
        assert await _count(session, RowResult) == 0
        assert await _count(session, Finding) == 0


async def test_同租户锁冲突稳定返回校验进行中且不compute(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(session, slug="validate-lock")
        batch = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="6" * 64,
            rows=(_normalized(mapping_id),),
        )
        await session.commit()

    async with session_factory() as lock_session, session_factory() as validate_session:
        bind_tenant(lock_session.sync_session, tenant_id)
        bind_tenant(validate_session.sync_session, tenant_id)
        await lock_tenant_nowait(lock_session, tenant_id)
        with pytest.raises(BatchValidationError) as exc:
            await validate_batch(
                validate_session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=user_id,
                file_version_id=batch.id,
            )
        assert exc.value.code == "BATCH_VALIDATION_IN_PROGRESS"
        await validate_session.rollback()
        await lock_session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await _count(session, ValidationRun) == 0
        assert await _count(session, RowResult) == 0


async def test_duplicate只使用其他lineage最高已解析revision并冻结dependency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(session, slug="validate-history")
        root = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="2" * 64,
            rows=(_normalized(mapping_id, invoice_no="OLD"),),
        )
        derived = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="2" * 64,
            rows=(_normalized(mapping_id, invoice_no="NEW"),),
            revision_no=2,
            source_id=root.id,
            root_id=root.id,
        )
        target = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="3" * 64,
            rows=(_normalized(mapping_id, invoice_no="OLD"),),
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await validate_batch(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=user_id,
            file_version_id=target.id,
        )
        await session.commit()
        dependency_ids = tuple(
            (await session.scalars(select(ValidationDependency.depended_file_version_id))).all()
        )
        duplicate_findings = tuple(
            (
                await session.scalars(select(Finding).where(Finding.kind == "invoice_duplicate"))
            ).all()
        )
    assert result.passed_count == 1
    assert dependency_ids == (derived.id,)
    assert duplicate_findings == ()


async def test_当前root的派生revision不与自身lineage判重(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(session, slug="validate-lineage")
        root = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="4" * 64,
            rows=(_normalized(mapping_id, invoice_no="SAME"),),
        )
        derived = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash="4" * 64,
            rows=(_normalized(mapping_id, invoice_no="SAME"),),
            revision_no=2,
            source_id=root.id,
            root_id=root.id,
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await validate_batch(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=user_id,
            file_version_id=derived.id,
        )
        await session.commit()
        assert await _count(session, ValidationDependency) == 0
        assert await _count(session, Finding) == 0
    assert result.passed_count == 1


async def test_跨租户相同发票号不参与查重或dependency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        first_tenant, first_user, first_mapping = await _seed_tenant(
            session, slug="validate-tenant-a"
        )
        await _seed_batch(
            session,
            tenant_id=first_tenant,
            user_id=first_user,
            mapping_id=first_mapping,
            content_hash="7" * 64,
            rows=(_normalized(first_mapping, invoice_no="CROSS-TENANT"),),
        )
        await session.commit()

    async with session_factory() as session:
        second_tenant, second_user, second_mapping = await _seed_tenant(
            session, slug="validate-tenant-b"
        )
        target = await _seed_batch(
            session,
            tenant_id=second_tenant,
            user_id=second_user,
            mapping_id=second_mapping,
            content_hash="8" * 64,
            rows=(_normalized(second_mapping, invoice_no="CROSS-TENANT"),),
        )
        await session.commit()

    async with session_factory() as first_lock, session_factory() as session:
        bind_tenant(first_lock.sync_session, first_tenant)
        bind_tenant(session.sync_session, second_tenant)
        await lock_tenant_nowait(first_lock, first_tenant)
        result = await validate_batch(
            session,
            session_factory,
            tenant_id=second_tenant,
            actor_id=second_user,
            file_version_id=target.id,
        )
        await session.commit()
        assert await _count(session, ValidationDependency) == 0
        assert await _count(session, Finding) == 0
        await first_lock.rollback()
    assert result.passed_count == 1


@pytest.mark.parametrize(
    "fault_target",
    ["_persist_dependencies", "_persist_finding", "process_row_once", "_summary"],
)
async def test_系统故障整批回滚且独立失败审计无PII(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    fault_target: str,
) -> None:
    async with session_factory() as session:
        tenant_id, user_id, mapping_id = await _seed_tenant(
            session, slug=f"validate-fault-{fault_target}"
        )
        batch = await _seed_batch(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            mapping_id=mapping_id,
            content_hash=uuid.uuid4().hex.ljust(64, "0")[:64],
            rows=(_normalized(mapping_id, amount="2000", invoice_no="FAULT-INVOICE"),),
        )
        await session.commit()

    async def _explode_async(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected private row data")

    def _explode_sync(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected private row data")

    if fault_target == "process_row_once":
        original_process_row_once = batch_service.process_row_once

        async def _explode_after_row_result(*args: object, **kwargs: object) -> None:
            await original_process_row_once(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected private row data")

        monkeypatch.setattr(batch_service, fault_target, _explode_after_row_result)
    else:
        monkeypatch.setattr(
            batch_service,
            fault_target,
            _explode_sync if fault_target == "_summary" else _explode_async,
        )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(BatchValidationInternalError):
            await validate_batch(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=user_id,
                file_version_id=batch.id,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await _count(session, ValidationRun) == 0
        assert await _count(session, ValidationDependency) == 0
        assert await _count(session, RowResult) == 0
        assert await _count(session, Finding) == 0
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "batch.validate_failed")
                )
            ).all()
        )
    assert len(audits) == 1
    assert set(audits[0].payload_json or {}) == {
        "file_version_id",
        "mapping_version_id",
        "ruleset_fingerprint",
        "error_category",
    }
    assert "private" not in str(audits[0].payload_json)
