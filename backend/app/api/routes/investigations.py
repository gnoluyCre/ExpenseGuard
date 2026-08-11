"""Strongly typed F7 anomaly-investigation transport adapters."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthDep, SessionFactoryDep, SettingsDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.agent.capability import (
    InvestigationCapability,
    build_configured_provider,
    declare_investigation_capability,
)
from app.core.agent.db_tools import DbReadOnlyToolBackend
from app.core.agent.query_service import (
    get_investigation,
    list_candidate_investigations,
    list_investigation_steps,
)
from app.core.agent.run_service import run_investigation
from app.core.agent.seed_service import prepare_investigation_seed
from app.core.agent.service_models import (
    CheckpointReconciler,
    EvidenceStepPage,
    InvestigationDetail,
    InvestigationPage,
    InvestigationRunOptions,
    InvestigationUnavailableError,
)
from app.core.security.permissions import Permission

router = APIRouter(tags=["investigations"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    500: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class InvestigationRunResponse(_ApiModel):
    id: uuid.UUID
    correlation_finding_id: uuid.UUID
    detection_run_id: uuid.UUID
    file_version_id: uuid.UUID
    provider_kind: str
    provider_model: str
    max_steps: int = Field(ge=1, le=12)
    timeout_seconds: int = Field(ge=1, le=120)
    redaction_version: str
    agent_version: str
    action_schema_version: int
    prompt_template_version: str
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class EvidenceStepResponse(_ApiModel):
    id: uuid.UUID
    investigation_run_id: uuid.UUID
    step_no: int = Field(ge=1, le=12)
    action_kind: str
    model_action: dict[str, Any]
    decision_summary: str
    tool_name: str | None
    tool_input: dict[str, Any] | None
    tool_output: dict[str, Any] | None
    created_at: datetime


class InvestigationCitationResponse(_ApiModel):
    schema_version: Literal[1] = 1
    clause_id: uuid.UUID
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=4096)


class InvestigationResultResponse(_ApiModel):
    id: uuid.UUID
    investigation_run_id: uuid.UUID
    outcome: str
    evidence_sufficient: bool | None
    summary: str
    reason_code: str
    citations: tuple[InvestigationCitationResponse, ...] = Field(max_length=100)
    result_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime


class InvestigationResponse(_ApiModel):
    run: InvestigationRunResponse
    steps: tuple[EvidenceStepResponse, ...]
    result: InvestigationResultResponse | None
    reused_existing: bool


class InvestigationHistoryResponse(_ApiModel):
    items: tuple[InvestigationResponse, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class EvidenceStepPageResponse(_ApiModel):
    items: tuple[EvidenceStepResponse, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _detail_response(detail: InvestigationDetail) -> InvestigationResponse:
    return InvestigationResponse.model_validate(detail)


def _history_response(page: InvestigationPage) -> InvestigationHistoryResponse:
    return InvestigationHistoryResponse.model_validate(page)


def _steps_response(page: EvidenceStepPage) -> EvidenceStepPageResponse:
    return EvidenceStepPageResponse.model_validate(page)


def _reconciler(request: Request) -> CheckpointReconciler:
    value = getattr(request.app.state, "investigation_checkpoint_reconciler", None)
    if value is None:  # pragma: no cover - lifespan invariant
        raise RuntimeError("investigation checkpoint reconciler is not configured")
    return value  # type: ignore[no-any-return]


@router.get(
    "/api/v1/investigations/capability",
    response_model=InvestigationCapability,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="capability",
)
async def get_capability(response: Response, settings: SettingsDep) -> InvestigationCapability:
    _no_store(response)
    return declare_investigation_capability(settings)


@router.post(
    "/api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations",
    response_model=InvestigationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": InvestigationResponse}, **_ERRORS},
    dependencies=[Depends(require_permission(Permission.BATCH_IMPORT))],
    name="run",
)
async def run_investigation_endpoint(
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    request: Request,
    response: Response,
    auth: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> InvestigationResponse:
    secret = settings.pii_tokenization_key.get_secret_value().encode()
    if len(secret) < 32:
        raise InvestigationUnavailableError(
            code="INVESTIGATION_REDACTION_UNAVAILABLE",
            message="PII 脱敏密钥尚未安全配置",
        )
    prepared = await prepare_investigation_seed(
        session_factory,
        tenant_id=auth.tenant_id,
        detection_run_id=detection_run_id,
        correlation_finding_id=correlation_finding_id,
        pii_secret=secret,
        token_version=settings.pii_tokenization_version,
    )
    capability = declare_investigation_capability(settings)
    provider = build_configured_provider(settings)
    tools = DbReadOnlyToolBackend(
        session_factory=session_factory,
        file_version_id=prepared.file_version_id,
        pii_secret=secret,
        token_version=settings.pii_tokenization_version,
    )
    try:
        detail = await run_investigation(
            session_factory,
            tenant_id=auth.tenant_id,
            actor_id=auth.user_id,
            detection_run_id=detection_run_id,
            correlation_finding_id=correlation_finding_id,
            file_version_id=prepared.file_version_id,
            idempotency_key=idempotency_key,
            options=InvestigationRunOptions(
                provider_kind=settings.llm_provider,
                provider_model=settings.llm_model or "unconfigured",
                max_steps=settings.investigation_max_steps,
                timeout_seconds=settings.llm_timeout_seconds,
                redaction_version=f"v{settings.pii_tokenization_version}",
            ),
            seed=prepared.seed,
            provider=provider,
            tools=tools,
            reconciler=_reconciler(request),
            unavailable_reason_code=capability.reason_code,
        )
    finally:
        if provider is not None:
            await provider.aclose()
    response.status_code = status.HTTP_200_OK if detail.reused_existing else status.HTTP_201_CREATED
    _no_store(response)
    return _detail_response(detail)


@router.get(
    "/api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations",
    response_model=InvestigationHistoryResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="history",
)
async def get_investigation_history(
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvestigationHistoryResponse:
    page = await list_candidate_investigations(
        db,
        tenant_id=auth.tenant_id,
        detection_run_id=detection_run_id,
        correlation_finding_id=correlation_finding_id,
        limit=limit,
        offset=offset,
    )
    _no_store(response)
    return _history_response(page)


@router.get(
    "/api/v1/investigations/{investigation_run_id}",
    response_model=InvestigationResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="detail",
)
async def get_investigation_detail(
    investigation_run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> InvestigationResponse:
    detail = await get_investigation(
        db,
        tenant_id=auth.tenant_id,
        investigation_run_id=investigation_run_id,
    )
    _no_store(response)
    return _detail_response(detail)


@router.get(
    "/api/v1/investigations/{investigation_run_id}/steps",
    response_model=EvidenceStepPageResponse,
    responses=_ERRORS,
    dependencies=[Depends(require_permission(Permission.BATCH_READ))],
    name="steps",
)
async def get_investigation_steps(
    investigation_run_id: uuid.UUID,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EvidenceStepPageResponse:
    page = await list_investigation_steps(
        db,
        tenant_id=auth.tenant_id,
        investigation_run_id=investigation_run_id,
        limit=limit,
        offset=offset,
    )
    _no_store(response)
    return _steps_response(page)
