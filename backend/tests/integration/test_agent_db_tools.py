"""CP-F7.2 tenant-scoped read-only database tool integration tests."""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.agent.db_tools import DbReadOnlyToolBackend
from app.core.agent.models import (
    GetCorrelationRowsCall,
    GetEmployeeHistoryCall,
    GetSupplierHistoryCall,
    SearchPolicyClausesCall,
)
from app.core.agent.redaction import PiiKind, stable_pii_token
from app.core.agent.seed_service import prepare_investigation_seed
from app.core.agent.tools import ToolContext, ToolExecutionError
from app.core.detection.models import DetectorKind
from app.core.detection.run_service import run_detection
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.policies.candidates import BindingCandidate
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow, FileVersion, ParseStatus
from app.db.models.findings import CorrelationFinding
from tests.integration.test_detection_services import _create_profile, _seed_batch

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]
SECRET = b"f7-db-tools-test-secret-at-least-32-bytes"


class _PolicyRetriever:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def search(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[BindingCandidate, ...]:
        del db
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "expense_date": expense_date,
                "query": query,
                "top_k": top_k,
            }
        )
        return (
            BindingCandidate(
                family_id=uuid.UUID("70000000-0000-0000-0000-000000000001"),
                family_stable_key="travel-policy",
                document_id=uuid.UUID("70000000-0000-0000-0000-000000000002"),
                document_title="差旅制度",
                document_version="v3",
                effective_date=date(2025, 1, 1),
                expiry_date=None,
                clause_id=uuid.UUID("70000000-0000-0000-0000-000000000003"),
                clause_no="7.2",
                clause_ordinal=2,
                clause_text="同一事项不得拆分报销。",
                chunk_id=uuid.UUID("70000000-0000-0000-0000-000000000004"),
                chunk_no=0,
                vector_score=0.8,
                rerank_score=0.9,
            ),
        )


async def _candidate(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f7-tools-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        run = await run_detection(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="f7-db-tools-detection",
        )
        await db.commit()
        finding = await db.scalar(
            select(CorrelationFinding).where(
                CorrelationFinding.detection_run_id == run.id,
                CorrelationFinding.detector == DetectorKind.SPLIT_INVOICE,
            )
        )
        assert finding is not None
        first_row = await db.scalar(
            select(ExpenseRow).where(
                ExpenseRow.file_version_id == file_id,
                ExpenseRow.row_no == 1,
            )
        )
        assert first_row is not None and first_row.normalized_json is not None
        merchant = NormalizedExpenseRecord.model_validate(first_row.normalized_json).merchant
        assert merchant is not None
        return tenant_id, actor_id, file_id, run.id, finding.id, merchant


def _context(
    *, tenant_id: uuid.UUID, run_id: uuid.UUID, finding_id: uuid.UUID, merchant: str
) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id,
        detection_run_id=run_id,
        correlation_finding_id=finding_id,
        allowed_employee_tokens=frozenset(
            {
                stable_pii_token(
                    secret=SECRET,
                    tenant_id=tenant_id,
                    kind=PiiKind.EMPLOYEE_NAME,
                    value="employee-a",
                )
            }
        ),
        allowed_supplier_tokens=frozenset(
            {
                stable_pii_token(
                    secret=SECRET,
                    tenant_id=tenant_id,
                    kind=PiiKind.SUPPLIER,
                    value=merchant,
                )
            }
        ),
    )


async def _add_history(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_file_id: uuid.UUID,
    merchant: str,
) -> uuid.UUID:
    source = await db.scalar(select(FileVersion).where(FileVersion.id == source_file_id))
    assert source is not None and source.mapping_version_id is not None
    history = FileVersion(
        tenant_id=tenant_id,
        filename="history.xlsx",
        content_hash="9" * 64,
        row_count=2,
        uploaded_by=actor_id,
        mapping_version_id=source.mapping_version_id,
        parse_status=ParseStatus.PARSED,
        revision_no=1,
    )
    db.add(history)
    await db.flush()
    source_row = await db.scalar(
        select(ExpenseRow).where(
            ExpenseRow.file_version_id == source_file_id,
            ExpenseRow.row_no == 1,
        )
    )
    assert source_row is not None and source_row.normalized_json is not None
    base = NormalizedExpenseRecord.model_validate(source_row.normalized_json)
    for row_no, expense_date in ((1, "2026-01-09"), (2, "2025-12-01")):
        record = base.model_copy(
            update={
                "mapping_version_id": source.mapping_version_id,
                "expense_date": expense_date,
                "merchant": merchant,
            }
        )
        db.add(
            ExpenseRow(
                tenant_id=tenant_id,
                file_version_id=history.id,
                row_no=row_no,
                raw_json={"employee": "must-not-leak", "note": "<script>alert(1)</script>"},
                normalized_json=record.model_dump(mode="json"),
            )
        )
    await db.commit()
    return history.id


