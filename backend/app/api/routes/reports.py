"""Immutable report generation and read APIs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel

from app.api.deps import AuthDep, SessionFactoryDep, SettingsDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.reports.export_service import (
    ReportExportSummary,
    create_report_export,
    download_report_export,
)
from app.core.reports.models import ReportSummary
from app.core.reports.query_service import (
    ParseErrorPage,
    ReportItemPage,
    ReportItemSort,
    SortDirection,
    list_report_items,
    list_report_parse_errors,
    load_report_for_file,
)
from app.core.reports.service import REPORT_TEMPLATE_VERSION, generate_report
from app.core.security.permissions import Permission
from app.db.models.reports import ReportAttentionGroup, ReportCitationStatus

router = APIRouter(tags=["reports"])


class ReportOverviewResponse(BaseModel):
    summary: ReportSummary
    policy_manifest: dict[str, object]
    binding_manifest: dict[str, object]


class ReportExportResponse(BaseModel):
    export_id: uuid.UUID
    report_run_id: uuid.UUID
    format: str
    template_version: str
    artifact_sha256: str
    size_bytes: int
    completed_at: datetime
    reused_existing: bool


_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "/api/batches/{file_version_id}/reports",
    response_model=ReportSummary,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": ReportSummary},
        **_READ_ERRORS,
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
    name="generate",
)
async def generate_report_endpoint(
    file_version_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> ReportSummary:
    result = await generate_report(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        file_version_id=file_version_id,
        idempotency_key=idempotency_key,
        template_version=REPORT_TEMPLATE_VERSION,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return result


@router.get(
    "/api/batches/{file_version_id}/report",
    response_model=ReportOverviewResponse,
    responses=_READ_ERRORS,
    dependencies=[Depends(require_permission(Permission.REPORT_READ))],
    name="get_for_batch",
)
async def get_report_for_batch_endpoint(
    file_version_id: uuid.UUID, db: TenantDbDep
) -> ReportOverviewResponse:
    snapshot = await load_report_for_file(db, file_version_id=file_version_id)
    return ReportOverviewResponse(
        summary=snapshot.summary,
        policy_manifest=snapshot.policy_manifest,
        binding_manifest=snapshot.binding_manifest,
    )


@router.get(
    "/api/reports/{report_id}/items",
    response_model=ReportItemPage,
    responses=_READ_ERRORS,
    dependencies=[Depends(require_permission(Permission.REPORT_READ))],
    name="list_items",
)
async def list_report_items_endpoint(
    report_id: uuid.UUID,
    db: TenantDbDep,
    attention_group: ReportAttentionGroup | None = None,
    citation_status: ReportCitationStatus | None = None,
    sort_by: ReportItemSort = ReportItemSort.DEFAULT,
    direction: SortDirection = SortDirection.ASC,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportItemPage:
    return await list_report_items(
        db,
        report_run_id=report_id,
        attention_group=attention_group,
        citation_status=citation_status,
        sort_by=sort_by,
        direction=direction,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/reports/{report_id}/parse-errors",
    response_model=ParseErrorPage,
    responses=_READ_ERRORS,
    dependencies=[Depends(require_permission(Permission.REPORT_READ))],
    name="list_parse_errors",
)
async def list_report_parse_errors_endpoint(
    report_id: uuid.UUID,
    db: TenantDbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ParseErrorPage:
    return await list_report_parse_errors(
        db,
        report_run_id=report_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/reports/{report_id}/exports",
    response_model=ReportExportResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": ReportExportResponse},
        **_READ_ERRORS,
        409: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_permission(Permission.REPORT_EXPORT))],
    name="create_export",
)
async def create_report_export_endpoint(
    report_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> ReportExportResponse:
    result = await create_report_export(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        report_run_id=report_id,
        idempotency_key=idempotency_key,
        export_root=settings.report_export_root,
        upload_root=settings.upload_root,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return _export_response(result)


@router.get(
    "/api/report-exports/{export_id}/download",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
            "description": "Completed XLSX artifact",
        },
        **_READ_ERRORS,
        409: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_permission(Permission.REPORT_EXPORT))],
    name="download_export",
)
async def download_report_export_endpoint(
    export_id: uuid.UUID,
    db: TenantDbDep,
    auth: AuthDep,
    settings: SettingsDep,
) -> Response:
    artifact = await download_report_export(
        db,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        export_id=export_id,
        export_root=settings.report_export_root,
    )
    return Response(
        content=artifact.content,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.filename)}",
            "X-Artifact-SHA256": artifact.artifact_sha256,
        },
    )


def _export_response(result: ReportExportSummary) -> ReportExportResponse:
    return ReportExportResponse(
        export_id=result.export_id,
        report_run_id=result.report_run_id,
        format=result.format,
        template_version=result.template_version,
        artifact_sha256=result.artifact_sha256,
        size_bytes=result.size_bytes,
        completed_at=result.completed_at,
        reused_existing=result.reused_existing,
    )
