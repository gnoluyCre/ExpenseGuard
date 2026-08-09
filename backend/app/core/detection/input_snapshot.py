"""Deterministic F2-to-F6 input snapshot loading and identity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.detection.canonical import canonical_sha256
from app.core.detection.errors import DetectionError, DetectionInternalError, DetectionNotFoundError
from app.core.detection.models import (
    AvailabilityStatus,
    DetectionBatch,
    FieldAvailabilitySnapshot,
    ParsedSourceRow,
    ParseErrorSourceRow,
)
from app.core.parsing.models import UNIFIED_FIELDS, NormalizedExpenseRecord
from app.db.models.batch import ExpenseRow, FieldAvailability, FileVersion, ParseStatus


@dataclass(frozen=True)
class DetectionInputSnapshot:
    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    batch: DetectionBatch
    input_fingerprint: str
    source_row_count: int
    parsed_row_count: int
    error_row_count: int


async def load_detection_input(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    file_version: FileVersion | None = None,
) -> DetectionInputSnapshot:
    """Load and validate the complete immutable detector input in stable order."""
    batch_row = file_version or await db.scalar(
        select(FileVersion).where(
            FileVersion.id == file_version_id,
            FileVersion.tenant_id == tenant_id,
        )
    )
    if batch_row is None:
        raise DetectionNotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    if batch_row.parse_status not in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS}:
        raise DetectionError(code="DETECTION_INPUT_NOT_READY", message="批次尚未完成结构化解析")
    if batch_row.mapping_version_id is None:
        raise DetectionInternalError(code="DETECTION_INPUT_INVALID", message="批次缺少映射版本快照")
    rows = tuple(
        (
            await db.execute(
                select(
                    ExpenseRow.row_no,
                    ExpenseRow.normalized_json,
                    ExpenseRow.parse_error_code,
                )
                .where(
                    ExpenseRow.tenant_id == tenant_id,
                    ExpenseRow.file_version_id == file_version_id,
                )
                .order_by(ExpenseRow.row_no)
            )
        ).all()
    )
    if not rows or batch_row.row_count != len(rows):
        raise DetectionInternalError(code="DETECTION_INPUT_INVALID", message="批次行数快照不一致")
    source_rows: list[ParsedSourceRow | ParseErrorSourceRow] = []
    parsed_count = 0
    for row_no, normalized_json, parse_error_code in rows:
        if normalized_json is not None:
            if parse_error_code is not None:
                raise DetectionInternalError(
                    code="DETECTION_INPUT_INVALID", message="批次行同时包含结果与解析错误"
                )
            try:
                normalized = NormalizedExpenseRecord.model_validate(normalized_json)
            except ValidationError as exc:
                raise DetectionInternalError(
                    code="DETECTION_INPUT_INVALID", message="批次规范化行快照无效"
                ) from exc
            if normalized.mapping_version_id != batch_row.mapping_version_id:
                raise DetectionInternalError(
                    code="DETECTION_INPUT_INVALID", message="批次行映射版本不一致"
                )
            source_rows.append(ParsedSourceRow(row_no=row_no, normalized=normalized))
            parsed_count += 1
        else:
            if parse_error_code is None:
                raise DetectionInternalError(
                    code="DETECTION_INPUT_INVALID", message="批次行既无结果也无解析错误"
                )
            try:
                source_rows.append(ParseErrorSourceRow(row_no=row_no, error_code=parse_error_code))
            except ValidationError as exc:
                raise DetectionInternalError(
                    code="DETECTION_INPUT_INVALID", message="批次解析错误身份无效"
                ) from exc
    availability_rows = tuple(
        (
            await db.execute(
                select(FieldAvailability.field_name, FieldAvailability.status)
                .where(
                    FieldAvailability.tenant_id == tenant_id,
                    FieldAvailability.file_version_id == file_version_id,
                )
                .order_by(FieldAvailability.field_name)
            )
        ).all()
    )
    by_field = {field_name: status for field_name, status in availability_rows}
    if len(by_field) != len(availability_rows) or set(by_field) != {
        field.value for field in UNIFIED_FIELDS
    }:
        raise DetectionInternalError(code="DETECTION_INPUT_INVALID", message="字段可用性快照不完整")
    availability = tuple(
        FieldAvailabilitySnapshot(
            field_name=field,
            status=AvailabilityStatus(by_field[field.value].value),
        )
        for field in UNIFIED_FIELDS
    )
    detection_batch = DetectionBatch(rows=tuple(source_rows), field_availability=availability)
    fingerprint = canonical_sha256(
        {
            "batch": detection_batch.model_dump(mode="json"),
            "content_hash": batch_row.content_hash,
            "file_version_id": str(batch_row.id),
            "mapping_version_id": str(batch_row.mapping_version_id),
            "revision_no": batch_row.revision_no,
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )
    return DetectionInputSnapshot(
        file_version_id=batch_row.id,
        mapping_version_id=batch_row.mapping_version_id,
        batch=detection_batch,
        input_fingerprint=fingerprint,
        source_row_count=len(rows),
        parsed_row_count=parsed_count,
        error_row_count=len(rows) - parsed_count,
    )
