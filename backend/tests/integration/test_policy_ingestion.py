"""CP-F4.2 制度导入、outbox 恢复与本地候选检索集成测试。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.policies.candidates import (
    BindingQuery,
    CandidateSearchError,
    search_binding_candidates,
)
from app.core.policies.index_worker import (
    claim_index_job,
    execute_claimed_job,
    reconcile_active_generation,
    reconcile_building_generation,
    record_job_failure,
    retry_failed_document,
)
from app.core.policies.models import PolicyLimits
from app.core.policies.service import (
    IndexProfile,
    PolicyServiceError,
    create_building_generation,
    create_policy_family,
    ensure_initial_active_generation,
    publish_policy_document,
    upload_policy_document,
)
from app.core.policies.storage import PrivatePolicyStorage
from app.core.retrieval import DeterministicLocalModels, IndexChunk, SearchCandidate
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.policy import (
    PolicyDocument,
    PolicyDocumentIndex,
    PolicyDocumentIndexStatus,
    PolicyDocumentStatus,
    PolicyIndexGeneration,
    PolicyIndexGenerationStatus,
    PolicyIndexJob,
    PolicyIndexJobStatus,
)
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


class MemoryVectorStore:
    def __init__(self) -> None:
        self.points: dict[uuid.UUID, tuple[uuid.UUID, int, IndexChunk]] = {}
        self.upsert_calls = 0
        self.fail_after_upsert = False
        self.ignore_date_filter = False
        self.forge_chunk_hash = False

    async def upsert_chunks(
        self, tenant_id: uuid.UUID, generation: int, chunks: Sequence[IndexChunk]
    ) -> None:
        self.upsert_calls += 1
        for chunk in chunks:
            self.points[chunk.chunk_id] = (tenant_id, generation, chunk)
        if self.fail_after_upsert:
            self.fail_after_upsert = False
            raise RuntimeError("simulated worker kill after qdrant commit")

    async def search_candidates(
        self,
        tenant_id: uuid.UUID,
        generation: int,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[SearchCandidate, ...]:
        del query
        candidates = []
        for point_id, (point_tenant, point_generation, chunk) in self.points.items():
            if point_tenant != tenant_id or point_generation != generation:
                continue
            if not self.ignore_date_filter and (
                expense_date < chunk.effective_date
                or (chunk.expiry_date is not None and expense_date >= chunk.expiry_date)
            ):
                continue
            candidates.append(
                SearchCandidate(
                    point_id=point_id,
                    family_id=chunk.family_id,
                    document_id=chunk.document_id,
                    clause_id=chunk.clause_id,
                    chunk_id=chunk.chunk_id,
                    vector_score=0.9,
                    effective_day=(chunk.effective_date - date(1970, 1, 1)).days,
                    expiry_day_exclusive=(
                        (date.max - date(1970, 1, 1)).days
                        if chunk.expiry_date is None
                        else (chunk.expiry_date - date(1970, 1, 1)).days
                    ),
                    document_content_sha256=chunk.document_content_sha256,
                    clause_text_sha256=chunk.clause_text_sha256,
                    chunk_text_sha256=(
                        "0" * 64 if self.forge_chunk_hash else chunk.chunk_text_sha256
                    ),
                    index_generation=generation,
                    embedding_model_fingerprint=_profile_fingerprint(),
                    chunker_version=chunk.chunker_version,
                )
            )
        return tuple(candidates[:top_k])

    async def verify_generation(self, tenant_id: uuid.UUID, generation: int) -> int:
        return sum(
            1
            for point_tenant, point_generation, _ in self.points.values()
            if point_tenant == tenant_id and point_generation == generation
        )


async def _seed_tenant(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        session.add(tenant)
        await session.flush()
        user = AppUser(
            tenant_id=tenant.id,
            username=f"{slug}-configurator",
            password_hash="test",
            role=Role.CONFIGURATOR,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        return tenant.id, user.id


def _profile() -> IndexProfile:
    return IndexProfile(
        collection_name="policy-test",
        collection_alias="policy-test-active",
        vector_size=8,
        embedding_model_family="fake",
        embedding_model_id="fake-embed",
        embedding_model_revision="v1",
        rerank_model_family="fake",
        rerank_model_id="fake-rerank",
        rerank_model_revision="v1",
    )


def _profile_fingerprint() -> str:
    from app.core.policies.canonical import canonical_sha256

    profile = _profile()
    return canonical_sha256(
        [
            profile.embedding_model_family,
            profile.embedding_model_id,
            profile.embedding_model_revision,
            profile.vector_size,
        ]
    )


def _limits() -> PolicyLimits:
    return PolicyLimits(
        max_file_bytes=100_000,
        max_pdf_pages=10,
        max_extracted_chars=10_000,
        max_clauses=10,
        chunk_chars=128,
    )


async def _create_indexing_document(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    storage_root: Path,
) -> uuid.UUID:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        family, _ = await create_policy_family(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            stable_key="travel-policy",
            display_name="差旅制度",
        )
        await ensure_initial_active_generation(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            profile=_profile(),
        )
        uploaded = await upload_policy_document(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            family_id=family.id,
            title="差旅制度",
            version="2026",
            effective_date=date(2026, 1, 1),
            expiry_date=date(2027, 1, 1),
            filename="policy.txt",
            content="第一条 交通费\n交通费用标准。\n第二条 住宿费\n住宿费用标准。".encode(),
            limits=_limits(),
            storage=PrivatePolicyStorage(storage_root),
        )
        await publish_policy_document(
            session,
            tenant_id=tenant_id,
            published_by=user_id,
            document_id=uploaded.document.id,
            attempt_limit=3,
        )
        await session.commit()
        return uploaded.document.id


async def _drain_active(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    store: MemoryVectorStore,
) -> tuple[uuid.UUID, ...]:
    now = datetime.now(UTC) + timedelta(seconds=1)
    while True:
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            claimed = await claim_index_job(
                session,
                tenant_id=tenant_id,
                worker_id="drain",
                lease_seconds=10,
                now=now,
            )
            await session.commit()
            if claimed is None:
                break
            await execute_claimed_job(session, claimed=claimed, vector_store=store)
            await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await reconcile_active_generation(
            session,
            tenant_id=tenant_id,
            vector_store=store,
            published_by=user_id,
        )
        await session.commit()
        return result


async def _drain_generation_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    generation_id: uuid.UUID,
    store: MemoryVectorStore,
) -> None:
    now = datetime.now(UTC) + timedelta(seconds=1)
    while True:
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            claimed = await claim_index_job(
                session,
                tenant_id=tenant_id,
                worker_id="generation-drain",
                lease_seconds=10,
                generation_id=generation_id,
                now=now,
            )
            await session.commit()
            if claimed is None:
                return
            assert claimed.generation_id == generation_id
            await execute_claimed_job(session, claimed=claimed, vector_store=store)
            await session.commit()


async def test_upload_is_idempotent_and_worker_recovers_after_qdrant_side_effect(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-recovery")
    document_id = await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    store = MemoryVectorStore()
    base_time = datetime.now(UTC) + timedelta(seconds=1)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await claim_index_job(
            session,
            tenant_id=tenant_id,
            worker_id="worker-a",
            lease_seconds=10,
            now=base_time,
        )
        assert first is not None
        await session.commit()

    store.fail_after_upsert = True
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(RuntimeError, match="simulated worker kill"):
            await execute_claimed_job(session, claimed=first, vector_store=store)
        await session.rollback()
    assert len(store.points) == 1

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        reclaimed = await claim_index_job(
            session,
            tenant_id=tenant_id,
            worker_id="worker-b",
            lease_seconds=10,
            now=base_time + timedelta(seconds=11),
        )
        assert reclaimed is not None
        assert reclaimed.job_id == first.job_id
        await session.commit()
        await execute_claimed_job(session, claimed=reclaimed, vector_store=store)
        await session.commit()
    assert len(store.points) == 1
    assert store.upsert_calls == 2

    while True:
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            claimed = await claim_index_job(
                session,
                tenant_id=tenant_id,
                worker_id="worker-c",
                lease_seconds=10,
                now=base_time + timedelta(seconds=20),
            )
            await session.commit()
            if claimed is None:
                break
            await execute_claimed_job(session, claimed=claimed, vector_store=store)
            await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        published = await reconcile_active_generation(
            session,
            tenant_id=tenant_id,
            vector_store=store,
            published_by=user_id,
        )
        await session.commit()
        assert published == (document_id,)
        document = await session.get(PolicyDocument, document_id)
        assert document is not None
        assert document.status == PolicyDocumentStatus.PUBLISHED
        assert await session.scalar(
            select(func.count())
            .select_from(PolicyIndexJob)
            .where(PolicyIndexJob.status == PolicyIndexJobStatus.COMPLETED)
        ) == len(store.points)

        candidates = await search_binding_candidates(
            session,
            tenant_id=tenant_id,
            expense_date=date(2026, 6, 1),
            query=BindingQuery(rule_kind="limit", reason_code="LIMIT_EXCEEDED"),
            vector_store=store,
            reranker=DeterministicLocalModels(vector_size=8),
            top_k=10,
            cutoff=-1.0,
        )
        assert candidates
        assert all(candidate.document_id == document_id for candidate in candidates)
        store.forge_chunk_hash = True
        assert (
            await search_binding_candidates(
                session,
                tenant_id=tenant_id,
                expense_date=date(2026, 6, 1),
                query=BindingQuery(rule_kind="limit", reason_code="LIMIT_EXCEEDED"),
                vector_store=store,
                reranker=DeterministicLocalModels(vector_size=8),
                top_k=10,
                cutoff=-1.0,
            )
        ) == ()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "policy.document_publish")
            )
            == 1
        )


async def test_candidates_enforce_expiry_boundary(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-expiry")
    await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    store = MemoryVectorStore()
    now = datetime.now(UTC) + timedelta(seconds=1)
    while True:
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            claimed = await claim_index_job(
                session,
                tenant_id=tenant_id,
                worker_id="worker",
                lease_seconds=10,
                now=now,
            )
            await session.commit()
            if claimed is None:
                break
            await execute_claimed_job(session, claimed=claimed, vector_store=store)
            await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        published = await reconcile_active_generation(
            session,
            tenant_id=tenant_id,
            vector_store=store,
            published_by=user_id,
        )
        await session.commit()
        assert len(published) == 1
        store.ignore_date_filter = True
        candidates = await search_binding_candidates(
            session,
            tenant_id=tenant_id,
            expense_date=date(2027, 1, 1),
            query=BindingQuery(rule_kind="limit", reason_code="LIMIT_EXCEEDED"),
            vector_store=store,
            reranker=DeterministicLocalModels(vector_size=8),
            top_k=10,
            cutoff=-1.0,
        )
        assert candidates == ()
        with pytest.raises(CandidateSearchError) as caught:
            await search_binding_candidates(
                session,
                tenant_id=tenant_id,
                expense_date=None,
                query=BindingQuery(rule_kind="limit", reason_code="LIMIT_EXCEEDED"),
                vector_store=store,
                reranker=DeterministicLocalModels(vector_size=8),
                top_k=10,
                cutoff=-1.0,
            )
        assert caught.value.code == "POLICY_EXPENSE_DATE_UNAVAILABLE"


async def test_content_hash_upload_is_idempotent_and_cross_tenant_is_hidden(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_a, user_a = await _seed_tenant(session_factory, slug="policy-idempotent-a")
    tenant_b, _ = await _seed_tenant(session_factory, slug="policy-idempotent-b")
    content = "第一条 交通费\n标准。".encode()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_a)
        family, _ = await create_policy_family(
            session,
            tenant_id=tenant_a,
            created_by=user_a,
            stable_key="travel",
            display_name="差旅",
        )
        first = await upload_policy_document(
            session,
            tenant_id=tenant_a,
            created_by=user_a,
            family_id=family.id,
            title="差旅",
            version="v1",
            effective_date=date(2026, 1, 1),
            expiry_date=None,
            filename="a.txt",
            content=content,
            limits=_limits(),
            storage=PrivatePolicyStorage(tmp_path),
        )
        await session.commit()
        second = await upload_policy_document(
            session,
            tenant_id=tenant_a,
            created_by=user_a,
            family_id=family.id,
            title="差旅",
            version="v1",
            effective_date=date(2026, 1, 1),
            expiry_date=None,
            filename="renamed.txt",
            content=content,
            limits=_limits(),
            storage=PrivatePolicyStorage(tmp_path),
        )
        await session.commit()
        assert second.created is False
        assert second.document.id == first.document.id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "policy.document_upload")
            )
            == 1
        )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_b)
        assert await session.get(PolicyDocument, first.document.id) is None


async def test_terminal_failure_can_be_manually_retried_without_duplicate_jobs(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-retry")
    document_id = await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    now = datetime.now(UTC) + timedelta(seconds=1)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        jobs = tuple(
            (
                await session.scalars(
                    select(PolicyIndexJob).where(PolicyIndexJob.document_id == document_id)
                )
            ).all()
        )
        assert jobs
        for job in jobs:
            job.attempt_limit = 1
        await session.commit()
        claimed = await claim_index_job(
            session,
            tenant_id=tenant_id,
            worker_id="failer",
            lease_seconds=10,
            now=now,
        )
        assert claimed is not None
        await session.commit()
        terminal = await record_job_failure(
            session,
            claimed=claimed,
            failure_code="POLICY_VECTOR_UPSERT_FAILED",
            retry_delay_seconds=0,
            now=now,
        )
        await session.commit()
        assert terminal is True
        count_before = await session.scalar(select(func.count()).select_from(PolicyIndexJob))
        retried = await retry_failed_document(
            session,
            tenant_id=tenant_id,
            actor_id=user_id,
            document_id=document_id,
            now=now,
        )
        await session.commit()
        assert retried == len(jobs)
        assert (
            await session.scalar(select(func.count()).select_from(PolicyIndexJob)) == count_before
        )
        document = await session.get(PolicyDocument, document_id)
        assert document is not None and document.status == PolicyDocumentStatus.INDEXING


async def test_building_generation_freezes_manifest_and_switches_atomically(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-generation")
    await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    active_store = MemoryVectorStore()
    assert (
        len(
            await _drain_active(
                session_factory, tenant_id=tenant_id, user_id=user_id, store=active_store
            )
        )
        == 1
    )

    next_profile = _profile().model_copy(
        update={
            "collection_name": "policy-test-v2",
            "collection_alias": "policy-test-v2-active",
            "embedding_model_revision": "v2",
        }
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        generation = await create_building_generation(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            profile=next_profile,
            attempt_limit=3,
        )
        frozen_expected = generation.expected_point_count
        await session.commit()
        assert frozen_expected > 0

    build_store = MemoryVectorStore()
    now = datetime.now(UTC) + timedelta(seconds=1)
    while True:
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            claimed = await claim_index_job(
                session,
                tenant_id=tenant_id,
                worker_id="builder",
                lease_seconds=10,
                now=now,
            )
            await session.commit()
            if claimed is None:
                break
            await execute_claimed_job(session, claimed=claimed, vector_store=build_store)
            await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        switched = await reconcile_building_generation(
            session,
            tenant_id=tenant_id,
            generation_id=generation.id,
            vector_store=build_store,
            attempt_limit=3,
        )
        await session.commit()
        assert switched is True
        generations = tuple(
            (
                await session.scalars(
                    select(PolicyIndexGeneration).order_by(PolicyIndexGeneration.generation)
                )
            ).all()
        )
        assert [item.status for item in generations] == [
            PolicyIndexGenerationStatus.RETIRED,
            PolicyIndexGenerationStatus.ACTIVE,
        ]
        assert generations[1].completed_point_count == frozen_expected
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PolicyDocumentIndex)
                .where(PolicyDocumentIndex.status == PolicyDocumentIndexStatus.INDEXING)
            )
            == 0
        )


async def test_publish_rejects_overlap_and_accepts_touching_half_open_interval(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-interval")
    first_id = await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    store = MemoryVectorStore()
    assert await _drain_active(
        session_factory, tenant_id=tenant_id, user_id=user_id, store=store
    ) == (first_id,)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await session.get(PolicyDocument, first_id)
        assert first is not None and first.family_id is not None
        overlapping = await upload_policy_document(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            family_id=first.family_id,
            title="差旅制度",
            version="overlap",
            effective_date=date(2026, 6, 1),
            expiry_date=date(2028, 1, 1),
            filename="overlap.txt",
            content="第一条 重叠版本\n标准。".encode(),
            limits=_limits(),
            storage=PrivatePolicyStorage(tmp_path),
        )
        with pytest.raises(PolicyServiceError) as caught:
            await publish_policy_document(
                session,
                tenant_id=tenant_id,
                published_by=user_id,
                document_id=overlapping.document.id,
                attempt_limit=3,
            )
        assert caught.value.code == "POLICY_INTERVAL_OVERLAP"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await session.get(PolicyDocument, first_id)
        assert first is not None and first.family_id is not None
        touching = await upload_policy_document(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            family_id=first.family_id,
            title="差旅制度",
            version="2027",
            effective_date=date(2027, 1, 1),
            expiry_date=date(2028, 1, 1),
            filename="2027.txt",
            content="第一条 新版本\n标准。".encode(),
            limits=_limits(),
            storage=PrivatePolicyStorage(tmp_path),
        )
        published = await publish_policy_document(
            session,
            tenant_id=tenant_id,
            published_by=user_id,
            document_id=touching.document.id,
            attempt_limit=3,
        )
        await session.commit()
        assert published.status == PolicyDocumentStatus.INDEXING


async def test_building_generation_adds_explicit_delta_before_switch(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    tenant_id, user_id = await _seed_tenant(session_factory, slug="policy-generation-delta")
    first_id = await _create_indexing_document(
        session_factory,
        tenant_id=tenant_id,
        user_id=user_id,
        storage_root=tmp_path,
    )
    active_store = MemoryVectorStore()
    assert await _drain_active(
        session_factory, tenant_id=tenant_id, user_id=user_id, store=active_store
    ) == (first_id,)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        active = await session.scalar(
            select(PolicyIndexGeneration).where(
                PolicyIndexGeneration.status == PolicyIndexGenerationStatus.ACTIVE
            )
        )
        first = await session.get(PolicyDocument, first_id)
        assert active is not None
        assert first is not None and first.family_id is not None
        building = await create_building_generation(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            profile=_profile().model_copy(
                update={
                    "collection_name": "policy-delta-v2",
                    "collection_alias": "policy-delta-v2-active",
                    "embedding_model_revision": "v2",
                }
            ),
            attempt_limit=3,
        )
        frozen_count = building.expected_point_count
        second = await upload_policy_document(
            session,
            tenant_id=tenant_id,
            created_by=user_id,
            family_id=first.family_id,
            title="差旅制度",
            version="2027",
            effective_date=date(2027, 1, 1),
            expiry_date=date(2028, 1, 1),
            filename="delta.txt",
            content="第一条 新制度\n新标准。".encode(),
            limits=_limits(),
            storage=PrivatePolicyStorage(tmp_path),
        )
        await publish_policy_document(
            session,
            tenant_id=tenant_id,
            published_by=user_id,
            document_id=second.document.id,
            attempt_limit=3,
        )
        await session.commit()
        active_id = active.id
        building_id = building.id

    await _drain_generation_jobs(
        session_factory,
        tenant_id=tenant_id,
        generation_id=active_id,
        store=active_store,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        published = await reconcile_active_generation(
            session,
            tenant_id=tenant_id,
            vector_store=active_store,
            published_by=user_id,
        )
        await session.commit()
        if not published:
            published = await reconcile_active_generation(
                session,
                tenant_id=tenant_id,
                vector_store=active_store,
                published_by=user_id,
            )
            await session.commit()
        assert published == (second.document.id,)

    build_store = MemoryVectorStore()
    await _drain_generation_jobs(
        session_factory,
        tenant_id=tenant_id,
        generation_id=building_id,
        store=build_store,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert (
            await reconcile_building_generation(
                session,
                tenant_id=tenant_id,
                generation_id=building_id,
                vector_store=build_store,
                attempt_limit=3,
            )
            is False
        )
        await session.commit()
        refreshed = await session.get(PolicyIndexGeneration, building_id)
        assert refreshed is not None
        assert refreshed.manifest_revision == 2
        assert refreshed.expected_point_count > frozen_count

    await _drain_generation_jobs(
        session_factory,
        tenant_id=tenant_id,
        generation_id=building_id,
        store=build_store,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert (
            await reconcile_building_generation(
                session,
                tenant_id=tenant_id,
                generation_id=building_id,
                vector_store=build_store,
                attempt_limit=3,
            )
            is True
        )
        await session.commit()
