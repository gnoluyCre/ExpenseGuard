"""本地制度向量检索抽象与实现。"""

from app.core.retrieval.factory import build_local_retrieval
from app.core.retrieval.models import IndexChunk, SearchCandidate
from app.core.retrieval.providers import (
    DeterministicLocalModels,
    HttpLocalModels,
    LocalModelProvider,
)
from app.core.retrieval.vector_store import QdrantVectorStore, VectorStore

__all__ = [
    "DeterministicLocalModels",
    "HttpLocalModels",
    "IndexChunk",
    "LocalModelProvider",
    "QdrantVectorStore",
    "SearchCandidate",
    "VectorStore",
    "build_local_retrieval",
]
