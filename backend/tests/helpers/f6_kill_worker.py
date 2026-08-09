"""Subprocess worker used to prove PostgreSQL rollback after a hard F6 process exit."""

from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.asyncio_compat import configure_event_loop_policy
from app.core.detection.run_service import run_detection
from app.core.tenancy.scope import bind_tenant, install_tenant_guard


async def _run() -> None:
    url = os.environ["F6_KILL_DB_URL"]
    tenant_id = uuid.UUID(os.environ["F6_KILL_TENANT_ID"])
    actor_id = uuid.UUID(os.environ["F6_KILL_ACTOR_ID"])
    file_version_id = uuid.UUID(os.environ["F6_KILL_FILE_ID"])
    stage = os.environ["F6_KILL_STAGE"]
    engine = create_async_engine(url, pool_pre_ping=True)
    install_tenant_guard()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    def hard_exit(current: str) -> None:
        if current == stage:
            os._exit(91)

    async with factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await run_detection(
            session,
            factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            idempotency_key="detection-hard-kill-key",
            fault_hook=hard_exit,
        )


if __name__ == "__main__":
    configure_event_loop_policy()
    asyncio.run(_run())
