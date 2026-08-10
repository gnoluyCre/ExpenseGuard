"""Tenant-scoped database implementations of the four F7 read-only tools."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.core.agent.models import (
    GetCorrelationRowsCall,
    GetEmployeeHistoryCall,
    GetSupplierHistoryCall,
    SearchPolicyClausesCall,
)
from app.core.agent.redaction import PiiKind, stable_pii_token
from app.core.agent.sanitization import SanitizedRow, sanitize_expense_row
from app.core.agent.tools import ToolContext, ToolExecutionError, ToolObservation
from app.core.detection.canonical import canonical_sha256
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.policies.candidates import (
    BindingCandidate,
    BindingQuery,
    search_binding_candidates,
)
from app.core.policies.citations import verify_exact_quote
from app.core.retrieval import LocalModelProvider, VectorStore
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow, FileVersion
from app.db.models.detection import CorrelationFindingRow
from app.db.models.findings import CorrelationFinding


class PolicyCandidateRetriever(Protocol):
    """Injected local-policy retrieval boundary used by the policy tool."""

    async def search(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[BindingCandidate, ...]: ...


class F4PolicyCandidateRetriever:
    """Adapter that reuses F4's PG-verified local retrieval pipeline."""

    def __init__(
        self,
        *,
        vector_store: VectorStore,
        reranker: LocalModelProvider,
        cutoff: float,
    ) -> None:
        self._vector_store = vector_store
        self._reranker = reranker
        self._cutoff = cutoff

    async def search(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[BindingCandidate, ...]:
        return await search_binding_candidates(
            db,
            tenant_id=tenant_id,
            expense_date=expense_date,
            query=BindingQuery(
                rule_kind="anomaly_investigation",
                reason_code="F7_POLICY_SEARCH",
                threshold_semantics=query,
            ),
            vector_store=self._vector_store,
            reranker=self._reranker,
            top_k=top_k,
            cutoff=self._cutoff,
        )


class DbReadOnlyToolBackend:
    """Read-only F7 tool backend with an explicit immutable candidate context."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        file_version_id: uuid.UUID,
        pii_secret: bytes,
        token_version: int,
        policy_retriever: PolicyCandidateRetriever | None = None,
    ) -> None:
        if len(pii_secret) < 32:
            raise ValueError("PII tokenization secret must contain at least 32 bytes")
        if token_version <= 0:
            raise ValueError("token version must be positive")
        self._session_factory = session_factory
        self._file_version_id = file_version_id
        self._pii_secret = pii_secret
        self._token_version = token_version
        self._policy_retriever = policy_retriever

    async def get_correlation_rows(
        self, context: ToolContext, call: GetCorrelationRowsCall
    ) -> ToolObservation:
        async with self._tenant_db(context.tenant_id) as db:
            return await self._get_correlation_rows(db, context, call)

    async def _get_correlation_rows(
        self,
        db: AsyncSession,
        context: ToolContext,
        call: GetCorrelationRowsCall,
    ) -> ToolObservation:
        candidate = await self._require_candidate(db, context)
        filters = self._row_identity_filters(context)
        total = int(
            await db.scalar(select(func.count()).select_from(CorrelationFindingRow).where(*filters))
            or 0
        )
        rows = tuple(
            (
                await db.execute(
                    select(CorrelationFindingRow, ExpenseRow)
                    .join(
                        ExpenseRow,
                        (ExpenseRow.file_version_id == CorrelationFindingRow.file_version_id)
                        & (ExpenseRow.row_no == CorrelationFindingRow.row_no)
                        & (ExpenseRow.tenant_id == CorrelationFindingRow.tenant_id),
                    )
                    .where(*filters, ExpenseRow.tenant_id == context.tenant_id)
                    .order_by(
                        CorrelationFindingRow.ordinal,
                        CorrelationFindingRow.row_no,
                        CorrelationFindingRow.id,
                    )
                    .limit(call.limit)
                    .offset(call.offset)
                )
            ).all()
        )
        items = [
            {
                "ordinal": link.ordinal,
                "row": self._sanitize_row(expense_row).model_dump(mode="json"),
            }
            for link, expense_row in (row._tuple() for row in rows)
        ]
        return ToolObservation(
            tool_name=call.kind,
            data={
                "candidate": self._candidate_identity(context),
                "detector": candidate.detector,
                "detector_version": candidate.detector_version,
                # F6 evidence may contain invoice serial fragments or other fields
                # that are intentionally outside F7's model-input allowlist.  Keep
                # its immutable identity, but only send sanitized participating rows.
                "evidence_fingerprint": canonical_sha256(candidate.evidence_json),
                "items": items,
                "offset": call.offset,
                "limit": call.limit,
                "total": total,
            },
            has_more=call.offset + len(items) < total,
        )

    async def get_employee_history(
        self, context: ToolContext, call: GetEmployeeHistoryCall
    ) -> ToolObservation:
        if call.employee_token not in context.allowed_employee_tokens:
            raise _forbidden_token("EMPLOYEE")
        async with self._tenant_db(context.tenant_id) as db:
            identities, anchor = await self._resolve_candidate_subject(
                db,
                context,
                kind=PiiKind.EMPLOYEE_NAME,
                token=call.employee_token,
            )
            return await self._history(
                db,
                context=context,
                kind="employee",
                token=call.employee_token,
                identities=identities,
                anchor=anchor,
                days=call.days,
                limit=call.limit,
            )

    async def get_supplier_history(
        self, context: ToolContext, call: GetSupplierHistoryCall
    ) -> ToolObservation:
        if call.supplier_token not in context.allowed_supplier_tokens:
            raise _forbidden_token("SUPPLIER")
        async with self._tenant_db(context.tenant_id) as db:
            identities, anchor = await self._resolve_candidate_subject(
                db,
                context,
                kind=PiiKind.SUPPLIER,
                token=call.supplier_token,
            )
            return await self._history(
                db,
                context=context,
                kind="supplier",
                token=call.supplier_token,
                identities=identities,
                anchor=anchor,
                days=call.days,
                limit=call.limit,
            )

    async def search_policy_clauses(
        self, context: ToolContext, call: SearchPolicyClausesCall
    ) -> ToolObservation:
        async with self._tenant_db(context.tenant_id) as db:
            return await self._search_policy_clauses(db, context, call)

    async def _search_policy_clauses(
        self,
        db: AsyncSession,
        context: ToolContext,
        call: SearchPolicyClausesCall,
    ) -> ToolObservation:
        await self._require_candidate(db, context)
        if self._policy_retriever is None:
            raise ToolExecutionError(
                code="INVESTIGATION_POLICY_TOOL_UNAVAILABLE",
                message="本地制度检索能力不可用",
            )
        expense_date = date.fromisoformat(call.expense_date)
        candidates = await self._policy_retriever.search(
            db,
            tenant_id=context.tenant_id,
            expense_date=expense_date,
            query=call.query,
            top_k=call.limit,
        )
        items: list[dict[str, object]] = []
        for candidate in candidates[: call.limit]:
            verified = verify_exact_quote(
                clause_id=candidate.clause_id,
                clause_text=candidate.clause_text,
                quote_start=0,
                quote_end=len(candidate.clause_text),
                exact_quote=candidate.clause_text,
            )
            items.append(
                {
                    "family_id": str(candidate.family_id),
                    "family_stable_key": candidate.family_stable_key,
                    "document_id": str(candidate.document_id),
                    "document_title": candidate.document_title,
                    "document_version": candidate.document_version,
                    "effective_date": candidate.effective_date.isoformat(),
                    "expiry_date": (
                        candidate.expiry_date.isoformat()
                        if candidate.expiry_date is not None
                        else None
                    ),
                    "clause_id": str(verified.clause_id),
                    "clause_no": candidate.clause_no,
                    "quote_start": verified.quote_start,
                    "quote_end": verified.quote_end,
                    "exact_quote": verified.exact_quote,
                    "vector_score": candidate.vector_score,
                    "rerank_score": candidate.rerank_score,
                }
            )
        return ToolObservation(
            tool_name=call.kind,
            data={
                "candidate": self._candidate_identity(context),
                "expense_date": call.expense_date,
                "items": items,
            },
            has_more=len(candidates) > len(items),
        )

    async def _history(
        self,
        db: AsyncSession,
        *,
        context: ToolContext,
        kind: str,
        token: str,
        identities: tuple[str, ...],
        anchor: date,
        days: int,
        limit: int,
    ) -> ToolObservation:
        identity_field = "employee" if kind == "employee" else "merchant"
        date_field = ExpenseRow.normalized_json["expense_date"].as_string()
        identity_json_field = ExpenseRow.normalized_json[identity_field].as_string()
        rows = tuple(
            (
                await db.execute(
                    select(ExpenseRow, FileVersion)
                    .join(
                        FileVersion,
                        (FileVersion.id == ExpenseRow.file_version_id)
                        & (FileVersion.tenant_id == ExpenseRow.tenant_id),
                    )
                    .where(
                        ExpenseRow.tenant_id == context.tenant_id,
                        FileVersion.tenant_id == context.tenant_id,
                        ExpenseRow.normalized_json.is_not(None),
                        identity_json_field.in_(identities),
                        date_field >= (anchor - timedelta(days=days)).isoformat(),
                        date_field <= anchor.isoformat(),
                    )
                    .order_by(
                        date_field.desc(),
                        FileVersion.revision_no.desc(),
                        ExpenseRow.row_no,
                        ExpenseRow.file_version_id,
                        ExpenseRow.id,
                    )
                    .limit(limit + 1)
                )
            ).all()
        )
        selected = rows[:limit]
        items = [
            {
                "file_version_id": str(expense_row.file_version_id),
                "file_revision": file_version.revision_no,
                "row": self._sanitize_row(expense_row).model_dump(mode="json"),
            }
            for expense_row, file_version in (row._tuple() for row in selected)
        ]
        return ToolObservation(
            tool_name=f"get_{kind}_history",
            data={
                "candidate": self._candidate_identity(context),
                f"{kind}_token": token,
                "anchor_date": anchor.isoformat(),
                "start_date": (anchor - timedelta(days=days)).isoformat(),
                "items": items,
                "limit": limit,
            },
            has_more=len(rows) > limit,
        )

    async def _resolve_candidate_subject(
        self,
        db: AsyncSession,
        context: ToolContext,
        *,
        kind: PiiKind,
        token: str,
    ) -> tuple[tuple[str, ...], date]:
        await self._require_candidate(db, context)
        rows = await self._candidate_expense_rows(db, context)
        identities: set[str] = set()
        dates: list[date] = []
        for row in rows:
            record = self._record(row)
            dates.append(date.fromisoformat(record.expense_date))
            value = record.employee if kind is PiiKind.EMPLOYEE_NAME else record.merchant
            if value is None:
                continue
            candidate_token = stable_pii_token(
                secret=self._pii_secret,
                tenant_id=context.tenant_id,
                kind=kind,
                value=value,
                version=self._token_version,
            )
            if candidate_token == token:
                identities.add(value)
        if not identities or not dates:
            raise _forbidden_token("EMPLOYEE" if kind is PiiKind.EMPLOYEE_NAME else "SUPPLIER")
        return tuple(sorted(identities)), max(dates)

    async def _candidate_expense_rows(
        self, db: AsyncSession, context: ToolContext
    ) -> tuple[ExpenseRow, ...]:
        rows = tuple(
            (
                await db.scalars(
                    select(ExpenseRow)
                    .join(
                        CorrelationFindingRow,
                        (CorrelationFindingRow.file_version_id == ExpenseRow.file_version_id)
                        & (CorrelationFindingRow.row_no == ExpenseRow.row_no)
                        & (CorrelationFindingRow.tenant_id == ExpenseRow.tenant_id),
                    )
                    .where(
                        *self._row_identity_filters(context),
                        ExpenseRow.tenant_id == context.tenant_id,
                    )
                    .order_by(
                        CorrelationFindingRow.ordinal,
                        CorrelationFindingRow.row_no,
                        CorrelationFindingRow.id,
                    )
                )
            ).all()
        )
        if not rows:
            raise ToolExecutionError(
                code="INVESTIGATION_CANDIDATE_ROWS_MISSING",
                message="当前调查候选缺少参与行",
            )
        return rows

    async def _require_candidate(
        self, db: AsyncSession, context: ToolContext
    ) -> CorrelationFinding:
        candidate = await db.scalar(
            select(CorrelationFinding).where(
                CorrelationFinding.id == context.correlation_finding_id,
                CorrelationFinding.detection_run_id == context.detection_run_id,
                CorrelationFinding.file_version_id == self._file_version_id,
                CorrelationFinding.tenant_id == context.tenant_id,
            )
        )
        if candidate is None:
            raise ToolExecutionError(
                code="INVESTIGATION_CANDIDATE_NOT_FOUND",
                message="调查候选不存在或身份不匹配",
            )
        return candidate

    @asynccontextmanager
    async def _tenant_db(self, tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """Open one tenant-bound read transaction and close it before tool return."""

        async with self._session_factory() as db:
            bind_tenant(db.sync_session, tenant_id)
            try:
                yield db
            finally:
                await db.rollback()

    def _sanitize_row(self, row: ExpenseRow) -> SanitizedRow:
        return sanitize_expense_row(
            tenant_id=row.tenant_id,
            row_no=row.row_no,
            record=self._record(row),
            secret=self._pii_secret,
            token_version=self._token_version,
        ).row

    @staticmethod
    def _record(row: ExpenseRow) -> NormalizedExpenseRecord:
        if row.normalized_json is None or row.parse_error_code is not None:
            raise ToolExecutionError(
                code="INVESTIGATION_ROW_UNAVAILABLE",
                message="调查参与行缺少可用的规范化记录",
            )
        try:
            return NormalizedExpenseRecord.model_validate(row.normalized_json)
        except ValueError as exc:
            raise ToolExecutionError(
                code="INVESTIGATION_ROW_INVALID",
                message="调查参与行的规范化记录无效",
            ) from exc

    def _candidate_identity(self, context: ToolContext) -> dict[str, str]:
        return {
            "tenant_id": str(context.tenant_id),
            "detection_run_id": str(context.detection_run_id),
            "correlation_finding_id": str(context.correlation_finding_id),
            "file_version_id": str(self._file_version_id),
        }

    def _row_identity_filters(self, context: ToolContext) -> tuple[ColumnElement[bool], ...]:
        return (
            CorrelationFindingRow.tenant_id == context.tenant_id,
            CorrelationFindingRow.detection_run_id == context.detection_run_id,
            CorrelationFindingRow.finding_id == context.correlation_finding_id,
            CorrelationFindingRow.file_version_id == self._file_version_id,
        )


def _forbidden_token(kind: str) -> ToolExecutionError:
    return ToolExecutionError(
        code=f"INVESTIGATION_{kind}_TOKEN_FORBIDDEN",
        message="主体 token 不属于当前调查上下文",
    )