async def test_seed_is_bounded_tokenized_and_contains_no_raw_subjects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _actor_id, file_id, run_id, finding_id, merchant = await _candidate(session_factory)

    prepared = await prepare_investigation_seed(
        session_factory,
        tenant_id=tenant_id,
        detection_run_id=run_id,
        correlation_finding_id=finding_id,
        pii_secret=SECRET,
        token_version=1,
    )

    assert prepared.file_version_id == file_id
    assert prepared.seed.token_drafts
    assert prepared.seed.tool_context.allowed_employee_tokens
    assert prepared.seed.tool_context.allowed_supplier_tokens
    payload = json.dumps(prepared.seed.evidence, ensure_ascii=False, sort_keys=True)
    assert "employee-a" not in payload
    assert merchant not in payload
    assert "invoice_no" not in payload
    assert "evidence_fingerprint" in prepared.seed.evidence


async def test_correlation_rows_are_identity_bound_paginated_and_sanitized(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _actor_id, file_id, run_id, finding_id, merchant = await _candidate(session_factory)
    context = _context(
        tenant_id=tenant_id,
        run_id=run_id,
        finding_id=finding_id,
        merchant=merchant,
    )
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        backend = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=file_id,
            pii_secret=SECRET,
            token_version=1,
        )
        observation = await backend.get_correlation_rows(
            context, GetCorrelationRowsCall(limit=1, offset=0)
        )
        assert observation.has_more is True
        assert observation.data["candidate"] == {
            "tenant_id": str(tenant_id),
            "detection_run_id": str(run_id),
            "correlation_finding_id": str(finding_id),
            "file_version_id": str(file_id),
        }
        item = observation.data["items"][0]
        assert "raw" not in item
        serialized = str(observation.model_dump(mode="json"))
        assert "employee-a" not in serialized
        assert merchant not in serialized
        assert "source_hmac" not in serialized

        wrong_file_backend = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=uuid.uuid4(),
            pii_secret=SECRET,
            token_version=1,
        )
        with pytest.raises(ToolExecutionError) as raised:
            await wrong_file_backend.get_correlation_rows(context, GetCorrelationRowsCall())
        assert raised.value.code == "INVESTIGATION_CANDIDATE_NOT_FOUND"

        cross_tenant = context.model_copy(update={"tenant_id": uuid.uuid4()})
        with pytest.raises(ToolExecutionError) as raised:
            await backend.get_correlation_rows(cross_tenant, GetCorrelationRowsCall())
        assert raised.value.code == "INVESTIGATION_CANDIDATE_NOT_FOUND"


