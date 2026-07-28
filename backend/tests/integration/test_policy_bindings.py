"""CP-F4.3 exact binding persistence and isolation tests."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.policies.bindings import (
    BindingSelection,
    BindingServiceError,
    save_rule_policy_bindings,
)
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.audit import AuditLog
from app.db.models.config import RuleConfig
from app.db.models.policy import (
    PolicyClause,
    PolicyDocument,
    PolicyDocumentStatus,
    PolicyFamily,
    PolicySourceBlob,
    RulePolicyBinding,
)
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


@dataclass(frozen=True)
class BindingSeed:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    rule_id: uuid.UUID
    document_id: uuid.UUID
    clause_ids: tuple[uuid.UUID, ...]
    clause_texts: tuple[str, ...]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _add_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    suffix: str,
    effective: date = date(2026, 1, 1),
    expiry: date | None = date(2027, 1, 1),
    status: PolicyDocumentStatus = PolicyDocumentStatus.PUBLISHED,
) -> tuple[PolicyDocument, tuple[PolicyClause, ...]]:
    family = PolicyFamily(
        tenant_id=tenant_id,
        stable_key=f"travel-{suffix}",
        display_name=f"差旅制度 {suffix}",
        created_by=user_id,
    )
    blob = PolicySourceBlob(
        tenant_id=tenant_id,
        storage_key=f"policies/{tenant_id}/{suffix}.txt",
        mime_type="text/plain",
        size_bytes=100,
        content_sha256=_sha(f"blob-{suffix}"),
        created_by=user_id,
    )
    session.add_all([family, blob])
    await session.flush()
    published = status is PolicyDocumentStatus.PUBLISHED
    document = PolicyDocument(
        tenant_id=tenant_id,
        title=f"差旅制度 {suffix}",
        version=suffix,
        effective_date=effective,
        expiry_date=expiry,
        source_filename=f"{suffix}.txt",
        family_id=family.id,
        source_blob_id=blob.id,
        content_sha256=blob.content_sha256,
        mime_type="text/plain",
        size_bytes=100,
        extracted_text_sha256=_sha(f"extracted-{suffix}"),
        parser_version="policy-parser-v1",
        chunker_version="policy-chunker-v1",
        status=status,
        created_by=user_id,
        published_by=user_id if published else None,
        published_at=utc_now() if published else None,
    )
    session.add(document)
    await session.flush()
    texts = (
        f"第一条 {suffix} 交通费必须符合标准。",
        f"第二条 {suffix} 住宿费必须符合标准。",
    )
    clauses = tuple(
        PolicyClause(
            tenant_id=tenant_id,
            document_id=document.id,
            family_id=family.id,
            clause_no=f"第{ordinal}条",
            hierarchy_path=None,
            text=text,
            ordinal=ordinal,
            text_sha256=_sha(text),
            source_locator_json={"kind": "text", "start": 0, "end": len(text)},
            source_start=0,
            source_end=len(text),
        )
        for ordinal, text in enumerate(texts, start=1)
    )
    session.add_all(clauses)
    await session.flush()
    return document, clauses


async def _seed(session_factory: async_sessionmaker[AsyncSession], *, slug: str) -> BindingSeed:
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        session.add(tenant)
        await session.flush()
        bind_tenant(session.sync_session, tenant.id)
        user = AppUser(
            tenant_id=tenant.id,
            username=f"{slug}-configurator",
            password_hash="test-only",
            role=Role.CONFIGURATOR,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        rule = RuleConfig(
            tenant_id=tenant.id,
            rule_id="expense.limit",
            definition={"schema_version": 1, "kind": "limit"},
            version=1,
            effective_from=date(2020, 1, 1),
            is_active=True,
            config_fingerprint=_sha(f"rule-{slug}"),
            created_by=user.id,
            backfilled_legacy=False,
        )
        session.add(rule)
        await session.flush()
        document, clauses = await _add_document(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            suffix=f"{slug}-2026",
        )
        await session.commit()
        return BindingSeed(
            tenant_id=tenant.id,
            user_id=user.id,
            rule_id=rule.id,
            document_id=document.id,
            clause_ids=tuple(item.id for item in clauses),
            clause_texts=tuple(item.text for item in clauses),
        )


def _selection(seed: BindingSeed, *, index: int, order: int) -> BindingSelection:
    text = seed.clause_texts[index]
    quote = text[4:12]
    return BindingSelection(
        policy_document_id=seed.document_id,
        policy_clause_id=seed.clause_ids[index],
        quote_start=4,
        quote_end=12,
        exact_quote=quote,
        citation_order=order,
    )


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_binding_selection_validation_does_not_echo_candidate_quote() -> None:
    sentinel = "SECRET_INVALID_QUOTE_DO_NOT_ECHO_8521"
    with pytest.raises(ValidationError) as exc:
        BindingSelection(
            policy_document_id=uuid.uuid4(),
            policy_clause_id=uuid.uuid4(),
            quote_start=9,
            quote_end=8,
            exact_quote=sentinel,
            citation_order=1,
        )
    assert sentinel not in str(exc.value)
    assert sentinel not in repr(exc.value)


async def test_binding_set_is_exact_ordered_audited_once_and_reused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-happy")
    request = (_selection(seed, index=1, order=2), _selection(seed, index=0, order=1))

    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        first = await save_rule_policy_bindings(
            session,
            tenant_id=seed.tenant_id,
            created_by=seed.user_id,
            rule_config_id=seed.rule_id,
            expense_date=date(2026, 7, 1),
            selections=request,
        )
        await session.commit()
    assert first.created is True
    assert [item.citation_order for item in first.bindings] == [1, 2]

    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        second = await save_rule_policy_bindings(
            session,
            tenant_id=seed.tenant_id,
            created_by=seed.user_id,
            rule_config_id=seed.rule_id,
            expense_date=date(2026, 7, 1),
            selections=request,
        )
        await session.commit()
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "policy.binding_create")
                )
            ).all()
        )
        assert await _count(session, RulePolicyBinding) == 2
    assert second.created is False
    assert tuple(item.id for item in second.bindings) == tuple(item.id for item in first.bindings)
    assert len(audits) == 1
    payload = audits[0].payload_json or {}
    assert set(payload) == {
        "rule_config_id",
        "binding_ids",
        "binding_fingerprints",
        "citation_count",
    }
    assert all(text not in str(payload) for text in seed.clause_texts)


@pytest.mark.parametrize(
    "orders",
    [(), (2,), (1, 3), (1, 1)],
)
async def test_binding_order_must_be_one_to_three_contiguous_and_is_atomic(
    session_factory: async_sessionmaker[AsyncSession], orders: tuple[int, ...]
) -> None:
    seed = await _seed(session_factory, slug=f"binding-order-{len(orders)}-{sum(orders)}")
    selections = tuple(
        _selection(seed, index=index % 2, order=order) for index, order in enumerate(orders)
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(BindingServiceError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=selections,
            )
        assert exc.value.code == "POLICY_BINDING_ORDER_INVALID"
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        assert await _count(session, RulePolicyBinding) == 0
        assert await _count(session, AuditLog) == 0


@pytest.mark.parametrize("expense_date", [date(2025, 12, 31), date(2027, 1, 1)])
async def test_binding_enforces_half_open_document_date(
    session_factory: async_sessionmaker[AsyncSession], expense_date: date
) -> None:
    seed = await _seed(session_factory, slug=f"binding-date-{expense_date.isoformat()}")
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(NotFoundError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=expense_date,
                selections=(_selection(seed, index=0, order=1),),
            )
        assert getattr(exc.value, "code", None) == "POLICY_NOT_FOUND"
        await session.rollback()


async def test_unpublished_and_cross_tenant_clause_are_hidden(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _seed(session_factory, slug="binding-tenant-a")
    second = await _seed(session_factory, slug="binding-tenant-b")
    foreign = BindingSelection(
        policy_document_id=second.document_id,
        policy_clause_id=second.clause_ids[0],
        quote_start=4,
        quote_end=12,
        exact_quote=second.clause_texts[0][4:12],
        citation_order=1,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, first.tenant_id)
        with pytest.raises(NotFoundError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=first.tenant_id,
                created_by=first.user_id,
                rule_config_id=first.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(foreign,),
            )
        assert getattr(exc.value, "code", None) == "POLICY_NOT_FOUND"
        await session.rollback()


async def test_unpublished_document_cannot_be_bound(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-unpublished")
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        document, clauses = await _add_document(
            session,
            tenant_id=seed.tenant_id,
            user_id=seed.user_id,
            suffix="draft",
            status=PolicyDocumentStatus.DRAFT,
        )
        await session.commit()
        text = clauses[0].text
        selection = BindingSelection(
            policy_document_id=document.id,
            policy_clause_id=clauses[0].id,
            quote_start=4,
            quote_end=12,
            exact_quote=text[4:12],
            citation_order=1,
        )
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(NotFoundError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(selection,),
            )
        assert exc.value.code == "POLICY_NOT_FOUND"
        await session.rollback()


async def test_same_tenant_document_clause_mismatch_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-identity")
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        other_document, _ = await _add_document(
            session,
            tenant_id=seed.tenant_id,
            user_id=seed.user_id,
            suffix="identity-other",
        )
        await session.commit()
    mismatched = BindingSelection(
        policy_document_id=other_document.id,
        policy_clause_id=seed.clause_ids[0],
        quote_start=4,
        quote_end=12,
        exact_quote=seed.clause_texts[0][4:12],
        citation_order=1,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(NotFoundError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(mismatched,),
            )
        assert exc.value.code == "POLICY_NOT_FOUND"
        await session.rollback()


async def test_non_configurator_cannot_confirm_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-role")
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        auditor = AppUser(
            tenant_id=seed.tenant_id,
            username="auditor",
            password_hash="test-only",
            role=Role.AUDITOR,
            is_active=True,
        )
        session.add(auditor)
        await session.commit()
        auditor_id = auditor.id
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(PermissionDeniedError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=auditor_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(_selection(seed, index=0, order=1),),
            )
        assert exc.value.code == "PERMISSION_DENIED"
        await session.rollback()


async def test_failed_exact_quote_is_not_persisted_audited_or_logged(
    session_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    seed = await _seed(session_factory, slug="binding-secret")
    sentinel = "SECRET_QUOTE_DO_NOT_PERSIST_7b987"
    selection = BindingSelection(
        policy_document_id=seed.document_id,
        policy_clause_id=seed.clause_ids[0],
        quote_start=4,
        quote_end=12,
        exact_quote=sentinel,
        citation_order=1,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(BindingServiceError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(selection,),
            )
        assert exc.value.code == "QUOTE_NOT_EXACT"
        assert sentinel not in str(exc.value)
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        assert await _count(session, RulePolicyBinding) == 0
        assert await _count(session, AuditLog) == 0
    assert sentinel not in caplog.text


async def test_tenant_lock_conflict_is_stable_and_has_no_side_effect(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-lock")
    async with session_factory() as lock_session, session_factory() as write_session:
        bind_tenant(lock_session.sync_session, seed.tenant_id)
        bind_tenant(write_session.sync_session, seed.tenant_id)
        await lock_tenant_nowait(lock_session, seed.tenant_id)
        with pytest.raises(BindingServiceError) as exc:
            await save_rule_policy_bindings(
                write_session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(_selection(seed, index=0, order=1),),
            )
        assert exc.value.code == "POLICY_CHANGE_IN_PROGRESS"
        await write_session.rollback()
        await lock_session.rollback()


async def test_existing_same_order_from_another_family_is_ambiguous(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await _seed(session_factory, slug="binding-ambiguous")
    requested = _selection(seed, index=0, order=1)
    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        await save_rule_policy_bindings(
            session,
            tenant_id=seed.tenant_id,
            created_by=seed.user_id,
            rule_config_id=seed.rule_id,
            expense_date=date(2026, 7, 1),
            selections=(requested,),
        )
        other_document, other_clauses = await _add_document(
            session,
            tenant_id=seed.tenant_id,
            user_id=seed.user_id,
            suffix="other-family",
        )
        other_clause = other_clauses[0]
        other_quote = other_clause.text[4:12]
        session.add(
            RulePolicyBinding(
                tenant_id=seed.tenant_id,
                rule_config_id=seed.rule_id,
                policy_family_id=other_document.family_id,
                policy_document_id=other_document.id,
                policy_clause_id=other_clause.id,
                quote_start=4,
                quote_end=12,
                quote=other_quote,
                quote_sha256=_sha(other_quote),
                clause_text_sha256=other_clause.text_sha256,
                citation_order=1,
                binding_fingerprint=_sha("other-binding"),
                created_by=seed.user_id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, seed.tenant_id)
        with pytest.raises(BindingServiceError) as exc:
            await save_rule_policy_bindings(
                session,
                tenant_id=seed.tenant_id,
                created_by=seed.user_id,
                rule_config_id=seed.rule_id,
                expense_date=date(2026, 7, 1),
                selections=(requested,),
            )
        assert exc.value.code == "POLICY_BINDING_AMBIGUOUS"
        await session.rollback()
