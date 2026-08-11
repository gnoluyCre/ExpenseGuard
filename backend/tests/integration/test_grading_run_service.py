"""PostgreSQL integration and rollback-recovery gates for CP-F8.3."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.grading.run_service as run_module
from app.core.detection.config_service import create_detection_config
from app.core.detection.models import DetectionProfileDefinition
from app.core.detection.run_service import run_detection
from app.core.grading.config_service import create_grading_config
from app.core.grading.errors import GradingError, GradingInternalError
from app.core.grading.run_service import run_grading
from app.core.grading.service_models import F7NotRunManifestRequest, GradingRunView
from app.core.parsing.models import NormalizedExpenseRecord, UnifiedField
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import ExpenseRow, FieldAvailability, FieldStatus, FileVersion, ParseStatus
from app.db.models.config import SchemaMappingVersion
from app.db.models.detection import CorrelationFindingRow, DetectionRequest, DetectionRun
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding, Finding
from app.db.models.grading import GradingItem, GradingItemRow, GradingRequest, GradingRun
from app.db.models.tenancy import AppUser, Role, Tenant
from app.db.models.validation import ValidationRun, ValidationRunStatus
from tests.unit.detection.helpers import profile_data, record
from tests.unit.grading.helpers import config, config_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


@dataclass(frozen=True)
class SeededRun:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    file_version_id: uuid.UUID
    validation_run_id: uuid.UUID
    detection_run_id: uuid.UUID
    grading_config_id: uuid.UUID
    f7_requests: tuple[F7NotRunManifestRequest, ...]


async def seed_grading_run_sources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    slug: str,
    with_sources: bool = True,
) -> SeededRun:
    """Create committed F3/F6/config facts suitable for run/query service tests."""

    async with session_factory() as db:
        tenant = Tenant(slug=slug, name=f"{slug} tenant")
        db.add(tenant)
        await db.flush()
        bind_tenant(db.sync_session, tenant.id)
        actor = AppUser(
            tenant_id=tenant.id,
            username="auditor",
            password_hash="test-only",
            role=Role.AUDITOR,
            is_active=True,
        )
        db.add(actor)
        await db.flush()
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
        db.add(mapping)
        await db.flush()
        rows: tuple[NormalizedExpenseRecord, ...] = (
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
        if not with_sources:
            rows = (record(amount="100", invoice_no="N900"),)
        file = FileVersion(
            tenant_id=tenant.id,
            filename="f8.xlsx",
            content_hash="3" * 64,
            row_count=len(rows),
            uploaded_by=actor.id,
            mapping_version_id=mapping.id,
            parse_status=ParseStatus.PARSED,
            revision_no=1,
        )
        db.add(file)
        await db.flush()
        for row_no, normalized in enumerate(rows, start=1):
            snapshot = normalized.model_copy(update={"mapping_version_id": mapping.id})
            db.add(
                ExpenseRow(
                    tenant_id=tenant.id,
                    file_version_id=file.id,
                    row_no=row_no,
                    raw_json={"row": row_no},
                    normalized_json=snapshot.model_dump(mode="json"),
                )
            )
        for field in UnifiedField:
            db.add(
                FieldAvailability(
                    tenant_id=tenant.id,
                    file_version_id=file.id,
                    field_name=field.value,
                    status=FieldStatus.AVAILABLE,
                    evidence=None,
                )
            )
        validation = ValidationRun(
            tenant_id=tenant.id,
            file_version_id=file.id,
            mapping_version_id=mapping.id,
            ruleset_fingerprint="4" * 64,
            ruleset_manifest={"schema_version": 1},
            status=ValidationRunStatus.COMPLETED,
            total_row_count=len(rows),
            evaluated_row_count=len(rows),
            passed_count=len(rows) - int(with_sources),
            flagged_count=int(with_sources),
            manual_review_count=0,
            parse_failed_count=0,
            completed_at=datetime.now(UTC),
            triggered_by=actor.id,
        )
        db.add(validation)
        await db.flush()
        if with_sources:
            db.add(
                Finding(
                    tenant_id=tenant.id,
                    file_version_id=file.id,
                    row_no=1,
                    kind="invoice_duplicate",
                    severity_impact=0,
                    severity_confidence=0,
                    rule_id="invoice-duplicate",
                    rule_version="v1",
                    validation_run_id=validation.id,
                    rule_kind="invoice_duplicate",
                    evidence_json={
                        "schema_version": 1,
                        "outcome": "flagged",
                        "rule_kind": "invoice_duplicate",
                        "reason_code": "invoice_duplicate",
                        "required_fields": ["invoice_no"],
                        "provenance": {
                            "invoice_no": {
                                "mode": "mapped",
                                "source_columns": ["invoice_no"],
                                "inference_rule_id": None,
                            }
                        },
                        "exemption_id": None,
                        "invoice_no": "N001",
                        "duplicate_of_file_version_id": str(file.id),
                        "duplicate_of_root_file_version_id": str(file.id),
                        "duplicate_of_row_no": 2,
                    },
                )
            )
        await db.commit()
        tenant_id, actor_id, file_id, validation_id = (
            tenant.id,
            actor.id,
            file.id,
            validation.id,
        )

    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        detection_config = await create_detection_config(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=0,
            definition=DetectionProfileDefinition.model_validate(profile_data()),
            change_reason="F8 run integration fixture",
            idempotency_key=f"detect-config-{slug}",
        )
        await db.commit()
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        detection = await run_detection(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key=f"detect-run-{slug}",
        )
        await db.commit()
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        grading_config = await create_grading_config(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=0,
            definition=config(),
            change_reason="F8 run integration fixture",
            idempotency_key=f"grade-config-{slug}",
        )
        await db.commit()
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        candidate_ids = tuple(
            (
                await db.scalars(
                    select(CorrelationFinding.id)
                    .where(CorrelationFinding.detection_run_id == detection.id)
                    .order_by(CorrelationFinding.id)
                )
            ).all()
        )
    f7_requests = tuple(
        F7NotRunManifestRequest(correlation_finding_id=candidate_id)
        for candidate_id in candidate_ids
    )
    if with_sources:
        assert candidate_ids
    else:
        assert not candidate_ids
    assert detection.detection_config_id == detection_config.id
    return SeededRun(
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_version_id=file_id,
        validation_run_id=validation_id,
        detection_run_id=detection.id,
        grading_config_id=grading_config.id,
        f7_requests=f7_requests,
    )


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    seed: SeededRun,
    *,
    key: str,
    fault_hook: run_module.FaultHook | None = None,
) -> GradingRunView:
    return await run_grading(
        session_factory,
        tenant_id=seed.tenant_id,
        actor_id=seed.actor_id,
        file_version_id=seed.file_version_id,
        validation_run_id=seed.validation_run_id,
        detection_run_id=seed.detection_run_id,
        grading_config_id=seed.grading_config_id,
        f7_requests=seed.f7_requests,
        idempotency_key=key,
        fault_hook=fault_hook,
    )


async def test_atomic_run_same_key_replay_and_new_key_alias_do_not_regrade(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = await seed_grading_run_sources(session_factory, slug=f"f8-replay-{uuid.uuid4().hex[:7]}")
    created = await _run(session_factory, seed, key="grading-run-key-0001")
    assert created.reused_existing is False
    assert created.deterministic_item_count == 1
    assert created.correlation_item_count == len(seed.f7_requests)

    with monkeypatch.context() as scoped:
        scoped.setattr(
            run_module,
            "build_grading_manifests",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("rebuilt")),
        )
        replay = await _run(session_factory, seed, key="grading-run-key-0001")
    assert replay.id == created.id and replay.reused_existing is True

    with monkeypatch.context() as scoped:
        scoped.setattr(
            run_module,
            "grade",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("regraded")),
        )
        alias = await _run(session_factory, seed, key="grading-run-key-0002")
    assert alias.id == created.id and alias.reused_existing is True

    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        assert await db.scalar(select(func.count()).select_from(GradingRun)) == 1
        assert await db.scalar(select(func.count()).select_from(GradingRequest)) == 2
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_complete")
            )
            == 1
        )


async def test_zero_item_run_is_valid(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await seed_grading_run_sources(
        session_factory,
        slug=f"f8-zero-{uuid.uuid4().hex[:8]}",
        with_sources=False,
    )
    result = await _run(session_factory, seed, key="grading-zero-key-0001")
    assert result.deterministic_item_count == result.correlation_item_count == 0
    assert result.high_attention_count == result.manual_attention_count == result.cleared_count == 0


async def test_post_commit_interruption_replays_without_failed_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await seed_grading_run_sources(
        session_factory, slug=f"f8-post-commit-{uuid.uuid4().hex[:7]}"
    )

    def interrupt_after_commit(stage: str) -> None:
        if stage == "transaction_committed":
            raise RuntimeError(stage)

    with pytest.raises(GradingInternalError) as interrupted:
        await _run(
            session_factory,
            seed,
            key="grading-post-commit-key",
            fault_hook=interrupt_after_commit,
        )
    assert interrupted.value.code == "GRADING_POST_COMMIT_INTERRUPTED"
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        assert await db.scalar(select(func.count()).select_from(GradingRun)) == 1
        assert await db.scalar(select(func.count()).select_from(GradingRequest)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_complete")
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_failed")
            )
            == 0
        )
    replay = await _run(session_factory, seed, key="grading-post-commit-key")
    assert replay.reused_existing is True


@pytest.mark.slow
@pytest.mark.parametrize(
    ("stage", "committed_before_restart", "reused_after_restart"),
    [
        ("success_audit_written", False, False),
        ("transaction_committed", True, True),
    ],
)
async def test_hard_process_exit_has_exact_commit_boundary_and_fresh_process_replay(
    session_factory: async_sessionmaker[AsyncSession],
    db_url: str,
    stage: str,
    committed_before_restart: bool,
    reused_after_restart: bool,
) -> None:
    seed = await seed_grading_run_sources(
        session_factory, slug=f"f8-kill-{stage[:8]}-{uuid.uuid4().hex[:6]}"
    )
    backend_dir = Path(__file__).parents[2]
    environment = {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ
    }
    environment.update(
        {
            "F8_KILL_ACTOR_ID": str(seed.actor_id),
            "F8_KILL_CONFIG_ID": str(seed.grading_config_id),
            "F8_KILL_DB_URL": db_url,
            "F8_KILL_DETECTION_ID": str(seed.detection_run_id),
            "F8_KILL_F7_IDS": ",".join(
                str(item.correlation_finding_id) for item in seed.f7_requests
            ),
            "F8_KILL_FILE_ID": str(seed.file_version_id),
            "F8_KILL_STAGE": stage,
            "F8_KILL_TENANT_ID": str(seed.tenant_id),
            "F8_KILL_VALIDATION_ID": str(seed.validation_run_id),
            "PYTHONPATH": str(backend_dir),
        }
    )
    worker = """
