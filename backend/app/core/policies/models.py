"""制度解析边界使用的强类型不可变模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyLimits(BaseModel):
    """制度文件、文本、条款和 chunk 的确定性上限。"""

    model_config = ConfigDict(frozen=True)

    max_file_bytes: int = Field(gt=0)
    max_pdf_pages: int = Field(gt=0)
    max_extracted_chars: int = Field(gt=0)
    max_clauses: int = Field(gt=0)
    chunk_chars: int = Field(ge=128, le=16_000)


class SourceLocator(BaseModel):
    """可恢复到抽取文本位置的来源定位。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "pdf", "docx"]
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    page_start: int | None = Field(default=None, gt=0)
    page_end: int | None = Field(default=None, gt=0)
    paragraph_start: int | None = Field(default=None, ge=0)
    paragraph_end: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> SourceLocator:
        if self.end <= self.start:
            raise ValueError("source locator end 必须大于 start")
        return self


class ParsedClause(BaseModel):
    """从原文逐字切出的 citation atom。"""

    model_config = ConfigDict(frozen=True)

    clause_no: str = Field(min_length=1, max_length=64)
    hierarchy_path: str | None = Field(default=None, max_length=1024)
    ordinal: int = Field(gt=0)
    text: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_start: int = Field(ge=0)
    source_end: int = Field(gt=0)
    source_locator: SourceLocator

    @model_validator(mode="after")
    def _offsets_match(self) -> ParsedClause:
        if self.source_end <= self.source_start:
            raise ValueError("clause end 必须大于 start")
        if not self.text.strip():
            raise ValueError("clause 不得为空或纯空白")
        return self


class PolicyChunkDraft(BaseModel):
    """同一 clause 内的连续、未改写检索切片。"""

    model_config = ConfigDict(frozen=True)

    clause_ordinal: int = Field(gt=0)
    chunk_no: int = Field(gt=0)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    text: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _valid_slice(self) -> PolicyChunkDraft:
        if self.end_offset <= self.start_offset:
            raise ValueError("chunk end 必须大于 start")
        return self


class ParsedPolicyDocument(BaseModel):
    """解析和切分完成、可预览的制度快照。"""

    model_config = ConfigDict(frozen=True)

    mime_type: str
    parser_version: str
    chunker_version: str
    extracted_text: str
    extracted_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clauses: tuple[ParsedClause, ...]
    chunks: tuple[PolicyChunkDraft, ...]
    parse_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
