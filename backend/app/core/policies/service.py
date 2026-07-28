"""制度 family、文档预览与发布编排服务。"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.policies.canonical import canonical_sha256
from app.core.policies.models import ParsedPolicyDocument, PolicyLimits
from app.core.policies.parser import CHUNKER_VERSION, PARSER_VERSION, parse_policy_document
from app.core.policies.storage import PrivatePolicyStorage
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import current_tenant
from app.db.models.policy import (
    PolicyChunk,
    PolicyClause,
    PolicyDocument,
    PolicyDocumentIndex,
    PolicyDocumentIndexStatus,
    PolicyDocumentStatus,
    PolicyFamily,
    PolicyIndexGeneration,
    PolicyIndexGenerationStatus,
    PolicyIndexJob,
    PolicyIndexJobStatus,
    PolicyIndexOperation,
    PolicySourceBlob,
)

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class PolicyServiceError(ExpenseGuardError):
    """可稳定映射到后续 API 的制度领域错误。"""

    status_code = 409


class UploadedPolicy(BaseModel):
    """上传/解析完成的 draft。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    document: PolicyDocument
    parsed: ParsedPolicyDocument
    created: bool


class PolicyPreview(BaseModel):
    """不允许改写的条款/chunk 预览。"""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    document: PolicyDocument
    clauses: tuple[PolicyClause, ...]
    chunks: tuple[PolicyChunk, ...]


class IndexProfile(BaseModel):
    """冻结到 generation 的本地检索 provenance。"""

    model_config = ConfigDict(frozen=True)

    collection_name: str
    collection_alias: str
    vector_size: int
    distance: str = "Cosine"
    embedding_model_family: str
    embedding_model_id: str
    embedding_model_revision: str
    rerank_model_family: str
    rerank_model_id: str
    rerank_model_revision: str


async def create_policy_family(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    stable_key: str,
    display_name: str,
) -> tuple[PolicyFamily, bool]:
    """创建或幂等复用 stable_key 完全相同的 family。"""
    await _lock_tenant(db, tenant_id)
    existing = await db.scalar(select(PolicyFamily).where(PolicyFamily.stable_key == stable_key))
    if existing is not None:
        if existing.display_name != display_name:
            raise PolicyServiceError(
                code="POLICY_FAMILY_CONFLICT", message="制度标识已存在且展示名不同"
            )
        return existing, False
    family = PolicyFamily(
        tenant_id=tenant_id,
        stable_key=stable_key,
        display_name=display_name,
        created_by=created_by,
    )
    db.add(family)
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.family_create",
        actor_id=created_by,
        target_type="policy_family",
        target_id=str(family.id),
        payload={"family_id": str(family.id), "stable_key": stable_key},
    )
    return family, True


