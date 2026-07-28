"""真实 Qdrant 的租户、代际与半开日期过滤集成测试。"""

import uuid
from datetime import date

import pytest
from qdrant_client import AsyncQdrantClient, models

from app.core.retrieval import DeterministicLocalModels, IndexChunk, QdrantVectorStore
from app.core.retrieval.vector_store import VectorStoreError

pytestmark = pytest.mark.integration


async def test_qdrant_upsert_is_idempotent_and_filters_expiry_boundary() -> None:
    collection = f"expenseguard_test_{uuid.uuid4().hex}"
    client = AsyncQdrantClient(url="http://127.0.0.1:6333", timeout=10)
    provider = DeterministicLocalModels(vector_size=8)
    fingerprint = "a" * 64
    store = QdrantVectorStore(
        collection_name=collection,
        embedding_model_fingerprint=fingerprint,
        chunker_version="policy-chunker-v1",
        models_provider=provider,
        client=client,
    )
    tenant_id = uuid.uuid4()
    chunk = IndexChunk(
        family_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        clause_id=uuid.uuid4(),
        chunk_id=uuid.uuid4(),
        text="交通费用标准",
        effective_date=date(2026, 1, 1),
        expiry_date=date(2027, 1, 1),
        document_content_sha256="b" * 64,
        clause_text_sha256="c" * 64,
        chunk_text_sha256="d" * 64,
        chunker_version="policy-chunker-v1",
    )
    try:
        await store.ensure_collection()
        await store.upsert_chunks(tenant_id, 1, [chunk])
        await store.upsert_chunks(tenant_id, 1, [chunk])

        assert await store.verify_generation(tenant_id, 1) == 1
        inside = await store.search_candidates(tenant_id, 1, date(2026, 12, 31), "交通", 10)
        expired = await store.search_candidates(tenant_id, 1, date(2027, 1, 1), "交通", 10)
        other_tenant = await store.search_candidates(
            uuid.uuid4(), 1, date(2026, 12, 31), "交通", 10
        )
        assert [candidate.chunk_id for candidate in inside] == [chunk.chunk_id]
        assert expired == ()
        assert other_tenant == ()
    finally:
        if await client.collection_exists(collection):
            await client.delete_collection(collection)
        await client.close()


async def test_existing_qdrant_collection_with_wrong_dimension_fails_closed() -> None:
    collection = f"expenseguard_test_{uuid.uuid4().hex}"
    client = AsyncQdrantClient(url="http://127.0.0.1:6333", timeout=10)
    await client.create_collection(
        collection,
        vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE),
    )
    store = QdrantVectorStore(
        collection_name=collection,
        embedding_model_fingerprint="a" * 64,
        chunker_version="policy-chunker-v1",
        models_provider=DeterministicLocalModels(vector_size=8),
        client=client,
    )
    try:
        with pytest.raises(VectorStoreError) as caught:
            await store.ensure_collection()
        assert caught.value.code == "POLICY_VECTOR_COLLECTION_MISMATCH"
    finally:
        await client.delete_collection(collection)
        await client.close()
