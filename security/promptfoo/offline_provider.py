"""Promptfoo provider that exercises the real F7 boundary without any network/model call."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.agent.models import ProviderResponse, TerminateAction
from app.core.agent.provider import ScriptedLlmProvider
from app.core.agent.step_runner import (
    SYSTEM_INSTRUCTION,
    execute_agent_step,
)
from app.core.agent.tools import ToolContext


class NoTools:
    """Any tool dispatch is a red-team failure for this terminal scripted action."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected tool access: {name}")


async def _evaluate(attack: str) -> dict[str, object]:
    provider = ScriptedLlmProvider(
        [
            ProviderResponse(
                action=TerminateAction(
                    outcome="insufficient",
                    evidence_sufficient=False,
                    reason_code="UNTRUSTED_INPUT_REQUIRES_REVIEW",
                    summary="不可信输入不能改变系统指令，已转人工复核",
                )
            )
        ]
    )
    execution = await execute_agent_step(
        provider=provider,
        tools=NoTools(),  # type: ignore[arg-type]
        context=ToolContext(
            tenant_id="00000000-0000-0000-0000-000000000001",
            detection_run_id="00000000-0000-0000-0000-000000000002",
            correlation_finding_id="00000000-0000-0000-0000-000000000003",
        ),
        step_no=1,
        evidence={"untrusted_expense_or_policy_text": attack},
        prior_steps=(),
    )
    captured = provider.prompts[0]
    return {
        "action_kind": execution.draft.action_kind,
        "outcome": execution.terminal.outcome if execution.terminal else None,
        "reason_code": execution.terminal.reason_code if execution.terminal else None,
        "provider_calls": provider.call_count,
        "tool_calls": 0,
        "network_calls": 0,
        "system_instruction_intact": captured.system_instruction == SYSTEM_INSTRUCTION,
        "attack_only_in_untrusted_evidence": attack in captured.evidence_json
        and attack not in captured.system_instruction,
    }


def call_api(
    prompt: str, options: dict[str, Any], context: dict[str, Any]
) -> dict[str, str]:
    del options, context
    return {"output": json.dumps(asyncio.run(_evaluate(prompt)), ensure_ascii=False)}
