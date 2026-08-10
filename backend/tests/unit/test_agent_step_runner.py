import uuid

import pytest

from app.core.agent.models import (
    GetCorrelationRowsCall,
    InvestigationOutcome,
    ProviderResponse,
    TerminateAction,
    ToolCallAction,
)
from app.core.agent.provider import ScriptedLlmProvider
from app.core.agent.step_runner import execute_agent_step
from app.core.agent.tools import ToolContext, ToolObservation


class _Tools:
    async def get_correlation_rows(self, context, call):
        return ToolObservation(tool_name=call.kind, data={"rows": [{"row_no": 2}]})

    async def get_employee_history(self, context, call):
        return ToolObservation(tool_name=call.kind, data={"items": []})

    async def get_supplier_history(self, context, call):
        return ToolObservation(tool_name=call.kind, data={"items": []})

    async def search_policy_clauses(self, context, call):
        return ToolObservation(tool_name=call.kind, data={"items": []})


def _context() -> ToolContext:
    return ToolContext(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        detection_run_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
        correlation_finding_id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
    )


@pytest.mark.asyncio
async def test_tool_step_has_stable_payload_fingerprint() -> None:
    response = ProviderResponse(
        action=ToolCallAction(
            call=GetCorrelationRowsCall(limit=5),
            rationale_summary="先核对全部参与行",
        )
    )
    first = await execute_agent_step(
        provider=ScriptedLlmProvider([response]),
        tools=_Tools(),
        context=_context(),
        step_no=1,
        evidence={"candidate": "split_invoice"},
        prior_steps=(),
    )
    second = await execute_agent_step(
        provider=ScriptedLlmProvider([response]),
        tools=_Tools(),
        context=_context(),
        step_no=1,
        evidence={"candidate": "split_invoice"},
        prior_steps=(),
    )
    assert first.draft.payload_fingerprint == second.draft.payload_fingerprint
    assert first.draft.tool_name == "get_correlation_rows"
    assert first.terminal is None


@pytest.mark.asyncio
async def test_terminate_step_never_calls_tool() -> None:
    response = ProviderResponse(
        action=TerminateAction(
            outcome=InvestigationOutcome.INSUFFICIENT,
            evidence_sufficient=False,
            reason_code="EVIDENCE_NOT_ENOUGH",
            summary="证据不足，转人工复核",
        )
    )
    execution = await execute_agent_step(
        provider=ScriptedLlmProvider([response]),
        tools=_Tools(),
        context=_context(),
        step_no=2,
        evidence={"candidate": "frequency_anomaly"},
        prior_steps=({"step_no": 1},),
    )
    assert execution.draft.action_kind == "terminate"
    assert execution.draft.tool_name is None
    assert execution.terminal is not None
    assert execution.terminal.evidence_sufficient is False
