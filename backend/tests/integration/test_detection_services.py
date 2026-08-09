"""CP-F6.3 config, run, query, idempotency, and recovery tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.detection.run_service as run_module
from app.core.detection.config_service import create_detection_config
from app.core.detection.errors import DetectionError, DetectionInternalError
from app.core.detection.models import DetectionProfileDefinition, UnifiedField
from app.core.detection.query_service import (
    get_batch_detection,
    get_correlation_finding,
    list_detection_findings,
)
from app.core.detection.run_service import run_detection
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import ExpenseRow, FieldAvailability, FieldStatus, FileVersion, ParseStatus
from app.db.models.config import SchemaMappingVersion
from app.db.models.detection import (
    CorrelationFindingRow,
    DetectionConfig,
    DetectionRequest,
    DetectionRun,
)
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding
from app.db.models.tenancy import AppUser, Role, Tenant
from tests.unit.detection.helpers import profile_data, record

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _seed_batch(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        session.add(tenant)
        await session.flush()
        bind_tenant(session.sync_session, tenant.id)
        actor = AppUser(
            tenant_id=tenant.id,
            username="auditor",
            password_hash="test-only",
            role=Role.AUDITOR,
            is_active=True,
        )
        session.add(actor)
        await session.flush()
        mapping = SchemaMappingVersion(
            tenant_id=tenant.id,
            header_signature="1" * 64,
            version=1,
            config_fingerprint="2" * 64,
            availability_thresholds={
                "available_min_non_null_rate": "0.8000",
                "inferred_min_success_rate": "0.8000",
            },
            currency_aliases={},
            inference_config={"rules": []},
            backfilled_legacy=False,
            created_by=actor.id,
        )
        session.add(mapping)
        await session.flush()
        batch = FileVersion(
            tenant_id=tenant.id,
            filename="f6.xlsx",
            content_hash="3" * 64,
            row_count=3,
            uploaded_by=actor.id,
            mapping_version_id=mapping.id,
            parse_status=ParseStatus.PARSED,
            revision_no=1,
        )
        session.add(batch)
        await session.flush()
        records = (
            record(
                amount="600",
                employee="employee-a",
                merchant="商户甲",
                invoice_no="N001",
                location="上海",
            ),
            record(
                amount="600",
                employee="employee-a",
                merchant="商户甲",
                invoice_no="N002",
                location="北京",
            ),
            record(
                amount="100",
                employee="employee-a",
                merchant="商户甲",
                invoice_no="N003",
                location="上海",
            ),
        )
        for row_no, normalized in enumerate(records, start=1):
            snapshot = normalized.model_copy(update={"mapping_version_id": mapping.id})
            session.add(
                ExpenseRow(
                    tenant_id=tenant.id,
                    file_version_id=batch.id,
                    row_no=row_no,
                    raw_json={"row": row_no},
                    normalized_json=snapshot.model_dump(mode="json"),
                )
            )
        for field in UnifiedField:
            session.add(
                FieldAvailability(
                    tenant_id=tenant.id,
                    file_version_id=batch.id,
                    field_name=field.value,
                    status=FieldStatus.AVAILABLE,
                    evidence=None,
                )
            )
        await session.commit()
        return tenant.id, actor.id, batch.id


async def _create_profile(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        result = await create_detection_config(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=0,
            definition=DetectionProfileDefinition.model_validate(profile_data()),
            change_reason="initial deterministic profile",
            idempotency_key="detection-config-key-1",
        )
        await session.commit()
        return result.id


async def _clone_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    source_file_id: uuid.UUID,
) -> uuid.UUID:
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        source = await session.scalar(select(FileVersion).where(FileVersion.id == source_file_id))
        assert source is not None and source.mapping_version_id is not None
        clone = FileVersion(
            tenant_id=tenant_id,
            filename="f6-clone.xlsx",
            content_hash="4" * 64,
            row_count=source.row_count,
            uploaded_by=actor_id,
            mapping_version_id=source.mapping_version_id,
            parse_status=source.parse_status,
            revision_no=1,
        )
        session.add(clone)
        await session.flush()
        rows = tuple(
            (
                await session.scalars(
                    select(ExpenseRow)
                    .where(ExpenseRow.file_version_id == source_file_id)
                    .order_by(ExpenseRow.row_no)
                )
            ).all()
        )
        for row in rows:
            session.add(
                ExpenseRow(
                    tenant_id=tenant_id,
                    file_version_id=clone.id,
                    row_no=row.row_no,
                    raw_json=row.raw_json,
                    normalized_json=row.normalized_json,
                    parse_error=row.parse_error,
                    parse_error_code=row.parse_error_code,
                    parse_error_detail=row.parse_error_detail,
                )
            )
        availability = tuple(
            (
                await session.scalars(
                    select(FieldAvailability).where(
                        FieldAvailability.file_version_id == source_file_id
                    )
                )
            ).all()
        )
        for item in availability:
            session.add(
                FieldAvailability(
                    tenant_id=tenant_id,
                    file_version_id=clone.id,
                    field_name=item.field_name,
                    status=item.status,
                    evidence=item.evidence,
                )
            )
        await session.commit()
        return clone.id


async def test_config_idempotency_and_atomic_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _ = await _seed_batch(
        session_factory, slug=f"f6-config-{uuid.uuid4().hex[:8]}"
    )
    definition = DetectionProfileDefinition.model_validate(profile_data())
    arguments = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "expected_current_version": 0,
        "definition": definition,
        "change_reason": "initial deterministic profile",
        "idempotency_key": "detection-config-key-1",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await create_detection_config(session, session_factory, **arguments)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replayed = await create_detection_config(session, session_factory, **arguments)
        await session.commit()
    assert replayed.id == created.id
    assert replayed.reused_existing is True
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionConfig)) == 1
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "detection.config_create")
                )
            ).all()
        )
    assert len(audits) == 1
    assert "initial deterministic profile" not in str(audits[0].payload_json)

    changed = profile_data()
    detectors = changed["detectors"]
    assert isinstance(detectors, list) and isinstance(detectors[0], dict)
    detectors[0]["date_window_days"] = 3
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(DetectionError, match="Idempotency-Key") as error:
            await create_detection_config(
                session,
                session_factory,
                **(arguments | {"definition": changed}),
            )
        assert error.value.code == "IDEMPOTENCY_KEY_REUSED"


async def test_run_replay_alias_query_and_input_drift(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f6-run-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="detection-run-key-1",
        )
        await session.commit()
    assert created.reused_existing is False
    assert created.finding_count > 0

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("completed replay must not execute detectors")

    monkeypatch.setattr(run_module, "run_detection_core", forbidden)
    for key in ("detection-run-key-1", "detection-run-key-2"):
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            replay = await run_detection(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_id,
                idempotency_key=key,
            )
            await session.commit()
        assert replay.id == created.id
        assert replay.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionRun)) == 1
        assert await session.scalar(select(func.count()).select_from(DetectionRequest)) == 2
        assert await session.scalar(select(func.count()).select_from(CapabilityDeclaration)) == 4
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "detection.run_complete")
            )
            == 1
        )
        batch_view = await get_batch_detection(
            session, tenant_id=tenant_id, file_version_id=file_id
        )
        page = await list_detection_findings(session, tenant_id=tenant_id, run_id=created.id)
        detail = await get_correlation_finding(
            session, tenant_id=tenant_id, finding_id=page.items[0].id, row_limit=1
        )
    assert len(batch_view.capabilities) == 4
    assert page.total == created.finding_count
    assert detail.total >= 2 and len(detail.rows) == 1

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        row = await session.scalar(
            select(ExpenseRow).where(ExpenseRow.file_version_id == file_id, ExpenseRow.row_no == 1)
        )
        assert row is not None and row.normalized_json is not None
        row.normalized_json = row.normalized_json | {"amount": "601"}
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(DetectionError) as drift:
            await run_detection(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_id,
                idempotency_key="detection-run-key-1",
            )
        assert drift.value.code == "DETECTION_INPUT_DRIFT"


@pytest.mark.parametrize(
    "stage",
    [
        "run_written",
        "declaration_written",
        "finding_written",
        "row_link_written",
        "request_ledger_written",
        "success_audit_written",
    ],
)
async def test_fault_points_roll_back_and_restart_without_duplicates(
    session_factory: async_sessionmaker[AsyncSession], stage: str
) -> None:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f6-recovery-{stage}-{uuid.uuid4().hex[:6]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)

    def fail_at(current: str) -> None:
        if current == stage:
            raise RuntimeError("simulated-process-loss-private-value")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(DetectionInternalError):
            await run_detection(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_id,
                idempotency_key="detection-recovery-key",
                fault_hook=fail_at,
            )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        for model in (
            DetectionRun,
            DetectionRequest,
            CapabilityDeclaration,
            CorrelationFinding,
            CorrelationFindingRow,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        failed = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "detection.run_failed")
                )
            ).all()
        )
    assert len(failed) == 1
    assert "simulated-process-loss-private-value" not in str(failed[0].payload_json)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        recovered = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="detection-recovery-key",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionRun)) == 1
        assert await session.scalar(select(func.count()).select_from(DetectionRequest)) == 1
        assert await session.scalar(select(func.count()).select_from(CapabilityDeclaration)) == 4
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "detection.run_complete")
            )
            == 1
        )
    assert recovered.finding_count > 0


async def test_hard_process_exit_rolls_back_before_fresh_process_restart(
    session_factory: async_sessionmaker[AsyncSession], db_url: str
) -> None:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f6-hard-kill-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    worker = Path(__file__).parents[1] / "helpers" / "f6_kill_worker.py"
    backend_dir = Path(__file__).parents[2]
    environment = {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ
    }
    environment.update(
        {
            "F6_KILL_ACTOR_ID": str(actor_id),
            "F6_KILL_DB_URL": db_url,
            "F6_KILL_FILE_ID": str(file_id),
            "F6_KILL_STAGE": "success_audit_written",
            "F6_KILL_TENANT_ID": str(tenant_id),
            "PYTHONPATH": str(backend_dir),
        }
    )
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(worker)],
        capture_output=True,
        check=False,
        env=environment,
        timeout=30,
    )
    assert process.returncode == 91, process.stderr.decode("utf-8", errors="replace")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        for model in (
            DetectionRun,
            DetectionRequest,
            CapabilityDeclaration,
            CorrelationFinding,
            CorrelationFindingRow,
        ):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "detection.run_complete")
            )
            == 0
        )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        recovered = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="detection-hard-kill-key",
        )
        await session.commit()
    assert recovered.finding_count > 0


async def test_tenant_nowait_conflict_leaves_no_partial_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f6-lock-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as holder, session_factory() as contender:
        bind_tenant(holder.sync_session, tenant_id)
        bind_tenant(contender.sync_session, tenant_id)
        await lock_tenant_nowait(holder, tenant_id)
        with pytest.raises(DetectionError) as conflict:
            await run_detection(
                contender,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_id,
                idempotency_key="detection-lock-key",
            )
        assert conflict.value.code == "DETECTION_CONFLICT"
        await holder.rollback()
        await contender.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionRun)) == 0
        assert await session.scalar(select(func.count()).select_from(DetectionRequest)) == 0


async def test_same_key_for_different_file_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, first_file_id = await _seed_batch(
        session_factory, slug=f"f6-key-scope-{uuid.uuid4().hex[:8]}"
    )
    second_file_id = await _clone_batch(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        source_file_id=first_file_id,
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=first_file_id,
            idempotency_key="detection-file-scoped-key",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(DetectionError) as reused:
            await run_detection(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=second_file_id,
                idempotency_key="detection-file-scoped-key",
            )
        assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"


async def test_profile_change_creates_new_zero_finding_run_with_four_declarations(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f6-profile-change-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="detection-profile-run-1",
        )
        await session.commit()
    disabled = profile_data()
    detectors = disabled["detectors"]
    assert isinstance(detectors, list)
    for detector in detectors:
        assert isinstance(detector, dict)
        detector["enabled"] = False
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await create_detection_config(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=1,
            definition=disabled,
            change_reason="disable all detectors for explicit capability test",
            idempotency_key="detection-config-key-2",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        second = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="detection-profile-run-2",
        )
        await session.commit()
    assert second.id != first.id
    assert second.finding_count == 0
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionRun)) == 2
        assert await session.scalar(select(func.count()).select_from(CapabilityDeclaration)) == 8
        zero_declarations = tuple(
            (
                await session.scalars(
                    select(CapabilityDeclaration).where(
                        CapabilityDeclaration.detection_run_id == second.id
                    )
                )
            ).all()
        )
    assert len(zero_declarations) == 4
    assert all(item.finding_count == 0 for item in zero_declarations)


async def test_config_failure_rolls_back_and_records_safe_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _ = await _seed_batch(
        session_factory, slug=f"f6-config-failure-{uuid.uuid4().hex[:8]}"
    )

    def fail_at(stage: str) -> None:
        if stage == "success_audit_written":
            raise RuntimeError("private-config-failure")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(DetectionInternalError):
            await create_detection_config(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                expected_current_version=0,
                definition=profile_data(),
                change_reason="initial profile",
                idempotency_key="detection-config-failure-key",
                fault_hook=fail_at,
            )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(DetectionConfig)) == 0
        failed = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "detection.config_failed")
                )
            ).all()
        )
    assert len(failed) == 1
    assert "private-config-failure" not in str(failed[0].payload_json)
