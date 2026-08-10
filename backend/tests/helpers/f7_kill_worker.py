"""Subprocess worker proving F7 terminal rollback and business-fact recovery."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import cast

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.asyncio_compat import configure_event_loop_policy
from app.core.agent.models import ProviderResponse, TerminateAction
from app.core.agent.provider import ScriptedLlmProvider
from app.core.agent.run_service import run_investigation
from app.core.agent.service_models import InvestigationRunOptions, InvestigationSeed
from app.core.agent.tools import ReadOnlyToolBackend, ToolContext
from app.core.tenancy.scope import install_tenant_guard


class NoTools:
    """Terminate-only worker backend; any tool access is a test failure."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected tool access: {name}")


async def _run() -> None:
    tenant_id = uuid.UUID(os.environ["F7_KILL_TENANT_ID"])
    actor_id = uuid.UUID(os.environ["F7_KILL_ACTOR_ID"])
    detection_run_id = uuid.UUID(os.environ["F7_KILL_RUN_ID"])
    finding_id = uuid.UUID(os.environ["F7_KILL_FINDING_ID"])
    file_version_id = uuid.UUID(os.environ["F7_KILL_FILE_ID"])
    engine = create_async_engine(os.environ["F7_KILL_DB_URL"], pool_pre_ping=True)
    install_tenant_guard()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    def hard_exit(stage: str) -> None:
        if stage == "terminal_audit_written":
            os._exit(91)

    await run_investigation(
        factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        detection_run_id=detection_run_id,
        correlation_finding_id=finding_id,
        file_version_id=file_version_id,
        idempotency_key="f7-hard-kill-key",
        options=InvestigationRunOptions(
            provider_kind="openai_compatible",
            provider_model="scripted-test",
            max_steps=6,
            timeout_seconds=30,
            redaction_version="v1",
        ),
        seed=InvestigationSeed(
            evidence={
                "schema_version": 1,
                "candidate": {"finding_id": str(finding_id), "rows": [1, 2]},
            },
            tool_context=ToolContext(
                tenant_id=tenant_id,
                detection_run_id=detection_run_id,
                correlation_finding_id=finding_id,
            ),
        ),
        provider=ScriptedLlmProvider(
            [
                ProviderResponse(
                    action=TerminateAction(
                        outcome="sufficient",
                        evidence_sufficient=True,
                        reason_code="EVIDENCE_SUFFICIENT",
                        summary="证据充分",
                    )
                )
            ]
        ),
        tools=cast(ReadOnlyToolBackend, NoTools()),
        fault_hook=hard_exit,
    )


if __name__ == "__main__":
    configure_event_loop_policy()
    asyncio.run(_run())