async def upload_policy_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    family_id: uuid.UUID,
    title: str,
    version: str,
    effective_date: date,
    expiry_date: date | None,
    filename: str,
    content: bytes,
    limits: PolicyLimits,
    storage: PrivatePolicyStorage,
) -> UploadedPolicy:
    """存储、解析并以一个事务追加 draft clause/chunk。"""
    if current_tenant(db.sync_session) != tenant_id:
        raise RuntimeError("制度上传租户与会话租户上下文不一致")
    parsed = parse_policy_document(filename=filename, content=content, limits=limits)
    stored = storage.store(
        tenant_id=tenant_id,
        content=content,
        suffix=Path(filename).suffix,
        max_bytes=limits.max_file_bytes,
    )
    await _lock_tenant(db, tenant_id)
    family = await db.scalar(select(PolicyFamily).where(PolicyFamily.id == family_id))
    if family is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="制度 family 不存在")

    existing = await db.scalar(
        select(PolicyDocument).where(
            PolicyDocument.family_id == family_id,
            PolicyDocument.version == version,
        )
    )
    if existing is not None:
        if (
            existing.content_sha256 != stored.content_sha256
            or existing.effective_date != effective_date
            or existing.expiry_date != expiry_date
            or existing.title != title
        ):
            raise PolicyServiceError(
                code="POLICY_VERSION_CONFLICT", message="制度版本已存在但内容或元数据不同"
            )
        return UploadedPolicy(document=existing, parsed=parsed, created=False)

    blob = await db.scalar(
        select(PolicySourceBlob).where(PolicySourceBlob.content_sha256 == stored.content_sha256)
    )
    if blob is None:
        blob = PolicySourceBlob(
            tenant_id=tenant_id,
            storage_key=stored.storage_key,
            mime_type=parsed.mime_type,
            size_bytes=stored.size_bytes,
            content_sha256=stored.content_sha256,
            created_by=created_by,
        )
        db.add(blob)
        await db.flush()

    document = PolicyDocument(
        tenant_id=tenant_id,
        family_id=family_id,
        source_blob_id=blob.id,
        title=title,
        version=version,
        effective_date=effective_date,
        expiry_date=expiry_date,
        source_filename=Path(filename).name,
        content_sha256=stored.content_sha256,
        mime_type=parsed.mime_type,
        size_bytes=stored.size_bytes,
        extracted_text_sha256=parsed.extracted_text_sha256,
        parser_version=parsed.parser_version,
        chunker_version=parsed.chunker_version,
        status=PolicyDocumentStatus.DRAFT,
        created_by=created_by,
    )
    db.add(document)
    await db.flush()

    clauses_by_ordinal: dict[int, PolicyClause] = {}
    for clause_draft in parsed.clauses:
        clause = PolicyClause(
            tenant_id=tenant_id,
            document_id=document.id,
            family_id=family_id,
            clause_no=clause_draft.clause_no,
            hierarchy_path=clause_draft.hierarchy_path,
            text=clause_draft.text,
            ordinal=clause_draft.ordinal,
            text_sha256=clause_draft.text_sha256,
            source_locator_json=clause_draft.source_locator.model_dump(mode="json"),
            source_start=clause_draft.source_start,
            source_end=clause_draft.source_end,
        )
        db.add(clause)
        clauses_by_ordinal[clause_draft.ordinal] = clause
    await db.flush()
    for chunk_draft in parsed.chunks:
        clause = clauses_by_ordinal[chunk_draft.clause_ordinal]
        db.add(
            PolicyChunk(
                tenant_id=tenant_id,
                document_id=document.id,
                clause_id=clause.id,
                chunk_no=chunk_draft.chunk_no,
                start_offset=chunk_draft.start_offset,
                end_offset=chunk_draft.end_offset,
                text=chunk_draft.text,
                text_sha256=chunk_draft.text_sha256,
                chunker_version=parsed.chunker_version,
            )
        )
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.document_upload",
        actor_id=created_by,
        target_type="policy_document",
        target_id=str(document.id),
        payload={
            "document_id": str(document.id),
            "family_id": str(family_id),
            "version": version,
            "content_sha256": stored.content_sha256,
            "parse_fingerprint": parsed.parse_fingerprint,
            "clause_count": len(parsed.clauses),
            "chunk_count": len(parsed.chunks),
        },
    )
    return UploadedPolicy(document=document, parsed=parsed, created=True)


async def preview_policy_document(db: AsyncSession, document_id: uuid.UUID) -> PolicyPreview:
    document = await db.scalar(select(PolicyDocument).where(PolicyDocument.id == document_id))
    if document is None or document.status == PolicyDocumentStatus.LEGACY_UNPUBLISHED:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="制度文档不存在")
    clauses = tuple(
        (
            await db.scalars(
                select(PolicyClause)
                .where(PolicyClause.document_id == document_id)
                .order_by(PolicyClause.ordinal, PolicyClause.id)
            )
        ).all()
    )
    chunks = tuple(
        (
            await db.scalars(
                select(PolicyChunk)
                .where(PolicyChunk.document_id == document_id)
                .order_by(PolicyChunk.clause_id, PolicyChunk.chunk_no, PolicyChunk.id)
            )
        ).all()
    )
    return PolicyPreview(document=document, clauses=clauses, chunks=chunks)


