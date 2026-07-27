"""Schema 映射版本查询与追加写 API。"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.deps import AuthDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.parsing.api_service import (
    MappingVersionView,
    list_mapping_versions,
    save_mapping_version,
)
from app.core.parsing.mapping import MappingValidationError
from app.core.parsing.models import (
    AvailabilityThresholds,
    InferenceConfig,
    InferenceRule,
    MappingEntry,
    UnifiedField,
)
from app.core.security.permissions import Permission

router = APIRouter(prefix="/api/schema-mappings", tags=["schema-mappings"])


class StrictApiModel(BaseModel):
    """拒绝客户端偷偷注入租户、版本或其他未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class MappingEntryRequest(StrictApiModel):
    source_column: str = Field(min_length=1, max_length=255)
    target_field: str


class AvailabilityThresholdsRequest(StrictApiModel):
    available_min_non_null_rate: Decimal = Decimal("0.80")
    inferred_min_success_rate: Decimal = Decimal("0.80")


class SaveSchemaMappingRequest(StrictApiModel):
    file_version_id: uuid.UUID
    mappings: tuple[MappingEntryRequest, ...]
    availability_thresholds: AvailabilityThresholdsRequest = Field(
        default_factory=AvailabilityThresholdsRequest
    )
    currency_aliases: dict[str, str] = Field(default_factory=dict)
    inference_rules: tuple[dict[str, object], ...] = ()


class MappingEntryResponse(BaseModel):
    source_column: str
    target_field: UnifiedField


class AvailabilityThresholdsResponse(BaseModel):
    available_min_non_null_rate: str
    inferred_min_success_rate: str


class MappingVersionResponse(BaseModel):
    id: uuid.UUID
    version: int
    created_at: datetime
    created_by: uuid.UUID | None
    is_current_for_batch: bool
    mappings: list[MappingEntryResponse]
    availability_thresholds: AvailabilityThresholdsResponse
    currency_aliases: dict[str, str]
    inference_rules: list[InferenceRule]


class SchemaMappingsResponse(BaseModel):
    file_version_id: uuid.UUID
    header_signature: str
    source_columns: list[str]
    versions: list[MappingVersionResponse]


class SaveSchemaMappingResponse(MappingVersionResponse):
    reused_existing: bool


@router.get(
    "",
    response_model=SchemaMappingsResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    name="list",
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
)
async def list_schema_mappings_endpoint(
    file_version_id: uuid.UUID,
    db: TenantDbDep,
) -> SchemaMappingsResponse:
    """返回当前批次表头可精确复用的映射版本。"""
    result = await list_mapping_versions(db, file_version_id=file_version_id)
    return SchemaMappingsResponse(
        file_version_id=result.file_version_id,
        header_signature=result.header_signature,
        source_columns=list(result.source_columns),
        versions=[_version_response(version) for version in result.versions],
    )


@router.put(
    "",
    response_model=SaveSchemaMappingResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {"model": SaveSchemaMappingResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    name="save",
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
)
async def save_schema_mapping_endpoint(
    payload: SaveSchemaMappingRequest,
    response: Response,
    db: TenantDbDep,
    auth: AuthDep,
) -> SaveSchemaMappingResponse:
    """追加一个不可变映射版本；相同内容重试幂等复用。"""
    mappings, thresholds, inference = _validated_config(payload)
    result = await save_mapping_version(
        db,
        tenant_id=auth.tenant_id,
        actor_id=auth.user_id,
        file_version_id=payload.file_version_id,
        mappings=mappings,
        availability_thresholds=thresholds,
        currency_aliases=payload.currency_aliases,
        inference_config=inference,
    )
    response.status_code = status.HTTP_200_OK if result.reused_existing else status.HTTP_201_CREATED
    return SaveSchemaMappingResponse(
        **_version_response(result.version).model_dump(),
        reused_existing=result.reused_existing,
    )


def _validated_config(
    payload: SaveSchemaMappingRequest,
) -> tuple[tuple[MappingEntry, ...], AvailabilityThresholds, InferenceConfig]:
    try:
        mappings = tuple(
            MappingEntry.model_validate(item.model_dump()) for item in payload.mappings
        )
    except ValidationError as exc:
        raise MappingValidationError(
            code="MAPPING_TARGET_FIELD_UNKNOWN",
            message="映射包含未知目标字段",
        ) from exc
    try:
        thresholds = AvailabilityThresholds.model_validate(
            payload.availability_thresholds.model_dump()
        )
    except ValidationError as exc:
        raise MappingValidationError(
            code="MAPPING_THRESHOLD_INVALID",
            message="字段可用性阈值必须位于 0 到 1 之间",
        ) from exc
    try:
        inference = InferenceConfig.model_validate({"rules": payload.inference_rules})
    except ValidationError as exc:
        raise MappingValidationError(
            code="MAPPING_INFERENCE_INVALID",
            message="映射推断配置无效",
        ) from exc
    return mappings, thresholds, inference


def _version_response(version: MappingVersionView) -> MappingVersionResponse:
    quantum = Decimal("0.0001")
    return MappingVersionResponse(
        id=version.id,
        version=version.version,
        created_at=version.created_at,
        created_by=version.created_by,
        is_current_for_batch=version.is_current_for_batch,
        mappings=[
            MappingEntryResponse(
                source_column=item.source_column,
                target_field=item.target_field,
            )
            for item in version.mappings
        ],
        availability_thresholds=AvailabilityThresholdsResponse(
            available_min_non_null_rate=format(
                version.availability_thresholds.available_min_non_null_rate.quantize(quantum),
                "f",
            ),
            inferred_min_success_rate=format(
                version.availability_thresholds.inferred_min_success_rate.quantize(quantum),
                "f",
            ),
        ),
        currency_aliases=version.currency_aliases,
        inference_rules=list(version.inference_config.rules),
    )
