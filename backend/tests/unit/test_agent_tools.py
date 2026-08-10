import uuid

import pytest

from app.core.agent.models import GetEmployeeHistoryCall, GetSupplierHistoryCall
from app.core.agent.tools import (
    ToolContext,
    ToolExecutionError,
    ToolObservation,
    dispatch_read_only_tool,
)


class _Backend:
    def __init__(self) -> None:
        self.calls = 0

    async def get_correlation_rows(self, context, call):
        self.calls += 1
        return ToolObservation(tool_name=call.kind, data={"items": []})

    async def get_employee_history(self, context, call):
        self.calls += 1
        return ToolObservation(tool_name=call.kind, data={"employee": call.employee_token})

    async def get_supplier_history(self, context, call):
        self.calls += 1
        return ToolObservation(tool_name=call.kind, data={"supplier": call.supplier_token})

    async def search_policy_clauses(self, context, call):
        self.calls += 1
        return ToolObservation(tool_name=call.kind, data={"clauses": []})


def _context() -> ToolContext:
    return ToolContext(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        detection_run_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
        correlation_finding_id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        allowed_employee_tokens=frozenset({"EMP_v1_0123456789abcdef"}),
        allowed_supplier_tokens=frozenset({"SUP_v1_0123456789abcdef"}),
    )


@pytest.mark.asyncio
async def test_dispatch_rejects_employee_outside_current_context() -> None:
    backend = _Backend()
    with pytest.raises(ToolExecutionError) as raised:
        await dispatch_read_only_tool(
            backend=backend,
            context=_context(),
            call=GetEmployeeHistoryCall(employee_token="EMP_v1_fedcba9876543210"),
        )
    assert raised.value.code == "INVESTIGATION_EMPLOYEE_TOKEN_FORBIDDEN"
    assert backend.calls == 0


@pytest.mark.asyncio
async def test_dispatch_allows_scoped_supplier_and_caps_output() -> None:
    backend = _Backend()
    observation = await dispatch_read_only_tool(
        backend=backend,
        context=_context(),
        call=GetSupplierHistoryCall(supplier_token="SUP_v1_0123456789abcdef"),
    )
    assert observation.tool_name == "get_supplier_history"
    assert backend.calls == 1
    with pytest.raises(ToolExecutionError) as raised:
        await dispatch_read_only_tool(
            backend=backend,
            context=_context(),
            call=GetSupplierHistoryCall(supplier_token="SUP_v1_0123456789abcdef"),
            max_output_bytes=8,
        )
    assert raised.value.code == "INVESTIGATION_TOOL_OUTPUT_TOO_LARGE"
