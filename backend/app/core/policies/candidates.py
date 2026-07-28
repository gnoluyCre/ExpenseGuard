"""无 PII binding 候选 query、稳定排序与 PostgreSQL 二次校验。"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError
from app.core.policies.canonical import canonical_sha256
from app.core.retrieval import LocalModelProvider, SearchCandidate, VectorStore
from app.db.models.policy import (
    PolicyChunk,
    PolicyClause,
    PolicyDocument,
    PolicyDocumentStatus,
    PolicyFamily,
    PolicyIndexGeneration,
    PolicyIndexGenerationStatus,
)


class CandidateSearchError(ExpenseGuardError):
    """候选检索不可用；不得回退最新制度或猜测条款。"""

    status_code = 409


class BindingQuery(BaseModel):
    """仅由规则语义组成，禁止 raw row/PII 字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_kind: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(min_length=1, max_length=128)
    expense_type: str | None = Field(default=None, max_length=128)
    threshold_semantics: str | None = Field(default=None, max_length=512)

    def stable_text(self) -> str:
        fields = [f"规则类型: {self.rule_kind}", f"原因代码: {self.reason_code}"]
        if self.expense_type is not None:
            fields.append(f"费用类型: {self.expense_type}")
        if self.threshold_semantics is not None:
            fields.append(f"阈值语义: {self.threshold_semantics}")
        return "\n".join(fields)

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class BindingCandidate(BaseModel):
    """已通过 PG 二次校验并按本地 rerank 排序的候选。"""

    model_config = ConfigDict(frozen=True)

    family_id: uuid.UUID
    family_stable_key: str
    document_id: uuid.UUID
    document_title: str
    document_version: str
    effective_date: date
    expiry_date: date | None
    clause_id: uuid.UUID
    clause_no: str
    clause_ordinal: int
    clause_text: str
    chunk_id: uuid.UUID
    chunk_no: int
    vector_score: float
    rerank_score: float


_EPOCH = date(1970, 1, 1)
_MAX_DAY = (date.max - _EPOCH).days


async def search_binding_candidates(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    expense_date: date | None,
    query: BindingQuery,
    vector_store: VectorStore,
    reranker: LocalModelProvider,
    top_k: int,
    cutoff: float,
) -> tuple[BindingCandidate, ...]:
    if expense_date is None:
        raise CandidateSearchError(
            code="POLICY_EXPENSE_DATE_UNAVAILABLE", message="缺少费用发生日，不能选择制度版本"
        )
    generation = await db.scalar(
        select(PolicyIndexGeneration).where(
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE
        )
    )
    if generation is None:
        raise CandidateSearchError(
            code="POLICY_INDEX_UNAVAILABLE", message="当前租户没有 active generation"
        )
    raw = await vector_store.search_candidates(
        tenant_id,
        generation.generation,
        expense_date,
        query.stable_text(),
        top_k,
    )
    verified: list[
        tuple[SearchCandidate, PolicyChunk, PolicyClause, PolicyDocument, PolicyFamily]
    ] = []
    for candidate in raw:
        row = (
            await db.execute(
                select(PolicyChunk, PolicyClause, PolicyDocument, PolicyFamily)
                .join(
                    PolicyClause,
                    and_(
                        PolicyClause.id == PolicyChunk.clause_id,
                        PolicyClause.document_id == PolicyChunk.document_id,
                    ),
                )
                .join(PolicyDocument, PolicyDocument.id == PolicyChunk.document_id)
                .join(PolicyFamily, PolicyFamily.id == PolicyDocument.family_id)
                .where(
                    PolicyChunk.id == candidate.chunk_id,
                    PolicyClause.id == candidate.clause_id,
                    PolicyDocument.id == candidate.document_id,
                    PolicyFamily.id == candidate.family_id,
                )
            )
        ).one_or_none()
        if row is None:
            continue
        chunk, clause, document, family = row._tuple()
        if not _candidate_is_valid(
            candidate=candidate,
            tenant_id=tenant_id,
            expense_date=expense_date,
            generation=generation,
            chunk=chunk,
            clause=clause,
            document=document,
        ):
            continue
        verified.append((candidate, chunk, clause, document, family))
    scores = await reranker.rerank(query.stable_text(), [row[1].text for row in verified])
    if len(scores) != len(verified):
        raise CandidateSearchError(
            code="POLICY_RERANK_INVALID", message="本地 rerank 响应数量不一致"
        )
    result = [
        BindingCandidate(
            family_id=family.id,
            family_stable_key=family.stable_key,
            document_id=document.id,
            document_title=document.title,
            document_version=document.version,
            effective_date=document.effective_date,
            expiry_date=document.expiry_date,
            clause_id=clause.id,
            clause_no=clause.clause_no,
            clause_ordinal=int(clause.ordinal or 0),
            clause_text=clause.text,
            chunk_id=chunk.id,
            chunk_no=chunk.chunk_no,
            vector_score=candidate.vector_score,
            rerank_score=score,
        )
        for (candidate, chunk, clause, document, family), score in zip(
            verified, scores, strict=True
        )
        if score >= cutoff
    ]
    result.sort(
        key=lambda item: (
            -item.rerank_score,
            -item.vector_score,
            item.family_stable_key,
            item.effective_date,
            item.clause_ordinal,
            item.chunk_no,
            str(item.chunk_id),
        )
    )
    return tuple(result[:top_k])


def _candidate_is_valid(
    *,
    candidate: SearchCandidate,
    tenant_id: uuid.UUID,
    expense_date: date,
    generation: PolicyIndexGeneration,
    chunk: PolicyChunk,
    clause: PolicyClause,
    document: PolicyDocument,
) -> bool:
    return (
        chunk.tenant_id == tenant_id
        and clause.tenant_id == tenant_id
        and document.tenant_id == tenant_id
        and candidate.point_id == chunk.id
        and document.status == PolicyDocumentStatus.PUBLISHED
        and document.effective_date <= expense_date
        and (document.expiry_date is None or expense_date < document.expiry_date)
        and candidate.index_generation == generation.generation
        and candidate.effective_day == (document.effective_date - _EPOCH).days
        and candidate.expiry_day_exclusive
        == (_MAX_DAY if document.expiry_date is None else (document.expiry_date - _EPOCH).days)
        and candidate.embedding_model_fingerprint == generation.embedding_model_fingerprint
        and candidate.chunker_version == generation.chunker_version == chunk.chunker_version
        and candidate.document_content_sha256 == document.content_sha256
        and candidate.clause_text_sha256 == clause.text_sha256
        and candidate.chunk_text_sha256 == chunk.text_sha256
        and chunk.text == clause.text[chunk.start_offset : chunk.end_offset]
    )
