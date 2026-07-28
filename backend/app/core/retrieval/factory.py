"""从强类型 Settings 与 frozen generation 构造本地检索边界。"""

from __future__ import annotations

from qdrant_client import AsyncQdrantClient

from app.core.retrieval.providers import (
    DeterministicLocalModels,
    HttpLocalModels,
    LocalModelProvider,
    validate_private_endpoint,
)
from app.core.retrieval.vector_store import QdrantVectorStore
from app.db.models.policy import PolicyIndexGeneration
from app.settings import Settings


def build_local_retrieval(
    settings: Settings,
    generation: PolicyIndexGeneration,
) -> tuple[QdrantVectorStore, LocalModelProvider]:
    """构造检索对象；任何外部或未列入白名单的端点 fail closed。"""
    validate_private_endpoint(
        settings.qdrant_url,
        allowed_hosts=settings.qdrant_allowed_hosts,
        code="POLICY_VECTOR_ENDPOINT_FORBIDDEN",
        message="Qdrant 端点不在显式内网白名单",
    )
    if settings.policy_embedding_provider == "http":
        provider: LocalModelProvider = HttpLocalModels(
            base_url=settings.policy_local_model_url,
            allowed_hosts=settings.policy_local_model_allowed_hosts,
            embedding_model=generation.embedding_model_id,
            rerank_model=generation.rerank_model_id,
            vector_size=generation.vector_size,
        )
    else:
        provider = DeterministicLocalModels(vector_size=generation.vector_size)
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        timeout=30,
        cloud_inference=False,
    )
    return (
        QdrantVectorStore(
            collection_name=generation.collection_name,
            embedding_model_fingerprint=generation.embedding_model_fingerprint,
            chunker_version=generation.chunker_version,
            models_provider=provider,
            client=client,
        ),
        provider,
    )
