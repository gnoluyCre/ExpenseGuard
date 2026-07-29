"""批次导入与文件版本管理路由。"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Header, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict

from app.api.deps import (
    AuthDep,
    SessionFactoryDep,
    SettingsDep,
    TenantDbDep,
    require_permission,
)
from app.api.errors import ErrorResponse
from app.core.batches.importer import (
    BatchDetail,
    BatchImportResult,
    BatchSummary,
    get_batch_detail,
    import_batch,
    list_batches,
)
from app.core.batches.revisions import create_file_revision
from app.core.errors import ExpenseGuardError
from app.core.parsing.api_service import (
    get_field_availability,
    list_parse_errors,
    record_parse_failure,
    record_parse_success,
)
from app.core.parsing.models import AvailabilityEvidence, BatchParseResult, RowErrorDetail
from app.core.parsing.service import BatchParseInternalError, parse_batch
from app.core.rules import RowVerdict, RuleKind, RuleOutcome
from app.core.rules.models import ReasonCode, RuleEvidence
from app.core.security.permissions import Permission
from app.core.validation.batch_service import ValidationSummary, validate_batch
from app.core.validation.query_service import get_validation_summary, list_findings

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


class ParseBatchRequest(BaseModel):
    """触发解析所需的不可变映射版本。"""

    model_config = ConfigDict(extra="forbid")

    mapping_version_id: uuid.UUID


class ParseBatchResponse(BaseModel):
    """解析计数与当前版本状态。"""

    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    mapping_version: int
    status: Literal["parsed", "parsed_with_errors"]
    total_rows: int
    success_count: int
    error_count: int
    parsed_at: str
    reused_existing: bool


class ParseErrorItemResponse(BaseModel):
    """解析失败行；raw_json 仅返回给同租户 BATCH_READ 用户。"""

    row_no: int
    raw_json: dict[str, Any]
    parse_error_code: str
    parse_error: str
    parse_error_detail: RowErrorDetail


class ParseErrorsResponse(BaseModel):
    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    total: int
    offset: int
    limit: int
    items: list[ParseErrorItemResponse]


class FieldAvailabilityItemResponse(BaseModel):
    field_name: str
    status: Literal["available", "inferred", "missing"]
    evidence: AvailabilityEvidence


class FieldAvailabilityResponse(BaseModel):
    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    items: list[FieldAvailabilityItemResponse]


class ValidationSummaryResponse(BaseModel):
    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    ruleset_fingerprint: str
    total_row_count: int
    evaluated_row_count: int
    passed_count: int
    flagged_count: int
    manual_review_count: int
    parse_failed_count: int
    reused_existing: bool


class FindingItemResponse(BaseModel):
    id: uuid.UUID
    row_no: int
    rule_id: str
    rule_version: str | None
    rule_kind: RuleKind
    outcome: RuleOutcome
    reason_code: ReasonCode
    reasoning: str
    evidence: RuleEvidence
    verdict: RowVerdict


class FindingsResponse(BaseModel):
    file_version_id: uuid.UUID
    total: int
    page: int
    page_size: int
    items: list[FindingItemResponse]


class RevisionRequestReason(StrEnum):
    """服务层接受的派生原因。"""

    RULESET_CHANGE = "ruleset_change"
    MAPPING_CHANGE = "mapping_change"
    POLICY_CHANGE = "policy_change"


class CreateRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: RevisionRequestReason


class CreateRevisionResponse(BaseModel):
    file_version_id: uuid.UUID
    source_file_version_id: uuid.UUID
    root_file_version_id: uuid.UUID
    revision_no: int
    reason: RevisionRequestReason
    parse_status: Literal["unparsed", "parsed", "parsed_with_errors"]
    mapping_version_id: uuid.UUID | None
    reused_existing: bool


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


def _validation_response(summary: ValidationSummary) -> ValidationSummaryResponse:
    return ValidationSummaryResponse(**summary.__dict__)


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


@router.post(
    "/{file_version_id}/parse",
    response_model=ParseBatchResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    name="parse",
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
)
async def parse_batch_endpoint(
    file_version_id: uuid.UUID,
    payload: ParseBatchRequest,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
) -> ParseBatchResponse:
    """按指定不可变映射版本原子解析批次。"""
    try:
        result = await parse_batch(
            db,
            file_version_id=file_version_id,
            mapping_version_id=payload.mapping_version_id,
        )
        await record_parse_success(
            db,
            tenant_id=auth.tenant_id,
            actor_id=auth.user_id,
            result=result,
        )
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await db.rollback()
        await record_parse_failure(
            session_factory,
            tenant_id=auth.tenant_id,
            actor_id=auth.user_id,
            file_version_id=file_version_id,
            mapping_version_id=payload.mapping_version_id,
        )
        raise BatchParseInternalError(
            code="BATCH_PARSE_INTERNAL_ERROR",
            message="批次解析遇到内部错误，本次写入已回滚",
        ) from exc
    return _parse_response(result)


@router.get(
    "/{file_version_id}/parse-errors",
    response_model=ParseErrorsResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    name="parse_errors",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def parse_errors_endpoint(
    file_version_id: uuid.UUID,
    db: TenantDbDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ParseErrorsResponse:
    """分页读取解析失败行，保留原始证据链。"""
    page = await list_parse_errors(
        db,
        file_version_id=file_version_id,
        offset=offset,
        limit=limit,
    )
    return ParseErrorsResponse(
        file_version_id=page.file_version_id,
        mapping_version_id=page.mapping_version_id,
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        items=[
            ParseErrorItemResponse(
                row_no=item.row_no,
                raw_json=item.raw_json,
                parse_error_code=item.parse_error_code,
                parse_error=item.parse_error,
                parse_error_detail=item.parse_error_detail,
            )
            for item in page.items
        ],
    )


@router.get(
    "/{file_version_id}/field-availability",
    response_model=FieldAvailabilityResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    name="field_availability",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def field_availability_endpoint(
    file_version_id: uuid.UUID,
    db: TenantDbDep,
) -> FieldAvailabilityResponse:
    """返回当前解析版本的全部统一字段可用性。"""
    result = await get_field_availability(db, file_version_id=file_version_id)
    return FieldAvailabilityResponse(
        file_version_id=result.file_version_id,
        mapping_version_id=result.mapping_version_id,
        items=[
            FieldAvailabilityItemResponse(
                field_name=item.field_name,
                status=item.status,
                evidence=item.evidence,
            )
            for item in result.items
        ],
    )


@router.post(
    "/{file_version_id}/validate",
    response_model=ValidationSummaryResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 500)},
    name="validate",
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
)
async def validate_batch_endpoint(
    file_version_id: uuid.UUID,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
) -> ValidationSummaryResponse:
    result = await validate_batch(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        file_version_id=file_version_id,
    )
    return _validation_response(result)


@router.get(
    "/{file_version_id}/validation",
    response_model=ValidationSummaryResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404)},
    name="validation",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def validation_endpoint(
    file_version_id: uuid.UUID, db: TenantDbDep
) -> ValidationSummaryResponse:
    run = await get_validation_summary(db, file_version_id)
    return ValidationSummaryResponse(
        file_version_id=run.file_version_id,
        mapping_version_id=run.mapping_version_id,
        ruleset_fingerprint=run.ruleset_fingerprint,
        total_row_count=run.total_row_count,
        evaluated_row_count=run.evaluated_row_count,
        passed_count=run.passed_count,
        flagged_count=run.flagged_count,
        manual_review_count=run.manual_review_count,
        parse_failed_count=run.parse_failed_count,
        reused_existing=True,
    )


@router.get(
    "/{file_version_id}/findings",
    response_model=FindingsResponse,
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 422)},
    name="findings",
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
)
async def findings_endpoint(
    file_version_id: uuid.UUID,
    db: TenantDbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    verdict: Literal["flagged", "manual_review"] | None = None,
) -> FindingsResponse:
    result = await list_findings(
        db,
        file_version_id=file_version_id,
        page=page,
        page_size=page_size,
        verdict=verdict,
    )
    return FindingsResponse(
        file_version_id=result.file_version_id,
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        items=[FindingItemResponse(**item.__dict__) for item in result.items],
    )


@router.post(
    "/{file_version_id}/revisions",
    response_model=CreateRevisionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": CreateRevisionResponse},
        **{code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)},
    },
    name="create_revision",
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
)
async def create_revision_endpoint(
    file_version_id: uuid.UUID,
    payload: CreateRevisionRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateRevisionResponse:
    result = await create_file_revision(
        db,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        source_file_version_id=file_version_id,
        reason=payload.reason,
        idempotency_key=idempotency_key or "",
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return CreateRevisionResponse.model_validate(result.model_dump())


def _parse_response(result: BatchParseResult) -> ParseBatchResponse:
    return ParseBatchResponse.model_validate(result.model_dump())