async def test_history_uses_candidate_short_map_stable_bounds_and_does_not_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, file_id, run_id, finding_id, merchant = await _candidate(session_factory)
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        history_id = await _add_history(
            db,
            tenant_id=tenant_id,
            actor_id=actor_id,
            source_file_id=file_id,
            merchant=merchant,
        )
        context = _context(
            tenant_id=tenant_id,
            run_id=run_id,
            finding_id=finding_id,
            merchant=merchant,
        )
        backend = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=file_id,
            pii_secret=SECRET,
            token_version=1,
        )
        before = int(await db.scalar(select(func.count()).select_from(ExpenseRow)) or 0)
        employee_token = next(iter(context.allowed_employee_tokens))
        employee = await backend.get_employee_history(
            context,
            GetEmployeeHistoryCall(employee_token=employee_token, days=60, limit=100),
        )
        assert employee.has_more is False
        employee_items = employee.data["items"]
        history_keys = [
            (
                item["row"]["expense_date"],
                -item["file_revision"],
                item["row"]["row_no"],
                item["file_version_id"],
            )
            for item in employee_items
        ]
        assert history_keys == sorted(
            history_keys,
            key=lambda item: (-date.fromisoformat(item[0]).toordinal(), *item[1:]),
        )
        supplier_token = next(iter(context.allowed_supplier_tokens))
        supplier = await backend.get_supplier_history(
            context,
            GetSupplierHistoryCall(supplier_token=supplier_token, days=60, limit=100),
        )
        assert any(item["file_version_id"] == str(history_id) for item in supplier.data["items"])
        serialized = str(supplier.model_dump(mode="json"))
        assert merchant not in serialized
        assert "must-not-leak" not in serialized
        assert "<script>" not in serialized
        assert int(await db.scalar(select(func.count()).select_from(ExpenseRow)) or 0) == before
        assert not db.new and not db.dirty and not db.deleted

        with pytest.raises(ToolExecutionError) as raised:
            await backend.get_employee_history(
                context,
                GetEmployeeHistoryCall(employee_token="EMP_v1_0123456789abcdef"),
            )
        assert raised.value.code == "INVESTIGATION_EMPLOYEE_TOKEN_FORBIDDEN"


async def test_policy_tool_delegates_to_local_f4_boundary_and_returns_exact_quote(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _actor_id, file_id, run_id, finding_id, merchant = await _candidate(session_factory)
    context = _context(
        tenant_id=tenant_id,
        run_id=run_id,
        finding_id=finding_id,
        merchant=merchant,
    )
    retriever = _PolicyRetriever()
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        backend = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=file_id,
            pii_secret=SECRET,
            token_version=1,
            policy_retriever=retriever,
        )
        result = await backend.search_policy_clauses(
            context,
            SearchPolicyClausesCall(query="拆分报销", expense_date="2026-01-10", limit=3),
        )
        assert retriever.calls == [
            {
                "tenant_id": tenant_id,
                "expense_date": date(2026, 1, 10),
                "query": "拆分报销",
                "top_k": 3,
            }
        ]
        item = result.data["items"][0]
        assert item["exact_quote"] == "同一事项不得拆分报销。"
        assert item["quote_start"] == 0
        assert item["quote_end"] == len(item["exact_quote"])

        unavailable = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=file_id,
            pii_secret=SECRET,
            token_version=1,
        )
        with pytest.raises(ToolExecutionError) as raised:
            await unavailable.search_policy_clauses(
                context,
                SearchPolicyClausesCall(query="拆分报销", expense_date="2026-01-10"),
            )
        assert raised.value.code == "INVESTIGATION_POLICY_TOOL_UNAVAILABLE"


async def test_each_tool_round_closes_its_short_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _actor_id, file_id, run_id, finding_id, merchant = await _candidate(session_factory)
    context = _context(
        tenant_id=tenant_id,
        run_id=run_id,
        finding_id=finding_id,
        merchant=merchant,
    )
    engine = session_factory.kw.get("bind")
    assert isinstance(engine, AsyncEngine)
    checked_out = 0
    checked_in = 0

    def checkout(*_args: object) -> None:
        nonlocal checked_out
        checked_out += 1

    def checkin(*_args: object) -> None:
        nonlocal checked_in
        checked_in += 1

    event.listen(engine.sync_engine, "checkout", checkout)
    event.listen(engine.sync_engine, "checkin", checkin)
    try:
        backend = DbReadOnlyToolBackend(
            session_factory=session_factory,
            file_version_id=file_id,
            pii_secret=SECRET,
            token_version=1,
        )
        first = await backend.get_correlation_rows(
            context, GetCorrelationRowsCall(limit=1, offset=0)
        )
        assert checked_out == checked_in == 1
        second = await backend.get_employee_history(
            context,
            GetEmployeeHistoryCall(
                employee_token=next(iter(context.allowed_employee_tokens)),
                days=30,
                limit=1,
            ),
        )
        assert first.tool_name == "get_correlation_rows"
        assert second.tool_name == "get_employee_history"
        assert checked_out == checked_in == 2
    finally:
        event.remove(engine.sync_engine, "checkout", checkout)
        event.remove(engine.sync_engine, "checkin", checkin)
