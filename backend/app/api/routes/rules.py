"""确定性规则配置 API。"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthDep, TenantDbDep, require_permission
from app.api.errors import ErrorResponse
from app.core.rules import RuleDefinition, validate_rule_definition
from app.core.security.permissions import Permission
from app.core.validation.rule_service import list_rule_versions, save_rule_version
from app.db.models.config import RuleConfig

router = APIRouter(prefix="/api/rules", tags=["rules"])


class SaveRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=128)
    effective_from: date
    definition: RuleDefinition


class RuleVersionResponse(BaseModel):
    id: uuid.UUID
    rule_id: str
    version: int
    effective_from: date
    config_fingerprint: str
    definition: RuleDefinition
    created_by: uuid.UUID
    created_at: datetime


class SaveRuleResponse(RuleVersionResponse):
    reused_existing: bool


def _response(rule: RuleConfig) -> RuleVersionResponse:
    if rule.effective_from is None or rule.config_fingerprint is None or rule.created_by is None:
        raise RuntimeError("F3 规则版本缺少必要字段")
    return RuleVersionResponse(
        id=rule.id,
        rule_id=rule.rule_id,
        version=rule.version,
        effective_from=rule.effective_from,
        config_fingerprint=rule.config_fingerprint,
        definition=validate_rule_definition(rule.definition),
        created_by=rule.created_by,
        created_at=rule.created_at,
    )


@router.get(
    "",
    response_model=list[RuleVersionResponse],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_permission(Permission.CONFIG_READ))],
    name="list",
)
async def list_rules_endpoint(
    db: TenantDbDep,
    rule_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    latest_only: bool = True,
) -> list[RuleVersionResponse]:
    return [
        _response(item)
        for item in await list_rule_versions(db, rule_id=rule_id, latest_only=latest_only)
    ]


@router.put(
    "",
    response_model=SaveRuleResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": SaveRuleResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    dependencies=[Depends(require_permission(Permission.CONFIG_WRITE))],
    name="save",
)
async def save_rule_endpoint(
    payload: SaveRuleRequest, response: Response, db: TenantDbDep, auth: AuthDep
) -> SaveRuleResponse:
    result = await save_rule_version(
        db,
        tenant_id=auth.tenant_id,
        created_by=auth.user_id,
        rule_id=payload.rule_id,
        effective_from=payload.effective_from,
        definition=payload.definition,
    )
    response.status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return SaveRuleResponse(
        **_response(result.rule_config).model_dump(), reused_existing=not result.created
    )
