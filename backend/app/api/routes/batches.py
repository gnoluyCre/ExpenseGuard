"""批次导入与文件版本管理路由。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel, ConfigDict

from app.api.deps import AuthDep, SettingsDep, TenantDbDep, require_permission
from app.core.batches.importer import (
    BatchDetail,
    BatchImportResult,
    BatchSummary,
    get_batch_detail,
    import_batch,
    list_batches,
)
from app.core.security.permissions import Permission

router = APIRouter(prefix="/api/batches", tags=["batches"])


class BatchSummaryResponse(BaseModel):
    """批次列表项。"""

    model_config = ConfigDict(from_attributes=True)

    file_version_id: uuid.UUID
    filename: str
    content_hash: str
    row_count: int
    uploaded_at: datetime
    uploaded_by: uuid.UUID


class BatchImportResponse(BatchSummaryResponse):
    """批次导入响应。"""

    reused_existing: bool
    stored_rows: int


class ExpenseRowResponse(BaseModel):
    """原始报销行摘要。"""

    row_no: int
    raw_json: dict[str, Any]
    parse_error: str | None


class BatchDetailResponse(BatchSummaryResponse):
    """批次详情响应。"""

    rows: list[ExpenseRowResponse]


def _summary_response(summary: BatchSummary) -> BatchSummaryResponse:
    return BatchSummaryResponse.model_validate(summary)


def _import_response(result: BatchImportResult) -> BatchImportResponse:
    return BatchImportResponse(
        **_summary_response(result.summary).model_dump(),
        reused_existing=result.reused_existing,
        stored_rows=result.stored_rows,
    )


def _detail_response(detail: BatchDetail) -> BatchDetailResponse:
    return BatchDetailResponse(
        **_summary_response(detail.summary).model_dump(),
        rows=[
            ExpenseRowResponse(
                row_no=row.row_no,
                raw_json=row.raw_json,
                parse_error=row.parse_error,
            )
            for row in detail.rows
        ],
    )


@router.post(
    "",
    response_model=BatchImportResponse,
    name="import",
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
)
async def import_batch_endpoint(
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> BatchImportResponse:
    """上传 Excel 并创建或复用文件版本。"""
    content = await file.read()
    result = await import_batch(
        db,
        tenant_id=auth.tenant_id,
        uploaded_by=auth.user_id,
        filename=file.filename or "",
        content=content,
        upload_root=settings.upload_root,
    )
    return _import_response(result)


@router.get(
    "",
    response_model=list[BatchSummaryResponse],
    name="list",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def list_batches_endpoint(db: TenantDbDep) -> list[BatchSummaryResponse]:
    """列出当前租户批次。"""
    return [_summary_response(summary) for summary in await list_batches(db)]


@router.get(
    "/{file_version_id}",
    response_model=BatchDetailResponse,
    name="detail",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def batch_detail_endpoint(file_version_id: uuid.UUID, db: TenantDbDep) -> BatchDetailResponse:
    """读取批次详情。"""
    return _detail_response(await get_batch_detail(db, file_version_id))