async def ensure_initial_active_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    profile: IndexProfile,
) -> PolicyIndexGeneration:
    """仅在租户还没有 generation 时创建空的 active generation。"""
    await _lock_tenant(db, tenant_id)
    existing = await db.scalar(
        select(PolicyIndexGeneration).order_by(PolicyIndexGeneration.generation.desc()).limit(1)
    )
    if existing is not None:
        return existing
    empty_manifest = canonical_sha256([])
    now = datetime.now(UTC)
    generation = PolicyIndexGeneration(
        tenant_id=tenant_id,
        generation=1,
        manifest_revision=1,
        collection_name=profile.collection_name,
        collection_alias=profile.collection_alias,
        vector_size=profile.vector_size,
        distance=profile.distance,
        embedding_model_family=profile.embedding_model_family,
        embedding_model_id=profile.embedding_model_id,
        embedding_model_revision=profile.embedding_model_revision,
        embedding_model_fingerprint=canonical_sha256(
            [
                profile.embedding_model_family,
                profile.embedding_model_id,
                profile.embedding_model_revision,
                profile.vector_size,
            ]
        ),
        rerank_model_family=profile.rerank_model_family,
        rerank_model_id=profile.rerank_model_id,
        rerank_model_revision=profile.rerank_model_revision,
        rerank_model_fingerprint=canonical_sha256(
            [
                profile.rerank_model_family,
                profile.rerank_model_id,
                profile.rerank_model_revision,
            ]
        ),
        parser_version=PARSER_VERSION,
        chunker_version=CHUNKER_VERSION,
        source_manifest_fingerprint=empty_manifest,
        expected_point_count=0,
        completed_point_count=0,
        status=PolicyIndexGenerationStatus.ACTIVE,
        activated_at=now,
        created_by=created_by,
    )
    db.add(generation)
    await db.flush()
    return generation


async def create_building_generation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    profile: IndexProfile,
    attempt_limit: int,
) -> PolicyIndexGeneration:
    """冻结当前 published manifest，创建不混入后续发布的 building generation。"""
    await _lock_tenant(db, tenant_id)
    existing_build = await db.scalar(
        select(PolicyIndexGeneration).where(
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.BUILDING
        )
    )
    if existing_build is not None:
        raise PolicyServiceError(code="POLICY_INDEX_IN_PROGRESS", message="已有索引代际正在构建")
    latest_number = int(await db.scalar(select(func.max(PolicyIndexGeneration.generation))) or 0)
    manifest_rows = await _published_manifest_rows(db)
    manifest = canonical_sha256([[str(chunk.id), chunk.text_sha256] for _, chunk in manifest_rows])
    generation = PolicyIndexGeneration(
        tenant_id=tenant_id,
        generation=latest_number + 1,
        manifest_revision=1,
        collection_name=profile.collection_name,
        collection_alias=profile.collection_alias,
        vector_size=profile.vector_size,
        distance=profile.distance,
        embedding_model_family=profile.embedding_model_family,
        embedding_model_id=profile.embedding_model_id,
        embedding_model_revision=profile.embedding_model_revision,
        embedding_model_fingerprint=canonical_sha256(
            [
                profile.embedding_model_family,
                profile.embedding_model_id,
                profile.embedding_model_revision,
                profile.vector_size,
            ]
        ),
        rerank_model_family=profile.rerank_model_family,
        rerank_model_id=profile.rerank_model_id,
        rerank_model_revision=profile.rerank_model_revision,
        rerank_model_fingerprint=canonical_sha256(
            [
                profile.rerank_model_family,
                profile.rerank_model_id,
                profile.rerank_model_revision,
            ]
        ),
        parser_version=PARSER_VERSION,
        chunker_version=CHUNKER_VERSION,
        source_manifest_fingerprint=manifest,
        expected_point_count=len(manifest_rows),
        completed_point_count=0,
        status=PolicyIndexGenerationStatus.BUILDING,
        created_by=created_by,
    )
    db.add(generation)
    await db.flush()
    await _append_generation_jobs(
        db,
        tenant_id=tenant_id,
        generation=generation,
        rows=manifest_rows,
        attempt_limit=attempt_limit,
    )
    return generation