import asyncio
import os
import uuid
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.asyncio_compat import configure_event_loop_policy
from app.core.grading.run_service import run_grading
from app.core.grading.service_models import F7NotRunManifestRequest
from app.core.tenancy.scope import install_tenant_guard

async def main():
    engine = create_async_engine(os.environ['F8_KILL_DB_URL'], pool_pre_ping=True)
    install_tenant_guard()
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    requests = tuple(
        F7NotRunManifestRequest(correlation_finding_id=uuid.UUID(value))
        for value in os.environ['F8_KILL_F7_IDS'].split(',') if value
    )
    def hard_exit(current):
        if current == os.environ['F8_KILL_STAGE']:
            os._exit(91)
    result = await run_grading(
        factory,
        tenant_id=uuid.UUID(os.environ['F8_KILL_TENANT_ID']),
        actor_id=uuid.UUID(os.environ['F8_KILL_ACTOR_ID']),
        file_version_id=uuid.UUID(os.environ['F8_KILL_FILE_ID']),
        validation_run_id=uuid.UUID(os.environ['F8_KILL_VALIDATION_ID']),
        detection_run_id=uuid.UUID(os.environ['F8_KILL_DETECTION_ID']),
        grading_config_id=uuid.UUID(os.environ['F8_KILL_CONFIG_ID']),
        f7_requests=requests,
        idempotency_key='grading-hard-kill-key',
        fault_hook=hard_exit,
    )
    print(f'reused={str(result.reused_existing).lower()}')
    await engine.dispose()

