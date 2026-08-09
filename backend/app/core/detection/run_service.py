"""Atomic, replay-safe CP-F6.3 detection-run orchestration."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.detection.canonical import canonical_sha256
from app.core.detection.config_service import profile_from_config, require_latest_detection_config
from app.core.detection.engine import run_detection_core, stable_findings
from app.core.detection.errors import DetectionError, DetectionInputError, DetectionInternalError
from app.core.detection.input_snapshot import DetectionInputSnapshot, load_detection_input
from app.core.detection.models import DetectionCoreResult
from app.core.detection.service_models import DetectionRunResult
from app.core.errors import ExpenseGuardError
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import FileVersion
from app.db.models.detection import (
    CorrelationFindingRow,
    DetectionConfig,
    DetectionRequest,
    DetectionRun,
)
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding
from app.db.models.tenancy import AppUser

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
PERSIST_BATCH_SIZE = 250
FaultHook = Callable[[str], None]


@dataclass
class _RunAttempt:
    config_id: uuid.UUID | None = None
    config_fingerprint: str | None = None
    input_fingerprint: str | None = None


async def run_detection(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> DetectionRunResult:
    """Create or replay a complete immutable F6 run."""
    key_hash = _idempotency_key_hash(idempotency_key)
    attempt = _RunAttempt()
    try:
        async with db.begin_nested():
            return await _run_detection(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_version_id,
                key_hash=key_hash,
                attempt=attempt,
                fault_hook=fault_hook,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise DetectionError(
                code="DETECTION_CONFLICT", message="该批次正在执行关联检测，请稍后重试"
            ) from exc
        await _rollback_and_record_run_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise DetectionInternalError() from exc
    except IntegrityError as exc:
        if _integrity_sqlstate(exc) == "23505":
            raise DetectionError(
                code="DETECTION_CONFLICT", message="关联检测并发创建冲突，请重试"
            ) from exc
        await _rollback_and_record_run_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise DetectionInternalError() from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_run_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise DetectionInternalError() from exc


async def _run_detection(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    key_hash: str,
    attempt: _RunAttempt,
    fault_hook: FaultHook | None,
) -> DetectionRunResult:
    await lock_tenant_nowait(db, tenant_id)
    file_version = await _lock_file(db, tenant_id=tenant_id, file_version_id=file_version_id)
    await _require_actor(db, tenant_id=tenant_id, actor_id=actor_id)

    keyed = await db.scalar(
        select(DetectionRequest).where(
            DetectionRequest.tenant_id == tenant_id,
            DetectionRequest.idempotency_key_hash == key_hash,
        )
    )
    if keyed is not None:
        run = await _request_run(db, tenant_id=tenant_id, request=keyed)
        expected_request = _detect_request_fingerprint(
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            config_id=run.detection_config_id,
            config_fingerprint=run.config_fingerprint,
        )
        if (
            keyed.request_fingerprint != expected_request
            or keyed.file_version_id != file_version_id
        ):
            raise DetectionError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他关联检测请求",
            )
        snapshot = await load_detection_input(
            db,
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            file_version=file_version,
        )
        if snapshot.input_fingerprint != run.input_fingerprint:
            raise DetectionError(
                code="DETECTION_INPUT_DRIFT",
                message="批次输入与已完成运行的冻结身份不一致",
            )
        return run_result(run, reused_existing=True)

    config = await require_latest_detection_config(db, tenant_id=tenant_id)
    attempt.config_id = config.id
    attempt.config_fingerprint = config.config_fingerprint
    request_fingerprint = _detect_request_fingerprint(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        config_id=config.id,
        config_fingerprint=config.config_fingerprint,
    )
    existing = await db.scalar(
        select(DetectionRun).where(
            DetectionRun.tenant_id == tenant_id,
            DetectionRun.file_version_id == file_version_id,
            DetectionRun.config_fingerprint == config.config_fingerprint,
        )
    )
    snapshot = await load_detection_input(
        db,
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        file_version=file_version,
    )
    attempt.input_fingerprint = snapshot.input_fingerprint
    if existing is not None:
        if existing.input_fingerprint != snapshot.input_fingerprint:
            raise DetectionError(
                code="DETECTION_INPUT_DRIFT",
                message="批次输入与已完成运行的冻结身份不一致",
            )
        await _write_request(
            db,
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            run=existing,
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
        _fault(fault_hook, "request_ledger_written")
        return run_result(existing, reused_existing=True)

    profile = profile_from_config(config)
    core_result = run_detection_core(profile, snapshot.batch)
    if core_result.profile_fingerprint != config.config_fingerprint:
        raise DetectionInternalError(
            code="DETECTION_CONFIG_CORRUPT", message="detector profile 身份不一致"
        )
    return await _persist_new_run(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_version_id=file_version_id,
        config=config,
        snapshot=snapshot,
        result=core_result,
        key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        fault_hook=fault_hook,
    )


async def _persist_new_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    config: DetectionConfig,
    snapshot: DetectionInputSnapshot,
    result: DetectionCoreResult,
    key_hash: str,
    request_fingerprint: str,
    fault_hook: FaultHook | None,
) -> DetectionRunResult:
    findings = stable_findings(finding for output in result.outputs for finding in output.findings)
    if sum(output.declaration.finding_count for output in result.outputs) != len(findings):
        raise DetectionInternalError(
            code="DETECTION_OUTPUT_INVALID", message="detector finding 计数不一致"
        )
    completed_at = cast(datetime, await db.scalar(select(func.clock_timestamp())))
    run = DetectionRun(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        detection_config_id=config.id,
        config_version=config.version,
        config_fingerprint=config.config_fingerprint,
        algorithm_bundle_version=config.algorithm_bundle_version,
        input_fingerprint=snapshot.input_fingerprint,
        run_fingerprint=canonical_sha256(
            {
                "algorithm_bundle_version": config.algorithm_bundle_version,
                "config_fingerprint": config.config_fingerprint,
                "file_version_id": str(file_version_id),
                "input_fingerprint": snapshot.input_fingerprint,
                "schema_version": 1,
                "tenant_id": str(tenant_id),
            }
        ),
        source_row_count=snapshot.source_row_count,
        parsed_row_count=snapshot.parsed_row_count,
        error_row_count=snapshot.error_row_count,
        finding_count=len(findings),
        created_by=actor_id,
        completed_at=completed_at,
    )
    db.add(run)
    await db.flush()
    _fault(fault_hook, "run_written")
    declarations: list[CapabilityDeclaration] = []
    for output in result.outputs:
        declaration = output.declaration
        declarations.append(
            CapabilityDeclaration(
                tenant_id=tenant_id,
                file_version_id=file_version_id,
                detection_run_id=run.id,
                config_fingerprint=config.config_fingerprint,
                detector=declaration.detector.value,
                detector_version=declaration.detector_version,
                status=declaration.status.value,
                reason_code=declaration.reason_code.value,
                reason=declaration.reason_snapshot,
                details_json=declaration.details.model_dump(mode="json"),
                finding_count=declaration.finding_count,
            )
        )
    db.add_all(declarations)
    await db.flush()
    _fault(fault_hook, "declaration_written")

    persisted_findings: list[tuple[CorrelationFinding, tuple[int, ...]]] = []
    for draft in findings:
        finding = CorrelationFinding(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            detection_run_id=run.id,
            detector=draft.detector.value,
            detector_version=draft.detector_version,
            finding_key=draft.finding_key,
            evidence_schema_version=draft.evidence.schema_version,
            evidence_json=draft.evidence.model_dump(mode="json"),
            reasoning_snapshot=draft.reasoning_snapshot,
            severity_impact=0,
            severity_confidence=0,
        )
        persisted_findings.append((finding, draft.participating_row_nos))

    for start in range(0, len(persisted_findings), PERSIST_BATCH_SIZE):
        batch = persisted_findings[start : start + PERSIST_BATCH_SIZE]
        db.add_all(finding for finding, _row_nos in batch)
        await db.flush()
        _fault(fault_hook, "finding_written")

    row_links: list[CorrelationFindingRow] = []
    for finding, row_nos in persisted_findings:
        for ordinal, row_no in enumerate(row_nos, start=1):
            row_links.append(
                CorrelationFindingRow(
                    tenant_id=tenant_id,
                    finding_id=finding.id,
                    detection_run_id=run.id,
                    file_version_id=file_version_id,
                    row_no=row_no,
                    ordinal=ordinal,
                )
            )

    for start in range(0, len(row_links), PERSIST_BATCH_SIZE):
        db.add_all(row_links[start : start + PERSIST_BATCH_SIZE])
        await db.flush()
        _fault(fault_hook, "row_link_written")
    await _write_request(
        db,
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        run=run,
        key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    _fault(fault_hook, "request_ledger_written")
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="detection.run_complete",
        target_type="detection_run",
        target_id=str(run.id),
        payload={
            "config_fingerprint": run.config_fingerprint,
            "config_version": run.config_version,
            "finding_count": run.finding_count,
            "input_fingerprint": run.input_fingerprint,
            "run_fingerprint": run.run_fingerprint,
            "source_row_count": run.source_row_count,
        },
    )
    _fault(fault_hook, "success_audit_written")
    return run_result(run, reused_existing=False)


async def _write_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    run: DetectionRun,
    key_hash: str,
    request_fingerprint: str,
) -> None:
    db.add(
        DetectionRequest(
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            detection_run_id=run.id,
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
    )
    await db.flush()


async def _lock_file(
    db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID
) -> FileVersion:
    batch = await db.scalar(
        select(FileVersion)
        .where(FileVersion.id == file_version_id, FileVersion.tenant_id == tenant_id)
        .with_for_update(nowait=True)
    )
    if batch is None:
        from app.core.detection.errors import DetectionNotFoundError

        raise DetectionNotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    return batch


async def _require_actor(db: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    actor = await db.scalar(
        select(AppUser).where(
            AppUser.id == actor_id,
            AppUser.tenant_id == tenant_id,
            AppUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise DetectionError(code="DETECTION_ACTOR_INVALID", message="操作用户无效")


async def _request_run(
    db: AsyncSession, *, tenant_id: uuid.UUID, request: DetectionRequest
) -> DetectionRun:
    run = await db.scalar(
        select(DetectionRun).where(
            DetectionRun.id == request.detection_run_id,
            DetectionRun.tenant_id == tenant_id,
            DetectionRun.file_version_id == request.file_version_id,
        )
    )
    if run is None:
        raise DetectionInternalError(
            code="DETECTION_LEDGER_CORRUPT", message="关联检测请求账本无效"
        )
    return run


def _detect_request_fingerprint(
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    config_id: uuid.UUID,
    config_fingerprint: str,
) -> str:
    return canonical_sha256(
        {
            "config_fingerprint": config_fingerprint,
            "config_id": str(config_id),
            "file_version_id": str(file_version_id),
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )


def run_result(run: DetectionRun, *, reused_existing: bool) -> DetectionRunResult:
    return DetectionRunResult(
        id=run.id,
        tenant_id=run.tenant_id,
        file_version_id=run.file_version_id,
        detection_config_id=run.detection_config_id,
        config_version=run.config_version,
        config_fingerprint=run.config_fingerprint,
        algorithm_bundle_version=run.algorithm_bundle_version,
        input_fingerprint=run.input_fingerprint,
        run_fingerprint=run.run_fingerprint,
        source_row_count=run.source_row_count,
        parsed_row_count=run.parsed_row_count,
        error_row_count=run.error_row_count,
        finding_count=run.finding_count,
        created_by=run.created_by,
        created_at=run.created_at,
        completed_at=run.completed_at,
        reused_existing=reused_existing,
    )


async def _rollback_and_record_run_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    attempt: _RunAttempt,
) -> None:
    await db.rollback()
    async with session_factory() as audit_db:
        bind_tenant(audit_db.sync_session, tenant_id)
        actor = await audit_db.scalar(
            select(AppUser).where(AppUser.id == actor_id, AppUser.tenant_id == tenant_id)
        )
        await write_audit(
            audit_db,
            tenant_id=tenant_id,
            actor_id=actor.id if actor is not None else None,
            action="detection.run_failed",
            target_type="file_version",
            target_id=str(file_version_id),
            payload={
                "config_fingerprint": attempt.config_fingerprint,
                "config_id": str(attempt.config_id) if attempt.config_id is not None else None,
                "file_version_id": str(file_version_id),
                "input_fingerprint": attempt.input_fingerprint,
                "reason_code": "INTERNAL_ERROR",
            },
        )
        await audit_db.commit()


def _idempotency_key_hash(value: str) -> str:
    if not 8 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise DetectionInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须为 8 到 128 个可见 ASCII 字符",
        )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None


def _integrity_sqlstate(exc: IntegrityError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
