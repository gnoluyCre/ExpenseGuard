"""Strongly typed F8 two-dimensional grading transport adapters."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import Field

from app.api.deps import AuthDep, SessionFactoryDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.detection.models import DetectorKind
from app.core.grading.config_service import (
    create_grading_config,
    get_current_grading_config,
    list_grading_configs,
)
from app.core.grading.errors import GradingNotFoundError
from app.core.grading.models import (
    Disposition,
    GradingConfigV1,
    InvestigationGradingOutcome,
    SourceKind,
    StrictGradingModel,
)
from app.core.grading.query_service import (
    get_batch_grading,
    get_grading_item,
    get_grading_run,
    list_grading_item_rows,
    list_grading_items,
)
from app.core.grading.run_service import run_grading
from app.core.grading.service_models import (
    BatchGradingView,
    F7ManifestRequest,
    GradingConfigPage,
    GradingConfigView,
    GradingItemPage,
    GradingItemView,
    GradingRowPage,
    GradingRunView,
)
from app.core.rules.models import RuleKind
from app.core.security.permissions import Permission

router = APIRouter(tags=["grading"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


class GradingConfigCreateRequest(StrictGradingModel):
    expected_current_version: Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
    definition: GradingConfigV1
    change_reason: str = Field(min_length=1, max_length=500)


class GradingRunCreateRequest(StrictGradingModel):
    validation_run_id: uuid.UUID
    detection_run_id: uuid.UUID
    grading_config_id: uuid.UUID
    f7_manifest: tuple[F7ManifestRequest, ...] = Field(max_length=20_000)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


@router.get(
    "/api/v1/grading-configs/current",
    response_model=GradingConfigView,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="get_current_config",
)
async def get_current_config_endpoint(
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> GradingConfigView:
    _no_store(response)
    result = await get_current_grading_config(db, tenant_id=auth.tenant_id)
    if result is None:
        raise GradingNotFoundError(
            code="GRADING_CONFIG_NOT_FOUND",
            message="二维分级配置不存在",
        )
    return result


@router.get(
    "/api/v1/grading-configs",
    response_model=GradingConfigPage,
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
) -> GradingConfigPage:
    _no_store(response)
    return await list_grading_configs(
        db,
        tenant_id=auth.tenant_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/v1/grading-configs",
    response_model=GradingConfigView,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": GradingConfigView}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="create_config",
)
async def create_config_endpoint(
    payload: GradingConfigCreateRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> GradingConfigView:
    _no_store(response)
    result = await create_grading_config(
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
    return result


@router.post(
    "/api/v1/files/{file_version_id}/grading-runs",
    response_model=GradingRunView,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": GradingRunView}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
    name="create_run",
)
async def create_run_endpoint(
    file_version_id: uuid.UUID,
    payload: GradingRunCreateRequest,
    response: Response,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> GradingRunView:
    _no_store(response)
    result = await run_grading(
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        file_version_id=file_version_id,
        validation_run_id=payload.validation_run_id,
        detection_run_id=payload.detection_run_id,
        grading_config_id=payload.grading_config_id,
        f7_requests=payload.f7_manifest,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return result


@router.get(
    "/api/v1/files/{file_version_id}/grading-runs/current",
    response_model=BatchGradingView,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_current_run",
)
async def get_current_run_endpoint(
    file_version_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> BatchGradingView:
    _no_store(response)
    return await get_batch_grading(db, tenant_id=auth.tenant_id, file_version_id=file_version_id)


@router.get(
    "/api/v1/grading-runs/{grading_run_id}",
    response_model=GradingRunView,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_run",
)
async def get_run_endpoint(
    grading_run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> GradingRunView:
    _no_store(response)
    return await get_grading_run(
        db,
        tenant_id=auth.tenant_id,
        grading_run_id=grading_run_id,
    )


@router.get(
    "/api/v1/grading-runs/{grading_run_id}/items",
    response_model=GradingItemPage,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="list_items",
)
async def list_items_endpoint(
    grading_run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    source_kind: SourceKind | None = None,
    rule_kind: RuleKind | None = None,
    detector: DetectorKind | None = None,
    severity_impact: Annotated[int | None, Query(ge=0, le=3)] = None,
    severity_confidence: Annotated[int | None, Query(ge=0, le=3)] = None,
    disposition: Disposition | None = None,
    f7_outcome: InvestigationGradingOutcome | None = None,
    sort_by: Literal["default"] = "default",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GradingItemPage:
    _no_store(response)
    return await list_grading_items(
        db,
        tenant_id=auth.tenant_id,
        grading_run_id=grading_run_id,
        source_kind=source_kind,
        rule_kind=rule_kind,
        detector=detector,
        severity_impact=severity_impact,
        severity_confidence=severity_confidence,
        disposition=disposition,
        f7_outcome=f7_outcome,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/v1/grading-items/{grading_item_id}",
    response_model=GradingItemView,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="get_item",
)
async def get_item_endpoint(
    grading_item_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> GradingItemView:
    _no_store(response)
    return await get_grading_item(
        db,
        tenant_id=auth.tenant_id,
        grading_item_id=grading_item_id,
    )


@router.get(
    "/api/v1/grading-items/{grading_item_id}/rows",
    response_model=GradingRowPage,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="list_item_rows",
)
async def list_item_rows_endpoint(
    grading_item_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GradingRowPage:
    _no_store(response)
    return await list_grading_item_rows(
        db,
        tenant_id=auth.tenant_id,
        grading_item_id=grading_item_id,
        limit=limit,
        offset=offset,
    )
