"""严格租户/代际/时间过滤的 Qdrant VectorStore。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, Protocol, cast

from pydantic import TypeAdapter, ValidationError
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.models import Distance, VectorParams

from app.core.errors import ExpenseGuardError
from app.core.retrieval.models import IndexChunk, SearchCandidate
from app.core.retrieval.providers import LocalModelProvider

_EPOCH = date(1970, 1, 1)
_MAX_DAY = (date.max - _EPOCH).days
_UUID_ADAPTER = TypeAdapter(uuid.UUID)


class VectorStoreError(ExpenseGuardError):
    """向量库不可用或返回不可信 payload。"""

    status_code = 503


class VectorStore(Protocol):
    """业务层唯一允许依赖的向量存储边界。"""

    async def upsert_chunks(
        self, tenant_id: uuid.UUID, generation: int, chunks: Sequence[IndexChunk]
    ) -> None: ...

    async def search_candidates(
        self,
        tenant_id: uuid.UUID,
        generation: int,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[SearchCandidate, ...]: ...

    async def verify_generation(self, tenant_id: uuid.UUID, generation: int) -> int: ...


class QdrantVectorStore:
    """Qdrant 实现；payload 仅作未信任候选，PG 仍是最终真源。"""

    def __init__(
        self,
        *,
        collection_name: str,
        embedding_model_fingerprint: str,
        chunker_version: str,
        models_provider: LocalModelProvider,
        client: AsyncQdrantClient,
    ) -> None:
        self._collection_name = collection_name
        self._embedding_model_fingerprint = embedding_model_fingerprint
        self._chunker_version = chunker_version
        self._models = models_provider
        self._client = client

    async def ensure_collection(self) -> None:
        try:
            exists = await self._client.collection_exists(self._collection_name)
            if not exists:
                await self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=VectorParams(
                        size=self._models.vector_size,
                        distance=Distance.COSINE,
                    ),
                )
            info = await self._client.get_collection(self._collection_name)
            vectors = info.config.params.vectors
            if (
                not isinstance(vectors, VectorParams)
                or vectors.size != self._models.vector_size
                or vectors.distance != Distance.COSINE
            ):
                raise VectorStoreError(
                    code="POLICY_VECTOR_COLLECTION_MISMATCH",
                    message="Qdrant collection 向量维度或距离与 generation 不一致",
                )
            for field_name, schema in (
                ("tenant_id", models.PayloadSchemaType.KEYWORD),
                ("index_generation", models.PayloadSchemaType.INTEGER),
                ("effective_day", models.PayloadSchemaType.INTEGER),
                ("expiry_day_exclusive", models.PayloadSchemaType.INTEGER),
            ):
                if field_name not in info.payload_schema:
                    await self._client.create_payload_index(
                        collection_name=self._collection_name,
                        field_name=field_name,
                        field_schema=schema,
                        wait=True,
                    )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                code="POLICY_VECTOR_STORE_UNAVAILABLE", message="本地向量库不可用"
            ) from exc

    async def close(self) -> None:
        """Release the per-request asynchronous Qdrant transport."""
        await self._client.close()

    async def upsert_chunks(
        self, tenant_id: uuid.UUID, generation: int, chunks: Sequence[IndexChunk]
    ) -> None:
        if not chunks:
            return
        if generation <= 0 or any(
            chunk.chunker_version != self._chunker_version for chunk in chunks
        ):
            raise VectorStoreError(
                code="POLICY_VECTOR_PROVENANCE_MISMATCH",
                message="chunk 与目标索引代际 provenance 不一致",
            )
        vectors = await self._models.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise VectorStoreError(
                code="POLICY_EMBEDDING_INVALID", message="embedding 数量与 chunk 不一致"
            )
        points = [
            models.PointStruct(
                id=str(chunk.chunk_id),
                vector=list(vector),
                payload=_payload(tenant_id, generation, chunk, self._embedding_model_fingerprint),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        try:
            await self._client.upsert(
                collection_name=self._collection_name,
                points=points,
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(
                code="POLICY_VECTOR_UPSERT_FAILED", message="本地向量写入失败"
            ) from exc

    async def search_candidates(
        self,
        tenant_id: uuid.UUID,
        generation: int,
        expense_date: date,
        query: str,
        top_k: int,
    ) -> tuple[SearchCandidate, ...]:
        if not query.strip() or top_k <= 0:
            raise ValueError("query 必须非空且 top_k 必须为正数")
        query_vector = (await self._models.embed([query]))[0]
        day = _epoch_day(expense_date)
        query_filter = _generation_filter(tenant_id, generation, day)
        try:
            response = await self._client.query_points(
                collection_name=self._collection_name,
                query=list(query_vector),
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise VectorStoreError(
                code="POLICY_VECTOR_SEARCH_FAILED", message="本地向量检索失败"
            ) from exc
        parsed: list[SearchCandidate] = []
        for point in response.points:
            try:
                payload = cast(dict[str, Any], point.payload or {})
                parsed.append(
                    SearchCandidate(
                        point_id=_UUID_ADAPTER.validate_python(point.id),
                        family_id=payload["family_id"],
                        document_id=payload["document_id"],
                        clause_id=payload["clause_id"],
                        chunk_id=payload["chunk_id"],
                        vector_score=float(point.score),
                        effective_day=payload["effective_day"],
                        expiry_day_exclusive=payload["expiry_day_exclusive"],
                        document_content_sha256=payload["document_content_sha256"],
                        clause_text_sha256=payload["clause_text_sha256"],
                        chunk_text_sha256=payload["chunk_text_sha256"],
                        index_generation=payload["index_generation"],
                        embedding_model_fingerprint=payload["embedding_model_fingerprint"],
                        chunker_version=payload["chunker_version"],
                    )
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise VectorStoreError(
                    code="POLICY_VECTOR_PAYLOAD_INVALID", message="向量候选 payload 不可信"
                ) from exc
        return tuple(parsed)

    async def verify_generation(self, tenant_id: uuid.UUID, generation: int) -> int:
        try:
            result = await self._client.count(
                collection_name=self._collection_name,
                count_filter=_generation_filter(tenant_id, generation),
                exact=True,
            )
        except Exception as exc:
            raise VectorStoreError(
                code="POLICY_VECTOR_VERIFY_FAILED", message="向量代际校验失败"
            ) from exc
        return result.count


def _payload(
    tenant_id: uuid.UUID,
    generation: int,
    chunk: IndexChunk,
    embedding_model_fingerprint: str,
) -> dict[str, str | int]:
    return {
        "tenant_id": str(tenant_id),
        "family_id": str(chunk.family_id),
        "document_id": str(chunk.document_id),
        "clause_id": str(chunk.clause_id),
        "chunk_id": str(chunk.chunk_id),
        "effective_day": _epoch_day(chunk.effective_date),
        "expiry_day_exclusive": (
            _MAX_DAY if chunk.expiry_date is None else _epoch_day(chunk.expiry_date)
        ),
        "document_content_sha256": chunk.document_content_sha256,
        "clause_text_sha256": chunk.clause_text_sha256,
        "chunk_text_sha256": chunk.chunk_text_sha256,
        "index_generation": generation,
        "embedding_model_fingerprint": embedding_model_fingerprint,
        "chunker_version": chunk.chunker_version,
    }


def _generation_filter(
    tenant_id: uuid.UUID, generation: int, expense_day: int | None = None
) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=str(tenant_id))),
        models.FieldCondition(key="index_generation", match=models.MatchValue(value=generation)),
    ]
    if expense_day is not None:
        must.extend(
            [
                models.FieldCondition(key="effective_day", range=models.Range(lte=expense_day)),
                models.FieldCondition(
                    key="expiry_day_exclusive", range=models.Range(gt=expense_day)
                ),
            ]
        )
    return models.Filter(must=must)


def _epoch_day(value: date) -> int:
    return (value - _EPOCH).days
