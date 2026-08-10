"""Explicit, opt-in smoke check for the configured OpenAI-compatible F7 endpoint."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.agent.capability import (  # noqa: E402
    build_configured_provider,
    declare_investigation_capability,
)
from app.core.agent.models import InvestigationPrompt  # noqa: E402
from app.core.agent.step_runner import SYSTEM_INSTRUCTION  # noqa: E402
from app.settings import get_settings  # noqa: E402


async def _run() -> int:
    settings = get_settings()
    capability = declare_investigation_capability(settings)
    if capability.status != "enabled":
        sys.stdout.write(
            json.dumps(
                {
                    "status": "unavailable",
                    "reason_code": capability.reason_code,
                    "network_attempted": False,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 2
    provider = build_configured_provider(settings)
    if provider is None:  # pragma: no cover - guarded by capability
        raise RuntimeError("configured provider was not built")
    try:
        result = await provider.complete(
            InvestigationPrompt(
                system_instruction=SYSTEM_INSTRUCTION,
                evidence_json='{"candidate":"SMOKE_ONLY","pii":false}',
                prior_steps_json="[]",
            )
        )
        sys.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "action_kind": result.action.kind,
                    "usage": result.usage.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        return 0
    finally:
        await provider.aclose()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