configure_event_loop_policy()
asyncio.run(main())
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", worker],
        capture_output=True,
        check=False,
        env=environment,
        timeout=45,
    )
    assert process.returncode == 91, process.stderr.decode("utf-8", errors="replace")
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        expected_before_restart = int(committed_before_restart)
        assert (
            await db.scalar(select(func.count()).select_from(GradingRun)) == expected_before_restart
        )
        assert (
            await db.scalar(select(func.count()).select_from(GradingRequest))
            == expected_before_restart
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_complete")
            )
            == expected_before_restart
        )
    restart_environment = environment | {"F8_KILL_STAGE": "never"}
    restart = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", worker],
        capture_output=True,
        check=False,
        env=restart_environment,
        timeout=45,
    )
    assert restart.returncode == 0, restart.stderr.decode("utf-8", errors="replace")
    assert restart.stdout.decode("utf-8").strip() == (f"reused={str(reused_after_restart).lower()}")
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        assert await db.scalar(select(func.count()).select_from(GradingRun)) == 1
        assert await db.scalar(select(func.count()).select_from(GradingRequest)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_complete")
            )
            == 1
        )


@pytest.mark.parametrize(
    "stage",
    [
        "run_written",
        "deterministic_item_written",
        "correlation_item_written",
        "item_row_written",
        "request_ledger_written",
        "success_audit_written",
    ],
)
async def test_fault_rolls_back_every_business_fact_and_same_key_recovers(
    session_factory: async_sessionmaker[AsyncSession], stage: str
) -> None:
    seed = await seed_grading_run_sources(
        session_factory, slug=f"f8-fault-{stage[:8]}-{uuid.uuid4().hex[:5]}"
    )

    def fail_at(current: str) -> None:
        if current == stage:
            raise RuntimeError(stage)

    with pytest.raises(GradingInternalError) as caught:
        await _run(
            session_factory,
            seed,
            key="grading-fault-key-0001",
            fault_hook=fail_at,
        )
    assert caught.value.code == "GRADING_INTERNAL_ROLLBACK"
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        for model in (GradingRun, GradingRequest, GradingItem, GradingItemRow):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_failed")
            )
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "grading.run_complete")
            )
            == 0
        )
    recovered = await _run(session_factory, seed, key="grading-fault-key-0001")
    assert recovered.reused_existing is False


