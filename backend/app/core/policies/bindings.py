"""Configurator-confirmed rule-to-policy binding service.

The service is deliberately PostgreSQL-only. Retrieval candidates can help a
configurator choose a clause, but they never participate in persistence or in
the exact-quote decision.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError, NotFoundError, PermissionDeniedError
from app.core.policies.canonical import canonical_binding_fingerprint
from app.core.policies.citations import CitationVerificationError, verify_exact_quote
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.db.models.config import RuleConfig
from app.db.models.policy import (
    PolicyClause,
    PolicyDocument,
    PolicyDocumentStatus,
    PolicyFamily,
    RulePolicyBinding,
)
from app.db.models.tenancy import AppUser, Role

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class BindingServiceError(ExpenseGuardError):
    """Stable, quote-safe binding error."""

    status_code = 409


class BindingSelection(BaseModel):
    """One exact clause slice in a complete, ordered binding set."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    policy_document_id: uuid.UUID
    policy_clause_id: uuid.UUID
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)
    exact_quote: str
    citation_order: int = Field(ge=1, le=3)

    @model_validator(mode="after")
    def _ordered_offsets(self) -> BindingSelection:
        if self.quote_end <= self.quote_start:
            raise ValueError("quote_end 必须大于 quote_start")
        return self


class SavedBindingSet(BaseModel):
    """A newly persisted or canonically reused complete binding set."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    bindings: tuple[RulePolicyBinding, ...]
    created: bool


async def save_rule_policy_bindings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    rule_config_id: uuid.UUID,
    expense_date: date,
    selections: Sequence[BindingSelection],
) -> SavedBindingSet:
    """Atomically persist one complete 1-3 citation binding set.

    All validation happens after the tenant lock and before any binding or
    audit row is added. Failed exact quotes are therefore neither persisted nor
    copied into an exception, audit payload, or log message.
    """
    await _lock_tenant(db, tenant_id)
    ordered = _validate_binding_order(selections)

    rule = await db.scalar(select(RuleConfig).where(RuleConfig.id == rule_config_id))
    actor = await db.scalar(select(AppUser).where(AppUser.id == created_by))
    if rule is None or actor is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="规则或操作人不存在")
    if actor.role != Role.CONFIGURATOR:
        raise PermissionDeniedError(code="PERMISSION_DENIED", message="只有配置员可以确认制度绑定")

    resolved: list[tuple[BindingSelection, PolicyDocument, PolicyFamily, PolicyClause, str]] = []
    for selection in ordered:
        row = (
            await db.execute(
                select(PolicyDocument, PolicyFamily, PolicyClause)
                .join(
                    PolicyFamily,
                    and_(
                        PolicyFamily.id == PolicyDocument.family_id,
                        PolicyFamily.tenant_id == PolicyDocument.tenant_id,
                    ),
                )
                .join(
                    PolicyClause,
                    and_(
                        PolicyClause.id == selection.policy_clause_id,
                        PolicyClause.document_id == PolicyDocument.id,
                        PolicyClause.family_id == PolicyDocument.family_id,
                        PolicyClause.tenant_id == PolicyDocument.tenant_id,
                    ),
                )
                .where(
                    PolicyDocument.id == selection.policy_document_id,
                    PolicyDocument.status == PolicyDocumentStatus.PUBLISHED,
                    PolicyDocument.effective_date <= expense_date,
                    or_(
                        PolicyDocument.expiry_date.is_(None),
                        PolicyDocument.expiry_date > expense_date,
                    ),
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(
                code="POLICY_NOT_FOUND",
                message="制度条款不存在、未发布或在指定日期无效",
            )
        document, family, clause = row
        if (
            clause.text_sha256 is None
            or document.content_sha256 is None
            or _text_sha256(clause.text) != clause.text_sha256
        ):
            raise BindingServiceError(
                code="POLICY_CLAUSE_INVALID", message="制度条款缺少冻结校验信息"
            )
        try:
            verified = verify_exact_quote(
                clause_id=clause.id,
                clause_text=clause.text,
                quote_start=selection.quote_start,
                quote_end=selection.quote_end,
                exact_quote=selection.exact_quote,
            )
        except CitationVerificationError as exc:
            raise BindingServiceError(
                code="QUOTE_NOT_EXACT", message="制度引用未通过逐字校验"
            ) from exc
        quote_sha256 = _text_sha256(verified.exact_quote)
        fingerprint = _binding_fingerprint(
            tenant_id=tenant_id,
            rule_config_id=rule.id,
            family_id=family.id,
            document_id=document.id,
            clause_id=clause.id,
            quote_start=verified.quote_start,
            quote_end=verified.quote_end,
            quote_sha256=quote_sha256,
            clause_text_sha256=clause.text_sha256,
            citation_order=selection.citation_order,
        )
        resolved.append((selection, document, family, clause, fingerprint))

    existing = await _applicable_bindings(
        db, rule_config_id=rule_config_id, expense_date=expense_date
    )
    if existing:
        _validate_existing_set(existing)
        requested_fingerprints = tuple(item[4] for item in resolved)
        existing_fingerprints = tuple(item[0].binding_fingerprint for item in existing)
        if existing_fingerprints == requested_fingerprints:
            return SavedBindingSet(bindings=tuple(item[0] for item in existing), created=False)
        raise BindingServiceError(
            code="POLICY_BINDING_AMBIGUOUS",
            message="该规则在指定日期已有不同的制度绑定",
        )

    bindings: list[RulePolicyBinding] = []
    for selection, document, family, clause, fingerprint in resolved:
        binding = RulePolicyBinding(
            tenant_id=tenant_id,
            rule_config_id=rule.id,
            policy_family_id=family.id,
            policy_document_id=document.id,
            policy_clause_id=clause.id,
            quote_start=selection.quote_start,
            quote_end=selection.quote_end,
            quote=selection.exact_quote,
            quote_sha256=_text_sha256(selection.exact_quote),
            clause_text_sha256=clause.text_sha256,
            citation_order=selection.citation_order,
            binding_fingerprint=fingerprint,
            created_by=created_by,
        )
        db.add(binding)
        bindings.append(binding)
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.binding_create",
        actor_id=created_by,
        target_type="rule_config",
        target_id=str(rule.id),
        payload={
            "rule_config_id": str(rule.id),
            "binding_ids": [str(binding.id) for binding in bindings],
            "binding_fingerprints": [binding.binding_fingerprint for binding in bindings],
            "citation_count": len(bindings),
        },
    )
    return SavedBindingSet(bindings=tuple(bindings), created=True)


def _validate_binding_order(
    selections: Sequence[BindingSelection],
) -> tuple[BindingSelection, ...]:
    if not 1 <= len(selections) <= 3:
        raise BindingServiceError(
            code="POLICY_BINDING_ORDER_INVALID", message="制度绑定必须包含 1 至 3 条引用"
        )
    ordered = tuple(sorted(selections, key=lambda item: item.citation_order))
    if tuple(item.citation_order for item in ordered) != tuple(range(1, len(ordered) + 1)):
        raise BindingServiceError(
            code="POLICY_BINDING_ORDER_INVALID", message="制度引用顺序必须从 1 连续递增"
        )
    return ordered


async def _applicable_bindings(
    db: AsyncSession, *, rule_config_id: uuid.UUID, expense_date: date
) -> tuple[tuple[RulePolicyBinding, PolicyDocument, PolicyClause], ...]:
    rows = (
        await db.execute(
            select(RulePolicyBinding, PolicyDocument, PolicyClause)
            .join(
                PolicyDocument,
                and_(
                    PolicyDocument.id == RulePolicyBinding.policy_document_id,
                    PolicyDocument.family_id == RulePolicyBinding.policy_family_id,
                    PolicyDocument.tenant_id == RulePolicyBinding.tenant_id,
                ),
            )
            .join(
                PolicyClause,
                and_(
                    PolicyClause.id == RulePolicyBinding.policy_clause_id,
                    PolicyClause.document_id == PolicyDocument.id,
                    PolicyClause.tenant_id == RulePolicyBinding.tenant_id,
                ),
            )
            .where(
                RulePolicyBinding.rule_config_id == rule_config_id,
                PolicyDocument.status == PolicyDocumentStatus.PUBLISHED,
                PolicyDocument.effective_date <= expense_date,
                or_(
                    PolicyDocument.expiry_date.is_(None),
                    PolicyDocument.expiry_date > expense_date,
                ),
            )
            .order_by(RulePolicyBinding.citation_order, RulePolicyBinding.id)
        )
    ).all()
    return tuple((binding, document, clause) for binding, document, clause in rows)


def _validate_existing_set(
    existing: Sequence[tuple[RulePolicyBinding, PolicyDocument, PolicyClause]],
) -> None:
    orders = tuple(item[0].citation_order for item in existing)
    if not 1 <= len(existing) <= 3 or orders != tuple(range(1, len(existing) + 1)):
        raise BindingServiceError(
            code="POLICY_BINDING_AMBIGUOUS",
            message="该规则在指定日期存在歧义制度绑定",
        )
    for binding, _, clause in existing:
        if (
            clause.text_sha256 != binding.clause_text_sha256
            or _text_sha256(clause.text) != clause.text_sha256
            or _text_sha256(binding.quote) != binding.quote_sha256
            or _binding_fingerprint(
                tenant_id=binding.tenant_id,
                rule_config_id=binding.rule_config_id,
                family_id=binding.policy_family_id,
                document_id=binding.policy_document_id,
                clause_id=binding.policy_clause_id,
                quote_start=binding.quote_start,
                quote_end=binding.quote_end,
                quote_sha256=binding.quote_sha256,
                clause_text_sha256=binding.clause_text_sha256,
                citation_order=binding.citation_order,
            )
            != binding.binding_fingerprint
        ):
            raise BindingServiceError(
                code="POLICY_BINDING_AMBIGUOUS",
                message="该规则在指定日期存在歧义制度绑定",
            )
        try:
            verify_exact_quote(
                clause_id=clause.id,
                clause_text=clause.text,
                quote_start=binding.quote_start,
                quote_end=binding.quote_end,
                exact_quote=binding.quote,
            )
        except CitationVerificationError as exc:
            raise BindingServiceError(
                code="POLICY_BINDING_AMBIGUOUS",
                message="该规则在指定日期存在歧义制度绑定",
            ) from exc


def _binding_fingerprint(
    *,
    tenant_id: uuid.UUID,
    rule_config_id: uuid.UUID,
    family_id: uuid.UUID,
    document_id: uuid.UUID,
    clause_id: uuid.UUID,
    quote_start: int,
    quote_end: int,
    quote_sha256: str,
    clause_text_sha256: str,
    citation_order: int,
) -> str:
    return canonical_binding_fingerprint(
        tenant_id=tenant_id,
        rule_config_id=rule_config_id,
        policy_family_id=family_id,
        policy_document_id=document_id,
        policy_clause_id=clause_id,
        quote_start=quote_start,
        quote_end=quote_end,
        quote_sha256=quote_sha256,
        clause_text_sha256=clause_text_sha256,
        citation_order=citation_order,
    )


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    try:
        await lock_tenant_nowait(db, tenant_id)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise BindingServiceError(
                code="POLICY_CHANGE_IN_PROGRESS",
                message="该租户的制度配置正在变更，请稍后重试",
            ) from exc
        raise