async def publish_policy_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    published_by: uuid.UUID,
    document_id: uuid.UUID,
    attempt_limit: int,
) -> PolicyDocument:
    """冻结 draft manifest，并向 active generation 追加幂等 outbox。"""
    await _lock_tenant(db, tenant_id)
    document = await db.scalar(
        select(PolicyDocument).where(PolicyDocument.id == document_id).with_for_update()
    )
    if document is None:
        raise NotFoundError(code="POLICY_NOT_FOUND", message="制度文档不存在")
    if document.status in {PolicyDocumentStatus.INDEXING, PolicyDocumentStatus.PUBLISHED}:
        return document
    if document.status != PolicyDocumentStatus.DRAFT:
        raise PolicyServiceError(code="POLICY_STATE_INVALID", message="制度当前状态不可发布")

    predecessor = await _validate_publish_interval(db, document)
    generation = await db.scalar(
        select(PolicyIndexGeneration).where(
            PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE
        )
    )
    if generation is None:
        raise PolicyServiceError(
            code="POLICY_INDEX_UNAVAILABLE", message="当前租户没有可用的本地索引代际"
        )
    if (
        generation.parser_version != document.parser_version
        or generation.chunker_version != document.chunker_version
    ):
        raise PolicyServiceError(
            code="POLICY_INDEX_PROVENANCE_MISMATCH", message="制度解析版本与索引代际不一致"
        )
    chunks = tuple(
        (
            await db.scalars(
                select(PolicyChunk)
                .where(PolicyChunk.document_id == document.id)
                .order_by(PolicyChunk.clause_id, PolicyChunk.chunk_no, PolicyChunk.id)
            )
        ).all()
    )
    if not chunks:
        raise PolicyServiceError(code="POLICY_CLAUSE_INVALID", message="制度没有可索引条款")
    manifest = canonical_sha256(
        [
            [str(chunk.id), chunk.text_sha256, chunk.start_offset, chunk.end_offset]
            for chunk in chunks
        ]
    )
    document_index = PolicyDocumentIndex(
        tenant_id=tenant_id,
        document_id=document.id,
        index_generation_id=generation.id,
        status=PolicyDocumentIndexStatus.INDEXING,
        expected_point_count=len(chunks),
        completed_point_count=0,
        manifest_fingerprint=manifest,
    )
    db.add(document_index)
    now = datetime.now(UTC)
    for chunk in chunks:
        db.add(
            PolicyIndexJob(
                tenant_id=tenant_id,
                document_id=document.id,
                chunk_id=chunk.id,
                index_generation_id=generation.id,
                operation=PolicyIndexOperation.UPSERT,
                status=PolicyIndexJobStatus.PENDING,
                attempt_count=0,
                attempt_limit=attempt_limit,
                available_at=now,
            )
        )
    if predecessor is not None:
        predecessor.expiry_date = document.effective_date
        predecessor_index = await db.scalar(
            select(PolicyDocumentIndex).where(
                PolicyDocumentIndex.document_id == predecessor.id,
                PolicyDocumentIndex.index_generation_id == generation.id,
            )
        )
        if predecessor_index is None:
            raise PolicyServiceError(
                code="POLICY_INDEX_SOURCE_INVALID", message="前序制度缺少 active generation 索引"
            )
        predecessor_index.status = PolicyDocumentIndexStatus.INDEXING
        predecessor_index.completed_point_count = 0
        predecessor_jobs = tuple(
            (
                await db.scalars(
                    select(PolicyIndexJob).where(
                        PolicyIndexJob.document_id == predecessor.id,
                        PolicyIndexJob.index_generation_id == generation.id,
                    )
                )
            ).all()
        )
        if len(predecessor_jobs) != predecessor_index.expected_point_count:
            raise PolicyServiceError(
                code="POLICY_INDEX_SOURCE_INVALID", message="前序制度索引任务数量不一致"
            )
        for job in predecessor_jobs:
            job.status = PolicyIndexJobStatus.PENDING
            job.attempt_count = 0
            job.available_at = now
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.last_failure_code = None
    document.status = PolicyDocumentStatus.INDEXING
    await db.flush()
    await write_audit(
        db,
        tenant_id=tenant_id,
        action="policy.document_publish",
        actor_id=published_by,
        target_type="policy_document",
        target_id=str(document.id),
        payload={
            "document_id": str(document.id),
            "family_id": str(document.family_id),
            "version": document.version,
            "content_sha256": document.content_sha256,
            "index_generation_id": str(generation.id),
            "manifest_fingerprint": manifest,
            "point_count": len(chunks),
            "status": PolicyDocumentStatus.INDEXING.value,
        },
    )
    return document


