"""可恢复的 policy index transactional-outbox worker。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.policies.service import PolicyServiceError
from app.core.retrieval import IndexChunk, VectorStore
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.db.models.policy import (
    PolicyChunk,
    PolicyClause,
    PolicyDocument,
    PolicyDocumentIndex,
    PolicyDocumentIndexStatus,
    PolicyDocumentStatus,
    PolicyIndexGeneration,
    PolicyIndexGenerationStatus,
    PolicyIndexJob,
    PolicyIndexJobStatus,
)


class ClaimedIndexJob(BaseModel):
    """跨事务传递的租约凭据，不含制度原文。"""

    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID
    tenant_id: uuid.UUID
    generation_id: uuid.UUID
    lease_token: str
    attempt_count: int


async def claim_index_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    generation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ClaimedIndexJob | None:
    """用 SKIP LOCKED 领取 pending 或租约过期的可重试 job。"""
    current = now or datetime.now(UTC)
    statement = (
        select(PolicyIndexJob)
        .where(
            PolicyIndexJob.attempt_count < PolicyIndexJob.attempt_limit,
            or_(
                and_(
                    PolicyIndexJob.status == PolicyIndexJobStatus.PENDING,
                    PolicyIndexJob.available_at <= current,
                ),
                and_(
                    PolicyIndexJob.status == PolicyIndexJobStatus.RUNNING,
                    PolicyIndexJob.lease_expires_at <= current,
                ),
            ),
        )
        .order_by(PolicyIndexJob.available_at, PolicyIndexJob.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if generation_id is not None:
        statement = statement.where(PolicyIndexJob.index_generation_id == generation_id)
    job = await db.scalar(statement)
    if job is None:
        return None
    token = uuid.uuid4().hex
    job.status = PolicyIndexJobStatus.RUNNING
    job.attempt_count += 1
    job.lease_owner = worker_id
    job.lease_token = token
    job.lease_expires_at = current + timedelta(seconds=lease_seconds)
    job.last_failure_code = None
    await db.flush()
    return ClaimedIndexJob(
        job_id=job.id,
        tenant_id=tenant_id,
        generation_id=job.index_generation_id,
        lease_token=token,
        attempt_count=job.attempt_count,
    )


async def execute_claimed_job(
    db: AsyncSession,
    *,
    claimed: ClaimedIndexJob,
    vector_store: VectorStore,
) -> None:
    """幂等 upsert 一个 chunk，并在仍持有租约时标记完成。"""
    job = await _leased_job(db, claimed)
    row = (
        await db.execute(
            select(PolicyChunk, PolicyClause, PolicyDocument, PolicyIndexGeneration)
            .join(
                PolicyClause,
                and_(
                    PolicyClause.id == PolicyChunk.clause_id,
                    PolicyClause.document_id == PolicyChunk.document_id,
                ),
            )
            .join(PolicyDocument, PolicyDocument.id == PolicyChunk.document_id)
            .join(PolicyIndexGeneration, PolicyIndexGeneration.id == job.index_generation_id)
            .where(
                PolicyChunk.id == job.chunk_id,
                PolicyDocument.status.in_(
                    [PolicyDocumentStatus.INDEXING, PolicyDocumentStatus.PUBLISHED]
                ),
                PolicyIndexGeneration.status.in_(
                    [
                        PolicyIndexGenerationStatus.ACTIVE,
                        PolicyIndexGenerationStatus.BUILDING,
                    ]
                ),
            )
        )
    ).one_or_none()
    if row is None:
        raise PolicyServiceError(
            code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源状态不一致"
        )
    chunk, clause, document, generation = row._tuple()
    if (
        clause.text_sha256 is None
        or document.content_sha256 is None
        or document.family_id is None
        or chunk.text != clause.text[chunk.start_offset : chunk.end_offset]
        or chunk.chunker_version != generation.chunker_version
    ):
        raise PolicyServiceError(code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源校验失败")
    index_chunk = IndexChunk(
        family_id=document.family_id,
        document_id=document.id,
        clause_id=clause.id,
        chunk_id=chunk.id,
        text=chunk.text,
        effective_date=document.effective_date,
        expiry_date=document.expiry_date,
        document_content_sha256=document.content_sha256,
        clause_text_sha256=clause.text_sha256,
        chunk_text_sha256=chunk.text_sha256,
        chunker_version=chunk.chunker_version,
    )
    await vector_store.upsert_chunks(
        claimed.tenant_id,
        generation.generation,
        [index_chunk],
    )
    # Qdrant 调用期间租约可能被回收；只有仍持有 token 的 worker 可完成 PG job。
    await db.refresh(job)
    if job.status != PolicyIndexJobStatus.RUNNING or job.lease_token != claimed.lease_token:
        raise PolicyServiceError(code="POLICY_INDEX_LEASE_LOST", message="索引任务租约已失效")
    job.status = PolicyIndexJobStatus.COMPLETED
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    job.last_failure_code = None
    await db.flush()


async def record_job_failure(
    db: AsyncSession,
    *,
    claimed: ClaimedIndexJob,
    failure_code: str,
    retry_delay_seconds: int,
    now: datetime | None = None,
) -> bool:
    """记录稳定错误码；达到上限时 terminal fail，返回是否终止。"""
    current = now or datetime.now(UTC)
    job = await _leased_job(db, claimed)
    terminal = job.attempt_count >= job.attempt_limit
    job.status = PolicyIndexJobStatus.FAILED if terminal else PolicyIndexJobStatus.PENDING
    job.available_at = current + timedelta(seconds=max(0, retry_delay_seconds))
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    job.last_failure_code = failure_code
    if terminal:
        siblings = tuple(
            (
                await db.scalars(
                    select(PolicyIndexJob).where(
                        PolicyIndexJob.document_id == job.document_id,
                        PolicyIndexJob.index_generation_id == job.index_generation_id,
                        PolicyIndexJob.id != job.id,
                        PolicyIndexJob.status.in_(
                            [PolicyIndexJobStatus.PENDING, PolicyIndexJobStatus.RUNNING]
                        ),
                    )
                )
            ).all()
        )
        for sibling in siblings:
            sibling.status = PolicyIndexJobStatus.FAILED
            sibling.lease_owner = None
            sibling.lease_token = None
            sibling.lease_expires_at = None
            sibling.last_failure_code = failure_code
        document_index = await db.scalar(
            select(PolicyDocumentIndex).where(
                PolicyDocumentIndex.document_id == job.document_id,
                PolicyDocumentIndex.index_generation_id == job.index_generation_id,
            )
        )
        document = await db.scalar(
            select(PolicyDocument).where(PolicyDocument.id == job.document_id)
        )
        if document_index is None or document is None:
            raise PolicyServiceError(
                code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源状态不一致"
            )
        document_index.status = PolicyDocumentIndexStatus.FAILED
        document_index.failure_code = failure_code
        if document.status == PolicyDocumentStatus.INDEXING:
            document.status = PolicyDocumentStatus.FAILED
            document.failure_code = failure_code
        await write_audit(
            db,
            tenant_id=claimed.tenant_id,
            action="policy.index_failed",
            target_type="policy_document",
            target_id=str(document.id),
            payload={
                "document_id": str(document.id),
                "index_generation_id": str(job.index_generation_id),
                "job_id": str(job.id),
                "reason_code": failure_code,
                "attempt_count": job.attempt_count,
            },
        )
    await db.flush()
    return terminal


async def retry_failed_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    document_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """人工审计后重置 terminal jobs；不创建重复逻辑点。"""
    await lock_tenant_nowait(db, tenant_id)
    document = await db.scalar(
        select(PolicyDocument).where(PolicyDocument.id == document_id).with_for_update()
    )
    if document is None:
        raise PolicyServiceError(code="POLICY_NOT_FOUND", message="制度文档不存在")
    if document.status != PolicyDocumentStatus.FAILED:
        raise PolicyServiceError(code="POLICY_STATE_INVALID", message="制度当前状态不可重试")
    jobs = tuple(
        (
            await db.scalars(
                select(PolicyIndexJob).where(
                    PolicyIndexJob.document_id == document_id,
                    PolicyIndexJob.status == PolicyIndexJobStatus.FAILED,
                )
            )
        ).all()
    )
    if not jobs:
        raise PolicyServiceError(code="POLICY_INDEX_JOB_NOT_FOUND", message="没有失败索引任务")
    current = now or datetime.now(UTC)
    for job in jobs:
        job.status = PolicyIndexJobStatus.PENDING
        job.attempt_count = 0
        job.available_at = current
        job.last_failure_code = None
    document.status = PolicyDocumentStatus.INDEXING
    document.failure_code = None
    document_index = await db.scalar(
        select(PolicyDocumentIndex).where(PolicyDocumentIndex.document_id == document_id)
    )
    if document_index is None:
        raise PolicyServiceError(
            code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源状态不一致"
        )
    document_index.status = PolicyDocumentIndexStatus.INDEXING
    document_index.failure_code = None
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.index_retry",
        actor_id=actor_id,
        target_type="policy_document",
        target_id=str(document_id),
        payload={"document_id": str(document_id), "retry_job_count": len(jobs)},
    )
    return len(jobs)


async def retry_failed_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    generation_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """人工重置 building generation 的 terminal job，并追加安全审计。"""
    await lock_tenant_nowait(db, tenant_id)
    generation = await db.scalar(
        select(PolicyIndexGeneration)
        .where(
            PolicyIndexGeneration.id == generation_id,
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.BUILDING,
        )
        .with_for_update()
    )
    if generation is None:
        raise PolicyServiceError(code="POLICY_INDEX_NOT_FOUND", message="构建中索引代际不存在")
    jobs = tuple(
        (
            await db.scalars(
                select(PolicyIndexJob).where(
                    PolicyIndexJob.index_generation_id == generation_id,
                    PolicyIndexJob.status == PolicyIndexJobStatus.FAILED,
                )
            )
        ).all()
    )
    if not jobs:
        raise PolicyServiceError(code="POLICY_INDEX_JOB_NOT_FOUND", message="没有失败索引任务")
    current = now or datetime.now(UTC)
    failed_document_ids = {job.document_id for job in jobs}
    for job in jobs:
        job.status = PolicyIndexJobStatus.PENDING
        job.attempt_count = 0
        job.available_at = current
        job.last_failure_code = None
    indexes = tuple(
        (
            await db.scalars(
                select(PolicyDocumentIndex).where(
                    PolicyDocumentIndex.index_generation_id == generation_id,
                    PolicyDocumentIndex.document_id.in_(failed_document_ids),
                )
            )
        ).all()
    )
    for document_index in indexes:
        document_index.status = PolicyDocumentIndexStatus.INDEXING
        document_index.failure_code = None
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.index_retry",
        actor_id=actor_id,
        target_type="policy_index_generation",
        target_id=str(generation_id),
        payload={"index_generation_id": str(generation_id), "retry_job_count": len(jobs)},
    )
    await db.flush()
    return len(jobs)


async def reconcile_building_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    generation_id: uuid.UUID,
    vector_store: VectorStore,
    attempt_limit: int,
) -> bool:
    """完成计数校验；manifest 有 delta 时显式追加 revision，否则原子切 active。"""
    from app.core.policies.canonical import canonical_sha256
    from app.core.policies.service import _append_generation_jobs, _published_manifest_rows

    await lock_tenant_nowait(db, tenant_id)
    generation = await db.scalar(
        select(PolicyIndexGeneration)
        .where(
            PolicyIndexGeneration.id == generation_id,
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.BUILDING,
        )
        .with_for_update()
    )
    if generation is None:
        raise PolicyServiceError(code="POLICY_INDEX_NOT_FOUND", message="构建中索引代际不存在")
    completed = int(
        await db.scalar(
            select(func.count())
            .select_from(PolicyIndexJob)
            .where(
                PolicyIndexJob.index_generation_id == generation_id,
                PolicyIndexJob.status == PolicyIndexJobStatus.COMPLETED,
            )
        )
        or 0
    )
    indexes = tuple(
        (
            await db.scalars(
                select(PolicyDocumentIndex)
                .where(PolicyDocumentIndex.index_generation_id == generation_id)
                .with_for_update()
            )
        ).all()
    )
    for document_index in indexes:
        document_completed = int(
            await db.scalar(
                select(func.count())
                .select_from(PolicyIndexJob)
                .where(
                    PolicyIndexJob.index_generation_id == generation_id,
                    PolicyIndexJob.document_id == document_index.document_id,
                    PolicyIndexJob.status == PolicyIndexJobStatus.COMPLETED,
                )
            )
            or 0
        )
        document_index.completed_point_count = document_completed
        if document_completed == document_index.expected_point_count:
            document_index.status = PolicyDocumentIndexStatus.COMPLETED
    qdrant_count = await vector_store.verify_generation(tenant_id, generation.generation)
    if qdrant_count != completed:
        raise PolicyServiceError(
            code="POLICY_INDEX_COUNT_MISMATCH", message="PG 与向量库点数不一致"
        )
    generation.completed_point_count = completed
    if completed != generation.expected_point_count:
        await db.flush()
        return False

    current_rows = await _published_manifest_rows(db)
    current_manifest = canonical_sha256(
        [[str(chunk.id), chunk.text_sha256] for _, chunk in current_rows]
    )
    indexed_chunk_ids = set(
        (
            await db.scalars(
                select(PolicyIndexJob.chunk_id).where(
                    PolicyIndexJob.index_generation_id == generation_id
                )
            )
        ).all()
    )
    delta = tuple(
        (document, chunk) for document, chunk in current_rows if chunk.id not in indexed_chunk_ids
    )
    if delta:
        generation.manifest_revision += 1
        generation.source_manifest_fingerprint = current_manifest
        generation.expected_point_count += len(delta)
        await _append_generation_jobs(
            db,
            tenant_id=tenant_id,
            generation=generation,
            rows=delta,
            attempt_limit=attempt_limit,
        )
        return False
    if current_manifest != generation.source_manifest_fingerprint:
        raise PolicyServiceError(
            code="POLICY_INDEX_MANIFEST_MISMATCH", message="published manifest 与冻结索引不一致"
        )
    active = await db.scalar(
        select(PolicyIndexGeneration)
        .where(PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE)
        .with_for_update()
    )
    if active is not None:
        active.status = PolicyIndexGenerationStatus.RETIRED
        await db.flush()
    generation.status = PolicyIndexGenerationStatus.ACTIVE
    generation.activated_at = datetime.now(UTC)
    await db.flush()
    return True


async def reconcile_active_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    vector_store: VectorStore,
    published_by: uuid.UUID,
) -> tuple[uuid.UUID, ...]:
    """核对 PG/Qdrant 总量，并原子发布所有已完整文档。"""
    await lock_tenant_nowait(db, tenant_id)
    generation = await db.scalar(
        select(PolicyIndexGeneration)
        .where(PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE)
        .with_for_update()
    )
    if generation is None:
        raise PolicyServiceError(
            code="POLICY_INDEX_UNAVAILABLE", message="当前租户没有 active generation"
        )
    indexes = tuple(
        (
            await db.scalars(
                select(PolicyDocumentIndex)
                .where(
                    PolicyDocumentIndex.index_generation_id == generation.id,
                    PolicyDocumentIndex.status == PolicyDocumentIndexStatus.INDEXING,
                )
                .order_by(PolicyDocumentIndex.document_id)
                .with_for_update()
            )
        ).all()
    )
    completed_new_points = 0
    ready: list[PolicyDocumentIndex] = []
    for document_index in indexes:
        count = int(
            await db.scalar(
                select(func.count())
                .select_from(PolicyIndexJob)
                .where(
                    PolicyIndexJob.document_id == document_index.document_id,
                    PolicyIndexJob.index_generation_id == generation.id,
                    PolicyIndexJob.status == PolicyIndexJobStatus.COMPLETED,
                )
            )
            or 0
        )
        document_index.completed_point_count = count
        indexed_document = await db.scalar(
            select(PolicyDocument).where(PolicyDocument.id == document_index.document_id)
        )
        if indexed_document is None:
            raise PolicyServiceError(
                code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源状态不一致"
            )
        if indexed_document.status == PolicyDocumentStatus.INDEXING:
            completed_new_points += count
        if count == document_index.expected_point_count:
            ready.append(document_index)

    qdrant_count = await vector_store.verify_generation(tenant_id, generation.generation)
    if qdrant_count != generation.completed_point_count + completed_new_points:
        raise PolicyServiceError(
            code="POLICY_INDEX_COUNT_MISMATCH", message="PG 与向量库点数不一致"
        )

    published_ids: list[uuid.UUID] = []
    current = datetime.now(UTC)
    for document_index in ready:
        document = await db.scalar(
            select(PolicyDocument)
            .where(PolicyDocument.id == document_index.document_id)
            .with_for_update()
        )
        if document is None or document.family_id is None:
            raise PolicyServiceError(
                code="POLICY_INDEX_SOURCE_INVALID", message="索引任务来源状态不一致"
            )
        document_index.status = PolicyDocumentIndexStatus.COMPLETED
        if document.status == PolicyDocumentStatus.INDEXING:
            predecessor_index = await db.scalar(
                select(PolicyDocumentIndex)
                .join(PolicyDocument, PolicyDocument.id == PolicyDocumentIndex.document_id)
                .where(
                    PolicyDocumentIndex.index_generation_id == generation.id,
                    PolicyDocument.family_id == document.family_id,
                    PolicyDocument.status == PolicyDocumentStatus.PUBLISHED,
                    PolicyDocument.expiry_date == document.effective_date,
                )
            )
            if (
                predecessor_index is not None
                and predecessor_index.status != PolicyDocumentIndexStatus.COMPLETED
            ):
                document_index.status = PolicyDocumentIndexStatus.INDEXING
                continue
            document.status = PolicyDocumentStatus.PUBLISHED
            document.published_by = published_by
            document.published_at = current
            document.failure_code = None
            generation.expected_point_count += document_index.expected_point_count
            generation.completed_point_count += document_index.expected_point_count
            published_ids.append(document.id)
    await db.flush()
    return tuple(published_ids)


async def _leased_job(db: AsyncSession, claimed: ClaimedIndexJob) -> PolicyIndexJob:
    job = await db.scalar(
        select(PolicyIndexJob)
        .where(
            PolicyIndexJob.id == claimed.job_id,
            PolicyIndexJob.status == PolicyIndexJobStatus.RUNNING,
            PolicyIndexJob.lease_token == claimed.lease_token,
        )
        .with_for_update()
    )
    if job is None:
        raise PolicyServiceError(code="POLICY_INDEX_LEASE_LOST", message="索引任务租约已失效")
    return job
