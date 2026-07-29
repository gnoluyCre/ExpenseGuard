"""Tenant-scoped read models for the policy and binding desktop workflow."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.policies.candidates import BindingQuery
from app.core.rules import validate_rule_definition
from app.db.models.config import RuleConfig
from app.db.models.policy import (
    PolicyClause,
    PolicyDocument,
    PolicyDocumentIndex,
    PolicyDocumentIndexStatus,
    PolicyFamily,
    PolicyIndexGeneration,
    PolicyIndexGenerationStatus,
    RulePolicyBinding,
)


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class PolicyDocumentListItem(_View):
    id: uuid.UUID
    title: str
    version: str
    effective_date: date
    expiry_date: date | None
    content_sha256: str | None
    status: str
    index_status: str | None
    index_completed_points: int | None
    index_expected_points: int | None
    failure_code: str | None
    created_at: datetime


class PolicyFamilyListItem(_View):
    id: uuid.UUID
    stable_key: str
    display_name: str
    created_at: datetime
    documents: tuple[PolicyDocumentListItem, ...]


class PolicyClauseView(_View):
    id: uuid.UUID
    clause_no: str
    hierarchy_path: str | None
    text: str
    text_sha256: str | None
    ordinal: int | None
    source_locator: dict[str, object] | None


class PolicyDocumentView(PolicyDocumentListItem):
    family_id: uuid.UUID
    family_stable_key: str
    source_filename: str | None
    mime_type: str | None
    size_bytes: int | None
    clauses: tuple[PolicyClauseView, ...]


class BindingHistoryView(_View):
    id: uuid.UUID
    rule_config_id: uuid.UUID
    citation_order: int
    binding_fingerprint: str
    family_id: uuid.UUID
    family_stable_key: str
    document_id: uuid.UUID
    document_title: str
    document_version: str
    effective_date: date
    expiry_date: date | None
    clause_id: uuid.UUID
    clause_no: str
    hierarchy_path: str | None
    clause_text: str
    quote: str
    quote_start: int
    quote_end: int
    quote_sha256: str
    created_by: uuid.UUID
    created_at: datetime


async def list_policy_families(db: AsyncSession) -> tuple[PolicyFamilyListItem, ...]:
    families = tuple(
        (await db.scalars(select(PolicyFamily).order_by(PolicyFamily.stable_key))).all()
    )
    documents = tuple(
        (
            await db.scalars(
                select(PolicyDocument)
                .where(PolicyDocument.family_id.is_not(None))
                .order_by(
                    PolicyDocument.family_id,
                    PolicyDocument.effective_date.desc(),
                    PolicyDocument.version.desc(),
                    PolicyDocument.id,
                )
            )
        ).all()
    )
    indexes = await _latest_document_indexes(db)
    by_family: dict[uuid.UUID, list[PolicyDocumentListItem]] = {}
    for document in documents:
        if document.family_id is None:
            continue
        by_family.setdefault(document.family_id, []).append(_document_item(document, indexes))
    return tuple(
        PolicyFamilyListItem(
            id=family.id,
            stable_key=family.stable_key,
            display_name=family.display_name,
            created_at=family.created_at,
            documents=tuple(by_family.get(family.id, ())),
        )
        for family in families
    )


async def get_policy_document(db: AsyncSession, document_id: uuid.UUID) -> PolicyDocumentView:
    row = (
        await db.execute(
            select(PolicyDocument, PolicyFamily)
            .join(PolicyFamily, PolicyFamily.id == PolicyDocument.family_id)
            .where(PolicyDocument.id == document_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="制度文档不存在")
    document, family = row._tuple()
    clauses = tuple(
        (
            await db.scalars(
                select(PolicyClause)
                .where(PolicyClause.document_id == document.id)
                .order_by(PolicyClause.ordinal, PolicyClause.id)
            )
        ).all()
    )
    indexes = await _latest_document_indexes(db)
    item = _document_item(document, indexes)
    return PolicyDocumentView(
        **item.model_dump(),
        family_id=family.id,
        family_stable_key=family.stable_key,
        source_filename=document.source_filename,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        clauses=tuple(
            PolicyClauseView(
                id=clause.id,
                clause_no=clause.clause_no,
                hierarchy_path=clause.hierarchy_path,
                text=clause.text,
                text_sha256=clause.text_sha256,
                ordinal=clause.ordinal,
                source_locator=(
                    dict(clause.source_locator_json)
                    if clause.source_locator_json is not None
                    else None
                ),
            )
            for clause in clauses
        ),
    )


async def list_rule_bindings(
    db: AsyncSession, rule_config_id: uuid.UUID
) -> tuple[BindingHistoryView, ...]:
    rule = await db.get(RuleConfig, rule_config_id)
    if rule is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="规则版本不存在")
    rows = (
        await db.execute(
            select(RulePolicyBinding, PolicyFamily, PolicyDocument, PolicyClause)
            .join(PolicyFamily, PolicyFamily.id == RulePolicyBinding.policy_family_id)
            .join(PolicyDocument, PolicyDocument.id == RulePolicyBinding.policy_document_id)
            .join(PolicyClause, PolicyClause.id == RulePolicyBinding.policy_clause_id)
            .where(RulePolicyBinding.rule_config_id == rule_config_id)
            .order_by(RulePolicyBinding.created_at.desc(), RulePolicyBinding.citation_order)
        )
    ).all()
    return tuple(
        BindingHistoryView(
            id=binding.id,
            rule_config_id=binding.rule_config_id,
            citation_order=binding.citation_order,
            binding_fingerprint=binding.binding_fingerprint,
            family_id=family.id,
            family_stable_key=family.stable_key,
            document_id=document.id,
            document_title=document.title,
            document_version=document.version,
            effective_date=document.effective_date,
            expiry_date=document.expiry_date,
            clause_id=clause.id,
            clause_no=clause.clause_no,
            hierarchy_path=clause.hierarchy_path,
            clause_text=clause.text,
            quote=binding.quote,
            quote_start=binding.quote_start,
            quote_end=binding.quote_end,
            quote_sha256=binding.quote_sha256,
            created_by=binding.created_by,
            created_at=binding.created_at,
        )
        for binding, family, document, clause in (row._tuple() for row in rows)
    )


async def get_active_generation(db: AsyncSession) -> PolicyIndexGeneration:
    generation = await db.scalar(
        select(PolicyIndexGeneration).where(
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE
        )
    )
    if generation is None:
        raise NotFoundError(code="POLICY_INDEX_UNAVAILABLE", message="当前没有可用制度索引")
    return generation


async def binding_query_for_rule(db: AsyncSession, rule_config_id: uuid.UUID) -> BindingQuery:
    rule = await db.get(RuleConfig, rule_config_id)
    if rule is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="规则版本不存在")
    definition = validate_rule_definition(rule.definition)
    reason_codes = {
        "limit": "limit_exceeded",
        "invoice_type": "invoice_type_not_allowed",
        "timeliness": "claim_submitted_late",
        "invoice_title": "invoice_title_not_allowed",
        "invoice_duplicate": "invoice_duplicate",
    }
    semantics = {
        "limit": "费用金额不得超过对应费用类型与币种的配置限额",
        "invoice_type": "费用类型只能使用制度允许的发票种类",
        "timeliness": "报销提交日期不得超过费用发生日后的配置天数",
        "invoice_title": "发票抬头必须属于制度允许的抬头",
        "invoice_duplicate": "同一发票号码不得重复报销",
    }
    return BindingQuery(
        rule_kind=definition.kind.value,
        reason_code=reason_codes[definition.kind.value],
        threshold_semantics=semantics[definition.kind.value],
    )


async def _latest_document_indexes(
    db: AsyncSession,
) -> dict[uuid.UUID, PolicyDocumentIndex]:
    rows = tuple(
        (
            await db.scalars(
                select(PolicyDocumentIndex).order_by(
                    PolicyDocumentIndex.document_id,
                    PolicyDocumentIndex.created_at.desc(),
                    PolicyDocumentIndex.id.desc(),
                )
            )
        ).all()
    )
    return {row.document_id: row for row in reversed(rows)}


def _document_item(
    document: PolicyDocument,
    indexes: dict[uuid.UUID, PolicyDocumentIndex],
) -> PolicyDocumentListItem:
    index = indexes.get(document.id)
    index_status: PolicyDocumentIndexStatus | None = index.status if index is not None else None
    return PolicyDocumentListItem(
        id=document.id,
        title=document.title,
        version=document.version,
        effective_date=document.effective_date,
        expiry_date=document.expiry_date,
        content_sha256=document.content_sha256,
        status=document.status.value,
        index_status=index_status.value if index_status is not None else None,
        index_completed_points=index.completed_point_count if index is not None else None,
        index_expected_points=index.expected_point_count if index is not None else None,
        failure_code=document.failure_code or (index.failure_code if index is not None else None),
        created_at=document.created_at,
    )