async def _validate_publish_interval(
    db: AsyncSession, document: PolicyDocument
) -> PolicyDocument | None:
    candidates = tuple(
        (
            await db.scalars(
                select(PolicyDocument).where(
                    PolicyDocument.family_id == document.family_id,
                    PolicyDocument.id != document.id,
                    PolicyDocument.status.in_(
                        [PolicyDocumentStatus.INDEXING, PolicyDocumentStatus.PUBLISHED]
                    ),
                )
            )
        ).all()
    )
    predecessor: PolicyDocument | None = None
    for candidate in candidates:
        if not _intervals_overlap(
            document.effective_date,
            document.expiry_date,
            candidate.effective_date,
            candidate.expiry_date,
        ):
            continue
        is_closable_predecessor = (
            candidate.status == PolicyDocumentStatus.PUBLISHED
            and candidate.expiry_date is None
            and candidate.effective_date < document.effective_date
        )
        if not is_closable_predecessor:
            raise PolicyServiceError(
                code="POLICY_INTERVAL_OVERLAP", message="制度生效区间与既有版本重叠"
            )
        if predecessor is not None:
            raise PolicyServiceError(
                code="POLICY_INTERVAL_OVERLAP", message="存在多个可关闭的前序制度版本"
            )
        predecessor = candidate
    return predecessor


def _intervals_overlap(
    left_start: date,
    left_end: date | None,
    right_start: date,
    right_end: date | None,
) -> bool:
    return (right_end is None or left_start < right_end) and (
        left_end is None or right_start < left_end
    )


async def _published_manifest_rows(
    db: AsyncSession,
) -> tuple[tuple[PolicyDocument, PolicyChunk], ...]:
    rows = (
        await db.execute(
            select(PolicyDocument, PolicyChunk)
            .join(PolicyChunk, PolicyChunk.document_id == PolicyDocument.id)
            .where(PolicyDocument.status == PolicyDocumentStatus.PUBLISHED)
            .order_by(
                PolicyDocument.id,
                PolicyChunk.clause_id,
                PolicyChunk.chunk_no,
                PolicyChunk.id,
            )
        )
    ).all()
    return tuple((row[0], row[1]) for row in rows)


async def _append_generation_jobs(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    generation: PolicyIndexGeneration,
    rows: tuple[tuple[PolicyDocument, PolicyChunk], ...],
    attempt_limit: int,
) -> None:
    now = datetime.now(UTC)
    grouped: dict[uuid.UUID, list[PolicyChunk]] = {}
    for document, chunk in rows:
        grouped.setdefault(document.id, []).append(chunk)
    for document_id, chunks in grouped.items():
        db.add(
            PolicyDocumentIndex(
                tenant_id=tenant_id,
                document_id=document_id,
                index_generation_id=generation.id,
                status=PolicyDocumentIndexStatus.INDEXING,
                expected_point_count=len(chunks),
                completed_point_count=0,
                manifest_fingerprint=canonical_sha256(
                    [[str(chunk.id), chunk.text_sha256] for chunk in chunks]
                ),
            )
        )
        for chunk in chunks:
            db.add(
                PolicyIndexJob(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    chunk_id=chunk.id,
                    index_generation_id=generation.id,
                    operation=PolicyIndexOperation.UPSERT,
                    status=PolicyIndexJobStatus.PENDING,
                    attempt_count=0,
                    attempt_limit=attempt_limit,
                    available_at=now,
                )
            )
    await db.flush()


async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    try:
        await lock_tenant_nowait(db, tenant_id=tenant_id)
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise PolicyServiceError(
                code="POLICY_CHANGE_IN_PROGRESS", message="该租户的制度配置正在变更，请稍后重试"
            ) from exc
        raise
