"""本地 embedding/rerank provider；禁止制度文本发送到公网端点。"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ExpenseGuardError


class LocalModelError(ExpenseGuardError):
    """本地模型不可用或响应不符合契约。"""

    status_code = 503


class LocalModelProvider(Protocol):
    """embedding/rerank 必须由同一私有边界实现。"""

    @property
    def vector_size(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...

    async def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]: ...


class _EmbeddingItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: list[_EmbeddingItem]


class _RerankItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int = Field(ge=0)
    relevance_score: float


class _RerankResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_RerankItem]


class DeterministicLocalModels:
    """仅供 dev/test 的无网络确定性替身，不用于生产质量检索。"""

    def __init__(self, vector_size: int) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size 必须为正数")
        self._vector_size = vector_size

    @property
    def vector_size(self) -> int:
        return self._vector_size

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._embed_one(text) for text in texts)

    async def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        query_terms = _bigrams(query)
        return tuple(_jaccard(query_terms, _bigrams(document)) for document in documents)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        values: list[float] = []
        counter = 0
        while len(values) < self._vector_size:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            values.extend((byte - 127.5) / 127.5 for byte in digest)
            counter += 1
        vector = values[: self._vector_size]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return tuple(value / norm for value in vector)


class HttpLocalModels:
    """Infinity 的 OpenAI-compatible embedding 与 rerank HTTP 适配器。"""

    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: Sequence[str],
        embedding_model: str,
        rerank_model: str,
        vector_size: int,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        validate_private_endpoint(
            base_url,
            allowed_hosts=allowed_hosts,
            code="POLICY_MODEL_ENDPOINT_FORBIDDEN",
            message="本地模型端点不在显式内网白名单",
        )
        self._base_url = base_url.rstrip("/")
        self._embedding_model = embedding_model
        self._rerank_model = rerank_model
        self._vector_size = vector_size
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def vector_size(self) -> int:
        return self._vector_size

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._embedding_model, "input": list(texts)},
            )
            response.raise_for_status()
            parsed = _EmbeddingResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalModelError(
                code="POLICY_EMBEDDING_UNAVAILABLE", message="本地 embedding 服务不可用"
            ) from exc
        ordered = sorted(parsed.data, key=lambda item: item.index)
        if len(ordered) != len(texts) or any(
            len(item.embedding) != self._vector_size for item in ordered
        ):
            raise LocalModelError(
                code="POLICY_EMBEDDING_INVALID", message="本地 embedding 响应维度不正确"
            )
        return tuple(tuple(item.embedding) for item in ordered)

    async def rerank(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        if not documents:
            return ()
        try:
            response = await self._client.post(
                f"{self._base_url}/rerank",
                json={
                    "model": self._rerank_model,
                    "query": query,
                    "documents": list(documents),
                    "top_n": len(documents),
                    "return_documents": False,
                },
            )
            response.raise_for_status()
            parsed = _RerankResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise LocalModelError(
                code="POLICY_RERANK_UNAVAILABLE", message="本地 rerank 服务不可用"
            ) from exc
        scores: list[float | None] = [None] * len(documents)
        for item in parsed.results:
            if item.index >= len(documents) or scores[item.index] is not None:
                raise LocalModelError(
                    code="POLICY_RERANK_INVALID", message="本地 rerank 响应索引不正确"
                )
            scores[item.index] = item.relevance_score
        if any(score is None for score in scores):
            raise LocalModelError(code="POLICY_RERANK_INVALID", message="本地 rerank 响应不完整")
        return tuple(float(score) for score in scores if score is not None)


def _bigrams(value: str) -> frozenset[str]:
    compact = "".join(value.split()).casefold()
    if len(compact) < 2:
        return frozenset({compact}) if compact else frozenset()
    return frozenset(compact[index : index + 2] for index in range(len(compact) - 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def validate_private_endpoint(
    url: str,
    *,
    allowed_hosts: Sequence[str],
    code: str,
    message: str,
) -> None:
    """只允许显式列出的 loopback/内网主机，且拒绝 URL 内嵌凭据。"""
    parsed = urlparse(url)
    allowed = frozenset(host.lower() for host in allowed_hosts)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.hostname.lower() not in allowed
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LocalModelError(code=code, message=message)
