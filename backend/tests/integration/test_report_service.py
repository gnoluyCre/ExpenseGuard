"""CP-F4.3 atomic report snapshots, idempotency, and recovery tests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.core.policies.canonical import canonical_binding_fingerprint
from app.core.reports.service import (
    ReportError,
    ReportInternalError,
    generate_report,
    load_report_snapshot,
)
from app.core.reviews.models import SamplingConfigParameters
from app.core.reviews.sampling import canonical_sampling_config_fingerprint
from app.core.rules import rule_config_fingerprint, validate_rule_definition
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.core.validation.batch_service import validate_batch
from app.db.base import utc_now
from app.db.models.audit import AuditLog
from app.db.models.batch import ExpenseRow, FileVersion, ParseStatus
from app.db.models.config import RuleConfig, SchemaMappingVersion
from app.db.models.findings import ReviewSamplingConfig
from app.db.models.policy import (
    PolicyClause,
    PolicyDocument,
    PolicyDocumentStatus,
    PolicyFamily,
    PolicySourceBlob,
    RulePolicyBinding,
)
from app.db.models.reports import (
    ReportCitation,
    ReportItem,
    ReportParseError,
    ReportRequest,
    ReportRun,
)
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_tenant_and_rules(
    session: AsyncSession, *, slug: str, with_sampling_config: bool = True
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = Tenant(slug=slug, name=f"{slug} tenant")
    session.add(tenant)
    await session.flush()
    bind_tenant(session.sync_session, tenant.id)
    user = AppUser(
        tenant_id=tenant.id,
        username="configurator",
        password_hash="test-only",
        role=Role.CONFIGURATOR,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    if with_sampling_config:
        sampling_parameters = SamplingConfigParameters(
            rate_bps=10_000,
            min_sample_size=1,
            max_sample_size=5_000,
        )
        session.add(
            ReviewSamplingConfig(
                tenant_id=tenant.id,
                version=1,
                rate_bps=sampling_parameters.rate_bps,
                min_sample_size=sampling_parameters.min_sample_size,
                max_sample_size=sampling_parameters.max_sample_size,
                algorithm_version=sampling_parameters.algorithm_version,
                config_fingerprint=canonical_sampling_config_fingerprint(sampling_parameters),
                idempotency_key_hash=_sha256("report-test-sampling-config"),
                request_fingerprint=_sha256("report-test-sampling-request"),
                created_by=user.id,
                change_reason="report integration test setup",
            )
        )
        await session.flush()
    mapping = SchemaMappingVersion(
        tenant_id=tenant.id,
        header_signature="1" * 64,
        version=1,
        config_fingerprint="2" * 64,
        availability_thresholds={},
        currency_aliases={},
        inference_config={"rules": []},
        backfilled_legacy=False,
        created_by=user.id,
    )
    session.add(mapping)
    await session.flush()
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
    for rule_id, raw in definitions:
        definition = validate_rule_definition(raw)
        session.add(
            RuleConfig(
                tenant_id=tenant.id,
                rule_id=rule_id,
                definition=definition.model_dump(mode="json"),
                version=1,
                effective_from=date(2020, 1, 1),
                is_active=True,
                config_fingerprint=rule_config_fingerprint(
                    rule_id=rule_id,
                    effective_from=date(2020, 1, 1),
                    definition=definition,
                ),
                created_by=user.id,
                backfilled_legacy=False,
            )
        )
    await session.flush()
    return tenant.id, user.id, mapping.id


def _normalized(mapping_id: uuid.UUID, *, amount: str, invoice_no: str) -> dict[str, object]:
    values: dict[str, object] = {
        "amount": amount,
        "expense_date": "2026-07-01",
        "expense_type": "差旅",
        "invoice_type": "增值税电子普通发票",
        "invoice_no": invoice_no,
        "invoice_title": "示例公司",
        "submission_date": "2026-07-15",
        "currency": "CNY",
    }
    return {
        "schema_version": 1,
        "mapping_version_id": str(mapping_id),
        **values,
        "field_provenance": {
            field: {
                "mode": "mapped",
                "source_columns": [field],
                "inference_rule_id": None,
            }
            for field in values
        },
    }


async def _seed_validated_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    with_sampling_config: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant_id, actor_id, mapping_id = await _seed_tenant_and_rules(
            session,
            slug=slug,
            with_sampling_config=with_sampling_config,
        )
        batch = FileVersion(
            tenant_id=tenant_id,
            filename="report.xlsx",
            content_hash=hashlib.sha256(slug.encode()).hexdigest(),
            row_count=3,
            uploaded_by=actor_id,
            mapping_version_id=mapping_id,
            parse_status=ParseStatus.PARSED_WITH_ERRORS,
            parsed_at=utc_now(),
        )
        session.add(batch)
        await session.flush()
        session.add_all(
            [
                ExpenseRow(
                    tenant_id=tenant_id,
                    file_version_id=batch.id,
                    row_no=2,
                    raw_json={"row": 2},
                    normalized_json=_normalized(mapping_id, amount="100", invoice_no="INV-001"),
                ),
                ExpenseRow(
                    tenant_id=tenant_id,
                    file_version_id=batch.id,
                    row_no=3,
                    raw_json={"row": 3},
                    normalized_json=_normalized(mapping_id, amount="2000", invoice_no="INV-001"),
                ),
                ExpenseRow(
                    tenant_id=tenant_id,
                    file_version_id=batch.id,
                    row_no=4,
                    raw_json={"row": 4},
                    normalized_json=None,
                    parse_error="金额格式无效",
                    parse_error_code="ROW_VALIDATION_FAILED",
                    parse_error_detail={
                        "schema_version": 1,
                        "mapping_version_id": str(mapping_id),
                        "errors": [
                            {
                                "field": "amount",
                                "code": "AMOUNT_INVALID_FORMAT",
                                "source_column": "金额",
                                "message": "金额格式无效",
                            }
                        ],
                    },
                ),
            ]
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await validate_batch(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch.id,
        )
        await session.commit()
    return tenant_id, actor_id, batch.id


async def _seed_policy_bindings(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    batch_id: uuid.UUID,
    bind_all: bool = True,
    invalid_fingerprint_ordinal: int | None = None,
) -> None:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        configs = tuple(
            (
                await session.scalars(
                    select(RuleConfig)
                    .where(RuleConfig.rule_id.in_(("expense.limit", "expense.invoice_duplicate")))
                    .order_by(RuleConfig.rule_id)
                )
            ).all()
        )
        del batch_id
        family = PolicyFamily(
            tenant_id=tenant_id,
            stable_key="travel-policy",
            display_name="差旅制度",
            created_by=actor_id,
        )
        session.add(family)
        await session.flush()
        source_hash = _sha256("policy source")
        blob = PolicySourceBlob(
            tenant_id=tenant_id,
            storage_key=f"{tenant_id}/policy.txt",
            mime_type="text/plain",
            size_bytes=13,
            content_sha256=source_hash,
            created_by=actor_id,
        )
        session.add(blob)
        await session.flush()
        document = PolicyDocument(
            tenant_id=tenant_id,
            title="差旅制度",
            version="2026-v1",
            effective_date=date(2026, 1, 1),
            expiry_date=None,
            source_filename="policy.txt",
            family_id=family.id,
            source_blob_id=blob.id,
            content_sha256=source_hash,
            mime_type="text/plain",
            size_bytes=13,
            extracted_text_sha256=source_hash,
            parser_version="test-v1",
            chunker_version="test-v1",
            status=PolicyDocumentStatus.PUBLISHED,
            created_by=actor_id,
            published_by=actor_id,
            published_at=utc_now(),
        )
        session.add(document)
        await session.flush()
        clauses = {
            "expense.limit": "第一条 报销金额不得超过1000元。",
            "expense.invoice_duplicate": "第二条 同一发票号码不得重复报销。",
        }
        for ordinal, config in enumerate(configs, start=1):
            text = clauses[config.rule_id]
            clause = PolicyClause(
                tenant_id=tenant_id,
                document_id=document.id,
                family_id=family.id,
                clause_no=str(ordinal),
                hierarchy_path=None,
                text=text,
                token_count=None,
                ordinal=ordinal,
                text_sha256=_sha256(text),
                source_locator_json={"paragraph": ordinal},
                source_start=None,
                source_end=None,
            )
            session.add(clause)
            await session.flush()
            if not bind_all and ordinal == 2:
                continue
            quote = text[4:-1]
            start = text.index(quote)
            quote_sha256 = _sha256(quote)
            clause_text_sha256 = _sha256(text)
            fingerprint = canonical_binding_fingerprint(
                tenant_id=tenant_id,
                rule_config_id=config.id,
                policy_family_id=family.id,
                policy_document_id=document.id,
                policy_clause_id=clause.id,
                quote_start=start,
                quote_end=start + len(quote),
                quote_sha256=quote_sha256,
                clause_text_sha256=clause_text_sha256,
                citation_order=1,
            )
            session.add(
                RulePolicyBinding(
                    tenant_id=tenant_id,
                    rule_config_id=config.id,
                    policy_family_id=family.id,
                    policy_document_id=document.id,
                    policy_clause_id=clause.id,
                    quote_start=start,
                    quote_end=start + len(quote),
                    quote=quote,
                    quote_sha256=quote_sha256,
                    clause_text_sha256=clause_text_sha256,
                    citation_order=1,
                    binding_fingerprint=(
                        "0" * 64 if invalid_fingerprint_ordinal == ordinal else fingerprint
                    ),
                    created_by=actor_id,
                )
            )
        await session.commit()


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_report原子装配多finding解析错误有序引用且稳定重放(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug="report-main")
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-main-key",
        )
        await session.commit()
    assert first.reused_existing is False
    assert first.stored_row_count == 3
    assert first.report_item_count == 2
    assert first.verified_citation_count == 2
    assert first.unavailable_citation_count == 0

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replay = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-main-key",
        )
        alias = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-alias-key",
        )
        await session.commit()
        snapshot = await load_report_snapshot(session, report_run_id=first.report_run_id)
        assert replay.report_run_id == alias.report_run_id == first.report_run_id
        assert [item.row_no for item in snapshot.items] == [3, 3]
        assert all(item.source_verdict == "flagged" for item in snapshot.items)
        assert [len(item.citations) for item in snapshot.items] == [1, 1]
        assert len(snapshot.parse_errors) == 1
        assert await _count(session, ReportRun) == 1
        assert await _count(session, ReportItem) == 2
        assert await _count(session, ReportCitation) == 2
        assert await _count(session, ReportParseError) == 1
        assert await _count(session, ReportRequest) == 2
        success_audits = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "batch.report_generate")
            )
            or 0
        )
        assert success_audits == 1


async def test_report缺binding整条finding引用不可用且不展示部分引用(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory, slug="report-unavailable"
    )
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
        bind_all=False,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-unavailable-key",
        )
        await session.commit()
        snapshot = await load_report_snapshot(session, report_run_id=result.report_run_id)
    assert result.verified_citation_count == 1
    assert result.unavailable_citation_count == 1
    unavailable = [item for item in snapshot.items if item.citation_status == "unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].source_verdict == "flagged"
    assert unavailable[0].attention_group == "high_attention"
    assert unavailable[0].requires_manual_citation is True
    assert unavailable[0].citations == ()
    assert unavailable[0].evidence_snapshot is not None
    assert (
        unavailable[0].evidence_snapshot["citation_unavailable_reason"]
        == "POLICY_BINDING_NOT_FOUND"
    )


async def test_report故障回滚全部snapshot并独立写安全失败审计(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory, slug="report-fault"
    )
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )

    def fail_after_success_audit(stage: str) -> None:
        if stage == "success_audit_written":
            raise RuntimeError("sentinel quote must not be persisted")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReportInternalError):
            await generate_report(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=batch_id,
                idempotency_key="report-fault-key",
                fault_hook=fail_after_success_audit,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await _count(session, ReportRun) == 0
        assert await _count(session, ReportRequest) == 0
        assert await _count(session, ReportItem) == 0
        assert await _count(session, ReportCitation) == 0
        assert await _count(session, ReportParseError) == 0
        audits = tuple(
            (
                await session.scalars(select(AuditLog).where(AuditLog.action.like("batch.report%")))
            ).all()
        )
        assert [audit.action for audit in audits] == ["batch.report_failed"]
        serialized = str(audits[0].payload_json)
        assert "sentinel quote" not in serialized
        assert set(audits[0].payload_json or {}) == {
            "error_category",
            "file_version_id",
            "sampling_reason_code",
        }
        assert (audits[0].payload_json or {})["sampling_reason_code"] == (
            "SAMPLING_PLAN_INTERNAL_ERROR"
        )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        recovered = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-fault-key",
        )
        await session.commit()
    assert recovered.reused_existing is False


async def test_report幂等key跨请求冲突且completed读取不依赖Qdrant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug="report-key")
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="globally-remembered-key",
        )
        await session.commit()
    other_tenant, other_actor, other_batch = await _seed_validated_batch(
        session_factory, slug="report-key-other"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, other_tenant)
        other = await generate_report(
            session,
            session_factory,
            tenant_id=other_tenant,
            actor_id=other_actor,
            file_version_id=other_batch,
            idempotency_key="globally-remembered-key",
        )
        await session.commit()
    assert other.report_run_id != created.report_run_id

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        snapshot = await load_report_snapshot(session, report_run_id=created.report_run_id)
        assert snapshot.summary.report_fingerprint == created.report_fingerprint
        # A request key is tenant-scoped but permanently bound within that tenant.
        with pytest.raises(ReportError, match="Idempotency-Key"):
            await generate_report(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=uuid.uuid4(),
                idempotency_key="globally-remembered-key",
            )


async def test_report租户与批次锁冲突稳定且零副作用(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug="report-lock")
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as holder, session_factory() as contender:
        bind_tenant(holder.sync_session, tenant_id)
        bind_tenant(contender.sync_session, tenant_id)
        await lock_tenant_nowait(holder, tenant_id)
        with pytest.raises(ReportError) as exc:
            await generate_report(
                contender,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=batch_id,
                idempotency_key="report-tenant-lock-key",
            )
        assert exc.value.code == "REPORT_GENERATION_IN_PROGRESS"
        await contender.rollback()
        await holder.rollback()

    async with session_factory() as file_holder, session_factory() as contender:
        bind_tenant(file_holder.sync_session, tenant_id)
        bind_tenant(contender.sync_session, tenant_id)
        locked = await file_holder.scalar(
            select(FileVersion).where(FileVersion.id == batch_id).with_for_update(nowait=True)
        )
        assert locked is not None
        with pytest.raises(ReportError) as exc:
            await generate_report(
                contender,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=batch_id,
                idempotency_key="report-file-lock-key",
            )
        assert exc.value.code == "REPORT_GENERATION_IN_PROGRESS"
        await contender.rollback()
        await file_holder.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await _count(session, ReportRun) == 0
        assert await _count(session, ReportRequest) == 0
        assert await _count(session, ReportItem) == 0


async def test_completed_report跨租户不可见(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory, slug="report-isolation-a"
    )
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-isolation-key",
        )
        await session.commit()

    other_tenant, _, _ = await _seed_tenant_only(session_factory, slug="report-isolation-b")
    async with session_factory() as session:
        bind_tenant(session.sync_session, other_tenant)
        with pytest.raises(NotFoundError) as exc:
            await load_report_snapshot(session, report_run_id=created.report_run_id)
        assert getattr(exc.value, "code", None) == "REPORT_NOT_FOUND"
        assert await _count(session, ReportRun) == 0
        assert await _count(session, ReportItem) == 0
        assert await _count(session, ReportCitation) == 0


async def test_report_rejects_cross_tenant_actor_without_failure_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _, batch_id = await _seed_validated_batch(session_factory, slug="report-actor-owner")
    _, other_actor_id, _ = await _seed_tenant_only(session_factory, slug="report-actor-other")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(NotFoundError) as exc:
            await generate_report(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=other_actor_id,
                file_version_id=batch_id,
                idempotency_key="report-cross-tenant-actor-key",
            )
        assert exc.value.code == "REPORT_ACTOR_NOT_FOUND"
        assert await _count(session, ReportRun) == 0
        failure_audits = int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "batch.report_failed")
            )
            or 0
        )
        assert failure_audits == 0


async def test_report_rejects_invalid_binding_fingerprint_as_unavailable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory, slug="report-binding-fingerprint"
    )
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
        invalid_fingerprint_ordinal=1,
    )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key="report-invalid-binding-fingerprint-key",
        )
        await session.commit()
        snapshot = await load_report_snapshot(session, report_run_id=result.report_run_id)

    unavailable = [item for item in snapshot.items if item.citation_status == "unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0].citations == ()
    assert unavailable[0].evidence_snapshot is not None
    assert (
        unavailable[0].evidence_snapshot["citation_unavailable_reason"]
        == "POLICY_BINDING_INTEGRITY_FAILED"
    )


async def _seed_tenant_only(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        values = await _seed_tenant_and_rules(session, slug=slug)
        await session.commit()
        return values


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
