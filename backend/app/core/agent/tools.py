"""Fail-closed dispatcher for the four F7 read-only tools."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.models import (
    GetCorrelationRowsCall,
    GetEmployeeHistoryCall,
    GetSupplierHistoryCall,
    ReadOnlyToolCall,
    SearchPolicyClausesCall,
)
from app.core.errors import ExpenseGuardError


class ToolExecutionError(ExpenseGuardError):
    status_code = 409


class ToolContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: uuid.UUID
    detection_run_id: uuid.UUID
    correlation_finding_id: uuid.UUID
    allowed_employee_tokens: frozenset[str] = Field(default_factory=frozenset)
    allowed_supplier_tokens: frozenset[str] = Field(default_factory=frozenset)


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str = Field(min_length=1, max_length=64)
    data: dict[str, Any]
    has_more: bool = False


class ReadOnlyToolBackend(Protocol):
    async def get_correlation_rows(
        self, context: ToolContext, call: GetCorrelationRowsCall
    ) -> ToolObservation: ...

    async def get_employee_history(
        self, context: ToolContext, call: GetEmployeeHistoryCall
    ) -> ToolObservation: ...

    async def get_supplier_history(
        self, context: ToolContext, call: GetSupplierHistoryCall
    ) -> ToolObservation: ...

    async def search_policy_clauses(
        self, context: ToolContext, call: SearchPolicyClausesCall
    ) -> ToolObservation: ...


async def dispatch_read_only_tool(
    *,
    backend: ReadOnlyToolBackend,
    context: ToolContext,
    call: ReadOnlyToolCall,
    max_output_bytes: int = 64_000,
) -> ToolObservation:
    """Validate subject scope before calling a backend and cap persisted output."""

    if isinstance(call, GetCorrelationRowsCall):
        observation = await backend.get_correlation_rows(context, call)
    elif isinstance(call, GetEmployeeHistoryCall):
        if call.employee_token not in context.allowed_employee_tokens:
            raise ToolExecutionError(
                code="INVESTIGATION_EMPLOYEE_TOKEN_FORBIDDEN",
                message="员工 token 不属于当前调查上下文",
            )
        observation = await backend.get_employee_history(context, call)
    elif isinstance(call, GetSupplierHistoryCall):
        if call.supplier_token not in context.allowed_supplier_tokens:
            raise ToolExecutionError(
                code="INVESTIGATION_SUPPLIER_TOKEN_FORBIDDEN",
                message="供应商 token 不属于当前调查上下文",
            )
        observation = await backend.get_supplier_history(context, call)
    elif isinstance(call, SearchPolicyClausesCall):
        observation = await backend.search_policy_clauses(context, call)
    else:
        raise ToolExecutionError(
            code="INVESTIGATION_TOOL_FORBIDDEN",
            message="模型请求了未授权工具",
        )
    encoded = json.dumps(
        observation.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if len(encoded) > max_output_bytes:
        raise ToolExecutionError(
            code="INVESTIGATION_TOOL_OUTPUT_TOO_LARGE",
            message="只读工具结果超过安全上限",
        )
    return observation
