"""向量索引和候选结果的强类型内部契约。"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class IndexChunk(BaseModel):
    """写入向量库的 PG 已校验 chunk。"""

    model_config = ConfigDict(frozen=True)

    family_id: uuid.UUID
    document_id: uuid.UUID
    clause_id: uuid.UUID
    chunk_id: uuid.UUID
    text: str = Field(min_length=1)
    effective_date: date
    expiry_date: date | None
    document_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clause_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunker_version: str = Field(min_length=1)


class SearchCandidate(BaseModel):
    """Qdrant 返回的未信任候选身份与分数。"""

    model_config = ConfigDict(frozen=True)

    point_id: uuid.UUID
    family_id: uuid.UUID
    document_id: uuid.UUID
    clause_id: uuid.UUID
    chunk_id: uuid.UUID
    vector_score: float
    effective_day: int
    expiry_day_exclusive: int
    document_content_sha256: str
    clause_text_sha256: str
    chunk_text_sha256: str
    index_generation: int
    embedding_model_fingerprint: str
    chunker_version: str
