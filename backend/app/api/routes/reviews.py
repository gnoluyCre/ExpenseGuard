"""Strongly typed F5 human-review and clearance-sampling APIs."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthDep, SessionFactoryDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.reviews.config_service import create_sampling_config, list_sampling_configs
from app.core.reviews.decision_service import submit_finding_review, submit_sampling_review
from app.core.reviews.errors import ReviewInputError
from app.core.reviews.models import (
    ClearanceReviewDetail,
    FindingReviewDetail,
    FindingReviewResult,
    ReviewQueueKind,
    ReviewQueuePage,
    ReviewQueueStatus,
    ReviewSummary,
    SamplingConfigResult,
    SamplingPlanResult,
    SamplingReviewResult,
)
from app.core.reviews.plan_service import (
    create_legacy_sampling_plan,
    get_sampling_plan,
)
from app.core.reviews.query_service import (
    get_finding_review_detail,
    get_review_summary,
    get_sampling_review_detail,
    list_review_queue,
)
from app.core.security.permissions import Permission
from app.db.models.findings import ReviewDecision, SamplingReviewDecision

router = APIRouter(tags=["reviews"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
}


class _StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SamplingConfigCreateRequest(_StrictApiModel):
    expected_current_version: int = Field(ge=0)
    rate_bps: int = Field(ge=1, le=10_000)
    min_sample_size: int = Field(ge=1)
    max_sample_size: int = Field(ge=1)
    change_reason: str = Field(min_length=1, max_length=500)


class SamplingConfigHistoryResponse(_StrictApiModel):
    current: SamplingConfigResult | None
    history: tuple[SamplingConfigResult, ...]


class CompletedSamplingPlanResponse(_StrictApiModel):
    status: Literal["completed"] = "completed"
    plan: SamplingPlanResult


class LegacySamplingPlanResponse(_StrictApiModel):
    status: Literal["legacy_not_initialized"] = "legacy_not_initialized"
    report_run_id: uuid.UUID


SamplingPlanResponse = Annotated[
    CompletedSamplingPlanResponse | LegacySamplingPlanResponse,
    Field(discriminator="status"),
]


class FindingDecisionRequest(_StrictApiModel):
    kind: Literal["finding"] = "finding"
    decision: ReviewDecision
    note: str | None = Field(default=None, min_length=1, max_length=2_000)


class SamplingDecisionRequest(_StrictApiModel):
    kind: Literal["clearance_sample"] = "clearance_sample"
    decision: SamplingReviewDecision
    note: str | None = Field(default=None, min_length=1, max_length=2_000)


ReviewDecisionRequest = Annotated[
    FindingDecisionRequest | SamplingDecisionRequest,
    Field(discriminator="kind"),
]


@router.get(
    "/api/review/sampling-config",
    response_model=SamplingConfigHistoryResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="get_sampling_config",
)
async def get_sampling_config_endpoint(
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> SamplingConfigHistoryResponse:
    _set_private_no_store(response)
    versions = await list_sampling_configs(db, tenant_id=auth.tenant_id)
    return SamplingConfigHistoryResponse(
        current=versions[0] if versions else None,
        history=versions,
    )


@router.put(
    "/api/review/sampling-config",
    response_model=SamplingConfigResult,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": SamplingConfigResult}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="put_sampling_config",
)
async def put_sampling_config_endpoint(
    payload: SamplingConfigCreateRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> SamplingConfigResult:
    _set_private_no_store(response)
    result = await create_sampling_config(
        db,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        expected_current_version=payload.expected_current_version,
        rate_bps=payload.rate_bps,
        min_sample_size=payload.min_sample_size,
        max_sample_size=payload.max_sample_size,
        change_reason=payload.change_reason,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return result


@router.post(
    "/api/reports/{report_id}/review-plan",
    response_model=CompletedSamplingPlanResponse,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": CompletedSamplingPlanResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.REVIEW_SUBMIT))],
    name="create_review_plan",
)
async def create_review_plan_endpoint(
    report_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> CompletedSamplingPlanResponse:
    _set_private_no_store(response)
    plan = await create_legacy_sampling_plan(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        report_run_id=report_id,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if plan.reused_existing else status.HTTP_201_CREATED
    return CompletedSamplingPlanResponse(plan=plan)


@router.get(
    "/api/reports/{report_id}/review-plan",
    response_model=SamplingPlanResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="get_review_plan",
)
async def get_review_plan_endpoint(
    report_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> SamplingPlanResponse:
    _set_private_no_store(response)
    plan = await get_sampling_plan(db, tenant_id=auth.tenant_id, report_run_id=report_id)
    if plan is None:
        return LegacySamplingPlanResponse(report_run_id=report_id)
    return CompletedSamplingPlanResponse(plan=plan)


@router.get(
    "/api/reviews/queue",
    response_model=ReviewQueuePage,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="list_queue",
)
async def list_review_queue_endpoint(
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    status: ReviewQueueStatus = "pending",
    kind: ReviewQueueKind | None = None,
    report_id: uuid.UUID | None = None,
    file_version_id: uuid.UUID | None = None,
    sort_by: Literal["default"] = "default",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReviewQueuePage:
    _set_private_no_store(response)
    return await list_review_queue(
        db,
        tenant_id=auth.tenant_id,
        status=status,
        kind=kind,
        report_run_id=report_id,
        file_version_id=file_version_id,
        sort_by=sort_by,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/api/reviews/findings/{report_item_id}",
    response_model=FindingReviewDetail,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="get_finding_detail",
)
async def get_finding_detail_endpoint(
    report_item_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> FindingReviewDetail:
    _set_private_no_store(response)
    return await get_finding_review_detail(
        db,
        tenant_id=auth.tenant_id,
        report_item_id=report_item_id,
    )


@router.get(
    "/api/reviews/samples/{sampling_audit_id}",
    response_model=ClearanceReviewDetail,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="get_sample_detail",
)
async def get_sample_detail_endpoint(
    sampling_audit_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> ClearanceReviewDetail:
    _set_private_no_store(response)
    return await get_sampling_review_detail(
        db,
        tenant_id=auth.tenant_id,
        sampling_audit_id=sampling_audit_id,
    )


@router.post(
    "/api/reviews/findings/{report_item_id}/decision",
    response_model=FindingReviewResult,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": FindingReviewResult}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.REVIEW_SUBMIT))],
    name="submit_finding_decision",
)
async def submit_finding_decision_endpoint(
    report_item_id: uuid.UUID,
    payload: ReviewDecisionRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> FindingReviewResult:
    _set_private_no_store(response)
    if not isinstance(payload, FindingDecisionRequest):
        raise ReviewInputError(code="REVIEW_DECISION_INVALID", message="复核结论类型无效")
    result = await submit_finding_review(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        report_item_id=report_item_id,
        decision=payload.decision,
        note=payload.note,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return result


@router.post(
    "/api/reviews/samples/{sampling_audit_id}/decision",
    response_model=SamplingReviewResult,
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_200_OK: {"model": SamplingReviewResult}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.REVIEW_SUBMIT))],
    name="submit_sample_decision",
)
async def submit_sample_decision_endpoint(
    sampling_audit_id: uuid.UUID,
    payload: ReviewDecisionRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> SamplingReviewResult:
    _set_private_no_store(response)
    if not isinstance(payload, SamplingDecisionRequest):
        raise ReviewInputError(code="REVIEW_DECISION_INVALID", message="抽检结论类型无效")
    result = await submit_sampling_review(
        db,
        session_factory,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        sampling_audit_id=sampling_audit_id,
        decision=payload.decision,
        note=payload.note,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return result


@router.get(
    "/api/reviews/summary",
    response_model=ReviewSummary,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.REVIEW_READ))],
    name="get_summary",
)
async def get_review_summary_endpoint(
    report_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> ReviewSummary:
    _set_private_no_store(response)
    return await get_review_summary(
        db,
        tenant_id=auth.tenant_id,
        report_run_id=report_id,
    )


def _set_private_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
