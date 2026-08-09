"""Strongly typed CP-F6.4 correlation-detection APIs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthDep, SessionFactoryDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.detection.config_service import create_detection_config, list_detection_configs
from app.core.detection.models import (
    CapabilityDetails,
    CapabilityStatus,
    CorrelationEvidence,
    DetectionProfileDefinition,
    DetectorKind,
)
from app.core.detection.query_service import (
    get_batch_detection,
    get_correlation_finding,
    get_detection_run,
    list_detection_findings,
)
from app.core.detection.run_service import run_detection
from app.core.detection.service_models import (
    BatchDetectionResult,
    CapabilityResult,
    DetectionConfigResult,
    DetectionRunResult,
    FindingDetail,
    FindingPage,
    FindingSummary,
    ParticipatingRowResult,
)
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.security.permissions import Permission

router = APIRouter(tags=["detection"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class _StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class DetectionConfigCreateRequest(_StrictApiModel):
    expected_current_version: int = Field(ge=0)
    definition: DetectionProfileDefinition
    change_reason: str = Field(min_length=1, max_length=500)


class DetectionConfigResponse(_StrictApiModel):
    id: uuid.UUID
    version: int
    definition: DetectionProfileDefinition
    config_fingerprint: str
    algorithm_bundle_version: str
    created_by: uuid.UUID
    created_at: datetime
    change_reason: str
    reused_existing: bool


class DetectionConfigHistoryResponse(_StrictApiModel):
    current: DetectionConfigResponse | None
    history: tuple[DetectionConfigResponse, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class DetectionRunResponse(_StrictApiModel):
    id: uuid.UUID
    file_version_id: uuid.UUID
    detection_config_id: uuid.UUID
    config_version: int
    config_fingerprint: str
    algorithm_bundle_version: str
    input_fingerprint: str
    run_fingerprint: str
    source_row_count: int = Field(ge=0)
    parsed_row_count: int = Field(ge=0)
    error_row_count: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    created_by: uuid.UUID
    created_at: datetime
    completed_at: datetime
    reused_existing: bool


class CapabilityResponse(_StrictApiModel):
    detector: DetectorKind
    detector_version: str
    status: CapabilityStatus
    reason_code: str
    reason: str
    details: CapabilityDetails
    finding_count: int = Field(ge=0)


class BatchDetectionResponse(_StrictApiModel):
    run: DetectionRunResponse | None
    capabilities: tuple[CapabilityResponse, ...]
    current_config_fingerprint: str | None
    config_stale: bool


class FindingSummaryResponse(_StrictApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    detector: DetectorKind
    detector_version: str
    finding_key: str
    reasoning: str
    participating_row_count: int = Field(ge=2)
    first_row_no: int = Field(ge=1)


class FindingPageResponse(_StrictApiModel):
    items: tuple[FindingSummaryResponse, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)


class ParticipatingRowResponse(_StrictApiModel):
    ordinal: int = Field(ge=1)
    row_no: int = Field(ge=1)
    raw: dict[str, Any]
    normalized: NormalizedExpenseRecord | None
    parse_error_code: str | None


class FindingDetailResponse(_StrictApiModel):
    id: uuid.UUID
    run_id: uuid.UUID
    file_version_id: uuid.UUID
    detector: DetectorKind
    detector_version: str
    finding_key: str
    evidence: CorrelationEvidence
    reasoning: str
    rows: tuple[ParticipatingRowResponse, ...]
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


def _config_response(result: DetectionConfigResult) -> DetectionConfigResponse:
    return DetectionConfigResponse.model_validate(result)


def _run_response(result: DetectionRunResult) -> DetectionRunResponse:
    return DetectionRunResponse.model_validate(result)


def _capability_response(result: CapabilityResult) -> CapabilityResponse:
    return CapabilityResponse.model_validate(result)


def _batch_response(result: BatchDetectionResult) -> BatchDetectionResponse:
    return BatchDetectionResponse(
        run=None if result.run is None else _run_response(result.run),
        capabilities=tuple(_capability_response(item) for item in result.capabilities),
        current_config_fingerprint=result.current_config_fingerprint,
        config_stale=result.config_stale,
    )


def _summary_response(result: FindingSummary) -> FindingSummaryResponse:
    return FindingSummaryResponse.model_validate(result)


def _page_response(result: FindingPage) -> FindingPageResponse:
    return FindingPageResponse(
        items=tuple(_summary_response(item) for item in result.items),
        total=result.total,
        limit=result.limit,
        offset=result.offset,
    )


def _row_response(result: ParticipatingRowResult) -> ParticipatingRowResponse:
    return ParticipatingRowResponse.model_validate(result)


def _detail_response(result: FindingDetail) -> FindingDetailResponse:
    return FindingDetailResponse(
        id=result.id,
        run_id=result.run_id,
        file_version_id=result.file_version_id,
        detector=result.detector,
        detector_version=result.detector_version,
        finding_key=result.finding_key,
        evidence=result.evidence,
        reasoning=result.reasoning,
        rows=tuple(_row_response(item) for item in result.rows),
        completed=result.completed,
        total=result.total,
    )


@router.get(
    "/api/detection/configs",
    response_model=DetectionConfigHistoryResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="list_configs",
)
async def list_configs_endpoint(
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DetectionConfigHistoryResponse:
    _set_private_no_store(response)
    page = await list_detection_configs(db, tenant_id=auth.tenant_id, limit=limit, offset=offset)
    current_page = await list_detection_configs(db, tenant_id=auth.tenant_id, limit=1, offset=0)
    return DetectionConfigHistoryResponse(
        current=_config_response(current_page.items[0]) if current_page.items else None,
        history=tuple(_config_response(item) for item in page.items),
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.put(
    "/api/detection/configs",
    response_model=DetectionConfigResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": DetectionConfigResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="put_config",
)
async def put_config_endpoint(
    payload: DetectionConfigCreateRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> DetectionConfigResponse:
    _set_private_no_store(response)
    result = await create_detection_config(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        expected_current_version=payload.expected_current_version,
        definition=payload.definition,
        change_reason=payload.change_reason,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return _config_response(result)


@router.post(
    "/api/batches/{file_version_id}/detect",
    response_model=DetectionRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": DetectionRunResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
    name="run_batch",
)
async def run_batch_endpoint(
    file_version_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> DetectionRunResponse:
    _set_private_no_store(response)
    result = await run_detection(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        file_version_id=file_version_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return _run_response(result)


@router.get(
    "/api/batches/{file_version_id}/detection",
    response_model=BatchDetectionResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_batch",
)
async def get_batch_endpoint(
    file_version_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> BatchDetectionResponse:
    _set_private_no_store(response)
    result = await get_batch_detection(
        db, tenant_id=auth.tenant_id, file_version_id=file_version_id
    )
    return _batch_response(result)


@router.get(
    "/api/detection-runs/{run_id}",
    response_model=DetectionRunResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_run",
)
async def get_run_endpoint(
    run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> DetectionRunResponse:
    _set_private_no_store(response)
    return _run_response(await get_detection_run(db, tenant_id=auth.tenant_id, run_id=run_id))


@router.get(
    "/api/detection-runs/{run_id}/findings",
    response_model=FindingPageResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="list_findings",
)
async def list_findings_endpoint(
    run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    detector: DetectorKind | None = None,
    capability_status: CapabilityStatus | None = None,
    sort_by: Literal["default"] = "default",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingPageResponse:
    _set_private_no_store(response)
    return _page_response(
        await list_detection_findings(
            db,
            tenant_id=auth.tenant_id,
            run_id=run_id,
            detector=detector,
            capability_status=capability_status,
            sort_by=sort_by,
            limit=limit,
            offset=offset,
        )
    )


@router.get(
    "/api/correlation-findings/{finding_id}",
    response_model=FindingDetailResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_finding",
)
async def get_finding_endpoint(
    finding_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    row_limit: Annotated[int, Query(ge=1, le=200)] = 50,
    row_offset: Annotated[int, Query(ge=0)] = 0,
) -> FindingDetailResponse:
    _set_private_no_store(response)
    return _detail_response(
        await get_correlation_finding(
            db,
            tenant_id=auth.tenant_id,
            finding_id=finding_id,
            row_limit=row_limit,
            row_offset=row_offset,
        )
    )


def _set_private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