async def test_same_key_conflict_and_file_lock_conflict_are_stable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await seed_grading_run_sources(
        session_factory, slug=f"f8-conflict-{uuid.uuid4().hex[:7]}"
    )
    await _run(session_factory, seed, key="grading-conflict-key")
    with pytest.raises(GradingError) as reused:
        await run_grading(
            session_factory,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            file_version_id=seed.file_version_id,
            validation_run_id=seed.validation_run_id,
            detection_run_id=seed.detection_run_id,
            grading_config_id=uuid.uuid4(),
            f7_requests=seed.f7_requests,
            idempotency_key="grading-conflict-key",
        )
    assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"

    async with session_factory() as blocker:
        bind_tenant(blocker.sync_session, seed.tenant_id)
        async with blocker.begin():
            await lock_tenant_nowait(blocker, seed.tenant_id)
            with pytest.raises(GradingError) as locked:
                await _run(session_factory, seed, key="grading-lock-key-0001")
            assert locked.value.code == "GRADING_LOCK_CONFLICT"


async def test_historical_alias_bypasses_current_config_but_new_identity_is_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await seed_grading_run_sources(session_factory, slug=f"f8-stale-{uuid.uuid4().hex[:8]}")
    created = await _run(session_factory, seed, key="grading-stale-key-0001")
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        changed = config_payload()
        changed["impact_by_rule_kind"]["limit"] = 1
        await create_grading_config(
            db,
            session_factory,
            tenant_id=seed.tenant_id,
            actor_id=seed.actor_id,
            expected_current_version=1,
            definition=changed,
            change_reason="advance current version",
            idempotency_key="grading-config-v2-key",
        )
        await db.commit()
    alias = await _run(session_factory, seed, key="grading-stale-alias-key")
    assert alias.id == created.id and alias.reused_existing is True

    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        row = await db.scalar(
            select(ExpenseRow).where(
                ExpenseRow.file_version_id == seed.file_version_id,
                ExpenseRow.row_no == 1,
            )
        )
        assert row is not None and row.normalized_json is not None
        row.normalized_json = row.normalized_json | {"amount": "601"}
        await db.commit()
    with pytest.raises(GradingError) as stale:
        await _run(session_factory, seed, key="grading-stale-new-key")
    assert stale.value.code == "GRADING_CONFIG_STALE"


async def test_fixture_proves_real_f3_f6_source_sets(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seed = await seed_grading_run_sources(session_factory, slug=f"f8-source-{uuid.uuid4().hex[:8]}")
    async with session_factory() as db:
        bind_tenant(db.sync_session, seed.tenant_id)
        assert await db.scalar(select(func.count()).select_from(Finding)) == 1
        assert await db.scalar(select(func.count()).select_from(CapabilityDeclaration)) == 4
        correlation_count = await db.scalar(select(func.count()).select_from(CorrelationFinding))
        row_count = await db.scalar(select(func.count()).select_from(CorrelationFindingRow))
        assert correlation_count is not None and correlation_count > 0
        assert row_count is not None and row_count > 0
        assert await db.scalar(select(func.count()).select_from(DetectionRun)) == 1
        assert await db.scalar(select(func.count()).select_from(DetectionRequest)) == 1
