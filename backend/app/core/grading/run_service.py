"""Atomic and replay-safe CP-F8.3 grading-run orchestration."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError
from app.core.grading.canonical import canonical_bytes, grading_input_fingerprint
from app.core.grading.config_service import config_from_record, require_current_grading_config
from app.core.grading.errors import (
    GradingError,
    GradingInputError,
    GradingInternalError,
    GradingNotFoundError,
)
from app.core.grading.grader import grade
from app.core.grading.manifest_builder import (
    F3_MAX_BYTES,
    F6_MAX_BYTES,
    F7_MAX_BYTES,
    build_grading_manifests,
)
from app.core.grading.models import (
    CorrelationGradingSource,
    DeterministicGradingSource,
    GradingCoreResult,
    GradingInput,
    GradingItemResult,
    SourceKind,
)
from app.core.grading.service_models import (
    F3Manifest,
    F6CandidateManifestItem,
    F6CapabilityManifestItem,
    F6Manifest,
    F7Manifest,
    F7ManifestRequest,
    F7NotRunManifestRequest,
    GradingManifestBundle,
    GradingRunView,
)
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import FileVersion
from app.db.models.grading import (
    GradingConfig,
    GradingItem,
    GradingItemRow,
    GradingRequest,
    GradingRun,
    GradingRunStatus,
)
from app.db.models.tenancy import AppUser, Tenant

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
UNIQUE_VIOLATION_SQLSTATE = "23505"
PERSIST_BATCH_SIZE = 250
FaultHook = Callable[[str], None]


@dataclass
class _RunAttempt:
    grading_run_id: uuid.UUID | None = None
    grading_config_id: uuid.UUID | None = None
    config_fingerprint: str | None = None
    input_fingerprint: str | None = None


async def run_grading(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    grading_config_id: uuid.UUID,
    f7_requests: Sequence[F7ManifestRequest],
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> GradingRunView:
    """Create, alias, or replay one immutable completed F8 run.

    The service owns the transaction so PostgreSQL deferred snapshot checks run
    before a successful response can escape this boundary.
    """

    key_hash = _idempotency_key_hash(idempotency_key)
    request_fingerprint = _request_fingerprint(
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_version_id=file_version_id,
        validation_run_id=validation_run_id,
        detection_run_id=detection_run_id,
        grading_config_id=grading_config_id,
        f7_requests=f7_requests,
    )
    attempt = _RunAttempt(grading_config_id=grading_config_id)
    committed = False
    try:
        async with session_factory() as db:
            bind_tenant(db.sync_session, tenant_id)
            async with db.begin():
                result = await _run_grading(
                    db,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    file_version_id=file_version_id,
                    validation_run_id=validation_run_id,
                    detection_run_id=detection_run_id,
                    grading_config_id=grading_config_id,
                    f7_requests=f7_requests,
                    key_hash=key_hash,
                    request_fingerprint=request_fingerprint,
                    attempt=attempt,
                    fault_hook=fault_hook,
                )
        committed = True
        _fault(fault_hook, "transaction_committed")
        return result
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise GradingError(
                code="GRADING_LOCK_CONFLICT",
                message="该批次正在执行二维分级，请稍后重试",
            ) from exc
        await _write_safe_failure_audit(
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise GradingInternalError(code="GRADING_INTERNAL_ROLLBACK") from exc
    except IntegrityError as exc:
        if _sqlstate(exc) == UNIQUE_VIOLATION_SQLSTATE:
            raise GradingError(
                code="GRADING_CONFLICT",
                message="二维分级并发创建冲突，请重试",
            ) from exc
        await _write_safe_failure_audit(
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise GradingInternalError(code="GRADING_INTERNAL_ROLLBACK") from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        if committed:
            raise GradingInternalError(code="GRADING_POST_COMMIT_INTERRUPTED") from exc
        await _write_safe_failure_audit(
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise GradingInternalError(code="GRADING_INTERNAL_ROLLBACK") from exc


async def _run_grading(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    grading_config_id: uuid.UUID,
    f7_requests: Sequence[F7ManifestRequest],
    key_hash: str,
    request_fingerprint: str,
    attempt: _RunAttempt,
    fault_hook: FaultHook | None,
) -> GradingRunView:
    await lock_tenant_nowait(db, tenant_id)
    await _lock_file(db, tenant_id=tenant_id, file_version_id=file_version_id)
    await _require_actor(db, tenant_id=tenant_id, actor_id=actor_id)

    keyed = await db.scalar(
        select(GradingRequest).where(
            GradingRequest.tenant_id == tenant_id,
            GradingRequest.idempotency_key_hash == key_hash,
        )
    )
    if keyed is not None:
        run = await _request_run(db, tenant_id=tenant_id, request=keyed)
        if (
            keyed.request_fingerprint != request_fingerprint
            or keyed.file_version_id != file_version_id
            or keyed.input_fingerprint != run.input_fingerprint
            or run.validation_run_id != validation_run_id
            or run.detection_run_id != detection_run_id
            or run.grading_config_id != grading_config_id
        ):
            raise GradingError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他二维分级请求",
            )
        _validate_stored_run(run)
        return _run_view(run, reused_existing=True)

    config = await db.scalar(
        select(GradingConfig).where(
            GradingConfig.id == grading_config_id,
            GradingConfig.tenant_id == tenant_id,
        )
    )
    if config is None:
        raise GradingNotFoundError(
            code="GRADING_CONFIG_MISSING",
            message="二维分级配置不存在",
        )
    definition = config_from_record(config)
    attempt.config_fingerprint = config.config_fingerprint
    bundle = await build_grading_manifests(
        db,
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        validation_run_id=validation_run_id,
        detection_run_id=detection_run_id,
        f7_requests=f7_requests,
    )
    grading_input = GradingInput(
        config=definition,
        f3_manifest_fingerprint=bundle.f3_manifest_fingerprint,
        f6_manifest_fingerprint=bundle.f6_manifest_fingerprint,
        f7_manifest_fingerprint=bundle.f7_manifest_fingerprint,
        deterministic_sources=bundle.deterministic_sources,
        correlation_sources=bundle.correlation_sources,
    )
    input_fingerprint = grading_input_fingerprint(grading_input)
    attempt.input_fingerprint = input_fingerprint

    existing = await db.scalar(
        select(GradingRun).where(
            GradingRun.tenant_id == tenant_id,
            GradingRun.file_version_id == file_version_id,
            GradingRun.validation_run_id == validation_run_id,
            GradingRun.detection_run_id == detection_run_id,
            GradingRun.grading_config_id == grading_config_id,
            GradingRun.input_fingerprint == input_fingerprint,
        )
    )
    if existing is not None:
        _validate_run_against_bundle(existing, bundle)
        await _write_request(
            db,
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            run=existing,
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
        _fault(fault_hook, "request_ledger_written")
        return _run_view(existing, reused_existing=True)

    current = await require_current_grading_config(db, tenant_id=tenant_id)
    if current.id != config.id or current.config_fingerprint != config.config_fingerprint:
        raise GradingError(
            code="GRADING_CONFIG_STALE",
            message="二维分级配置已更新，请使用当前版本",
        )
    core_result = grade(grading_input)
    _validate_core_result(core_result, grading_input)
    return await _persist_new_run(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_version_id=file_version_id,
        validation_run_id=validation_run_id,
        detection_run_id=detection_run_id,
        config=config,
        bundle=bundle,
        result=core_result,
        key_hash=key_hash,
        request_fingerprint=request_fingerprint,
        attempt=attempt,
        fault_hook=fault_hook,
    )


async def _persist_new_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    config: GradingConfig,
    bundle: GradingManifestBundle,
    result: GradingCoreResult,
    key_hash: str,
    request_fingerprint: str,
    attempt: _RunAttempt,
    fault_hook: FaultHook | None,
) -> GradingRunView:
    completed_at = cast(datetime, await db.scalar(select(func.clock_timestamp())))
    run = GradingRun(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        validation_run_id=validation_run_id,
        detection_run_id=detection_run_id,
        grading_config_id=config.id,
        created_by=actor_id,
        config_version=config.version,
        config_schema_version=config.schema_version,
        config_fingerprint=config.config_fingerprint,
        algorithm_version=config.algorithm_version,
        f3_manifest_json=bundle.f3_manifest.model_dump(mode="json"),
        f3_manifest_fingerprint=bundle.f3_manifest_fingerprint,
        f6_manifest_json=bundle.f6_manifest.model_dump(mode="json"),
        f6_manifest_fingerprint=bundle.f6_manifest_fingerprint,
        f7_manifest_json=bundle.f7_manifest.model_dump(mode="json"),
        f7_manifest_fingerprint=bundle.f7_manifest_fingerprint,
        input_fingerprint=result.input_fingerprint,
        status=GradingRunStatus.COMPLETED,
        deterministic_item_count=result.deterministic_item_count,
        correlation_item_count=result.correlation_item_count,
        high_attention_count=result.high_attention_count,
        manual_attention_count=result.manual_attention_count,
        cleared_count=result.cleared_count,
        completed_at=completed_at,
    )
    db.add(run)
    await db.flush()
    attempt.grading_run_id = run.id
    _fault(fault_hook, "run_written")

    deterministic_by_id = {source.finding_id: source for source in bundle.deterministic_sources}
    correlation_by_id = {
        source.correlation_finding_id: source for source in bundle.correlation_sources
    }
    f6_by_id = {item.correlation_finding_id: item for item in bundle.f6_manifest.candidates}
    capability_by_detector = {item.detector: item for item in bundle.f6_manifest.capabilities}
    f7_by_id = {item.correlation_finding_id: item for item in bundle.f7_manifest.entries}
    deterministic_items: list[tuple[GradingItem, DeterministicGradingSource]] = []
    correlation_items: list[tuple[GradingItem, CorrelationGradingSource]] = []
    for item_result in result.items:
        source: DeterministicGradingSource | CorrelationGradingSource
        if item_result.source_kind is SourceKind.DETERMINISTIC:
            source = deterministic_by_id[item_result.source_id]
            item = _deterministic_item(
                run=run,
                source=source,
                result=item_result,
            )
            deterministic_items.append((item, source))
        else:
            source = correlation_by_id[item_result.source_id]
            item = _correlation_item(
                run=run,
                source=source,
                result=item_result,
                candidate=f6_by_id[source.correlation_finding_id],
                capability=capability_by_detector[source.detector],
                investigation=f7_by_id[source.correlation_finding_id],
            )
            correlation_items.append((item, source))

    for start in range(0, len(deterministic_items), PERSIST_BATCH_SIZE):
        deterministic_batch = deterministic_items[start : start + PERSIST_BATCH_SIZE]
        db.add_all(item for item, _source in deterministic_batch)
        await db.flush()
        _fault(fault_hook, "deterministic_item_written")
    for start in range(0, len(correlation_items), PERSIST_BATCH_SIZE):
        correlation_batch = correlation_items[start : start + PERSIST_BATCH_SIZE]
        db.add_all(item for item, _source in correlation_batch)
        await db.flush()
        _fault(fault_hook, "correlation_item_written")

    item_rows: list[GradingItemRow] = []
    for item, deterministic_source in deterministic_items:
        item_rows.append(
            GradingItemRow(
                tenant_id=tenant_id,
                grading_run_id=run.id,
                grading_item_id=item.id,
                file_version_id=file_version_id,
                row_no=deterministic_source.row.row_no,
                ordinal=1,
                source_row_fingerprint=deterministic_source.row.source_row_fingerprint,
            )
        )
    for item, correlation_source in correlation_items:
        for ordinal, participant in enumerate(correlation_source.participating_rows, start=1):
            item_rows.append(
                GradingItemRow(
                    tenant_id=tenant_id,
                    grading_run_id=run.id,
                    grading_item_id=item.id,
                    file_version_id=file_version_id,
                    row_no=participant.row_no,
                    ordinal=ordinal,
                    source_row_fingerprint=participant.source_row_fingerprint,
                )
            )
    for start in range(0, len(item_rows), PERSIST_BATCH_SIZE):
        db.add_all(item_rows[start : start + PERSIST_BATCH_SIZE])
        await db.flush()
        _fault(fault_hook, "item_row_written")

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
        action="grading.run_complete",
        target_type="grading_run",
        target_id=str(run.id),
        payload={
            "config_fingerprint": run.config_fingerprint,
            "correlation_item_count": run.correlation_item_count,
            "detection_run_id": str(run.detection_run_id),
            "deterministic_item_count": run.deterministic_item_count,
            "input_fingerprint": run.input_fingerprint,
            "validation_run_id": str(run.validation_run_id),
        },
    )
    _fault(fault_hook, "success_audit_written")
    return _run_view(run, reused_existing=False)


def _deterministic_item(
    *,
    run: GradingRun,
    source: DeterministicGradingSource,
    result: GradingItemResult,
) -> GradingItem:
    return GradingItem(
        tenant_id=run.tenant_id,
        grading_run_id=run.id,
        file_version_id=run.file_version_id,
        validation_run_id=run.validation_run_id,
        detection_run_id=run.detection_run_id,
        source_kind=SourceKind.DETERMINISTIC.value,
        finding_id=source.finding_id,
        correlation_finding_id=None,
        capability_declaration_id=None,
        investigation_run_id=None,
        investigation_result_id=None,
        rule_kind=source.rule_kind.value,
        f3_outcome=source.outcome.value,
        f3_evidence_fingerprint=source.evidence_fingerprint,
        detector=None,
        f6_detector_version=None,
        f6_finding_key=None,
        f6_config_fingerprint=None,
        f6_capability_status=None,
        f6_evidence_fingerprint=None,
        f7_outcome=None,
        f7_evidence_sufficient=None,
        f7_input_fingerprint=None,
        f7_config_fingerprint=None,
        f7_result_fingerprint=None,
        f7_not_run_reason_code=None,
        severity_impact=int(result.severity_impact),
        severity_confidence=int(result.severity_confidence),
        disposition=result.disposition.value,
        reason_codes_json=[code.value for code in result.reason_codes],
        evidence_snapshot=result.evidence_snapshot.model_dump(mode="json"),
        item_fingerprint=result.item_fingerprint,
        first_row_no=result.first_row_no,
    )


def _correlation_item(
    *,
    run: GradingRun,
    source: CorrelationGradingSource,
    result: GradingItemResult,
    candidate: F6CandidateManifestItem,
    capability: F6CapabilityManifestItem,
    investigation: F7ManifestRequest,
) -> GradingItem:
    if isinstance(investigation, F7NotRunManifestRequest):
        f7_outcome = "not_run"
        f7_evidence_sufficient = None
        investigation_run_id = None
        investigation_result_id = None
        f7_input_fingerprint = None
        f7_config_fingerprint = None
        f7_result_fingerprint = None
        f7_not_run_reason_code = investigation.reason_code
    else:
        f7_outcome = investigation.outcome.value
        f7_evidence_sufficient = investigation.evidence_sufficient
        investigation_run_id = investigation.investigation_run_id
        investigation_result_id = investigation.investigation_result_id
        f7_input_fingerprint = investigation.input_fingerprint
        f7_config_fingerprint = investigation.config_fingerprint
        f7_result_fingerprint = investigation.result_fingerprint
        f7_not_run_reason_code = None
    return GradingItem(
        tenant_id=run.tenant_id,
        grading_run_id=run.id,
        file_version_id=run.file_version_id,
        validation_run_id=run.validation_run_id,
        detection_run_id=run.detection_run_id,
        source_kind=SourceKind.CORRELATION.value,
        finding_id=None,
        correlation_finding_id=source.correlation_finding_id,
        capability_declaration_id=capability.declaration_id,
        investigation_run_id=investigation_run_id,
        investigation_result_id=investigation_result_id,
        rule_kind=None,
        f3_outcome=None,
        f3_evidence_fingerprint=None,
        detector=source.detector.value,
        f6_detector_version=candidate.detector_version,
        f6_finding_key=candidate.finding_key,
        f6_config_fingerprint=capability.config_fingerprint,
        f6_capability_status=capability.status.value,
        f6_evidence_fingerprint=candidate.evidence_fingerprint,
        f7_outcome=f7_outcome,
        f7_evidence_sufficient=f7_evidence_sufficient,
        f7_input_fingerprint=f7_input_fingerprint,
        f7_config_fingerprint=f7_config_fingerprint,
        f7_result_fingerprint=f7_result_fingerprint,
        f7_not_run_reason_code=f7_not_run_reason_code,
        severity_impact=int(result.severity_impact),
        severity_confidence=int(result.severity_confidence),
        disposition=result.disposition.value,
        reason_codes_json=[code.value for code in result.reason_codes],
        evidence_snapshot=result.evidence_snapshot.model_dump(mode="json"),
        item_fingerprint=result.item_fingerprint,
        first_row_no=result.first_row_no,
    )


async def _write_request(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    run: GradingRun,
    key_hash: str,
    request_fingerprint: str,
) -> None:
    db.add(
        GradingRequest(
            tenant_id=tenant_id,
            grading_run_id=run.id,
            file_version_id=file_version_id,
            input_fingerprint=run.input_fingerprint,
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
    )
    await db.flush()


async def _lock_file(db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID) -> None:
    file_id = await db.scalar(
        select(FileVersion.id)
        .where(FileVersion.id == file_version_id, FileVersion.tenant_id == tenant_id)
        .with_for_update(nowait=True)
    )
    if file_id is None:
        raise GradingNotFoundError(code="GRADING_FILE_NOT_FOUND", message="批次不存在")


async def _require_actor(db: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    actor = await db.scalar(
        select(AppUser.id).where(
            AppUser.id == actor_id,
            AppUser.tenant_id == tenant_id,
            AppUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise GradingError(code="GRADING_ACTOR_INVALID", message="操作用户无效")


async def _request_run(
    db: AsyncSession, *, tenant_id: uuid.UUID, request: GradingRequest
) -> GradingRun:
    run = await db.scalar(
        select(GradingRun).where(
            GradingRun.id == request.grading_run_id,
            GradingRun.tenant_id == tenant_id,
            GradingRun.file_version_id == request.file_version_id,
        )
    )
    if run is None:
        raise GradingInternalError(
            code="GRADING_LEDGER_CORRUPT",
            message="二维分级请求账本无效",
        )
    return run


def _validate_stored_run(run: GradingRun) -> None:
    if run.status != GradingRunStatus.COMPLETED or run.completed_at is None:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级快照无效")
    try:
        f3 = F3Manifest.model_validate(run.f3_manifest_json)
        f6 = F6Manifest.model_validate(run.f6_manifest_json)
        f7 = F7Manifest.model_validate(run.f7_manifest_json)
    except ValidationError as exc:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级快照无效") from exc
    if (
        _plain_fingerprint(f3, F3_MAX_BYTES) != run.f3_manifest_fingerprint
        or _plain_fingerprint(f6, F6_MAX_BYTES) != run.f6_manifest_fingerprint
        or _plain_fingerprint(f7, F7_MAX_BYTES) != run.f7_manifest_fingerprint
        or f3.validation_run_id != run.validation_run_id
        or f6.detection_run_id != run.detection_run_id
        or f7.detection_run_id != run.detection_run_id
        or f3.file_version_id != run.file_version_id
        or f6.file_version_id != run.file_version_id
        or f7.file_version_id != run.file_version_id
        or f3.tenant_id != run.tenant_id
        or f6.tenant_id != run.tenant_id
        or f7.tenant_id != run.tenant_id
    ):
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级快照无效")


def _validate_run_against_bundle(run: GradingRun, bundle: GradingManifestBundle) -> None:
    _validate_stored_run(run)
    if (
        run.f3_manifest_fingerprint != bundle.f3_manifest_fingerprint
        or run.f6_manifest_fingerprint != bundle.f6_manifest_fingerprint
        or run.f7_manifest_fingerprint != bundle.f7_manifest_fingerprint
        or run.f3_manifest_json != bundle.f3_manifest.model_dump(mode="json")
        or run.f6_manifest_json != bundle.f6_manifest.model_dump(mode="json")
        or run.f7_manifest_json != bundle.f7_manifest.model_dump(mode="json")
    ):
        raise GradingInputError(
            code="GRADING_INPUT_DRIFT",
            message="二维分级输入与历史运行不一致",
        )


def _validate_core_result(result: GradingCoreResult, grading_input: GradingInput) -> None:
    expected_ids = {
        *(source.source_id for source in grading_input.deterministic_sources),
        *(source.source_id for source in grading_input.correlation_sources),
    }
    actual_ids = {item.source_id for item in result.items}
    if (
        result.input_fingerprint != grading_input_fingerprint(grading_input)
        or expected_ids != actual_ids
        or len(actual_ids) != len(result.items)
        or result.deterministic_item_count != len(grading_input.deterministic_sources)
        or result.correlation_item_count != len(grading_input.correlation_sources)
    ):
        raise GradingInternalError(
            code="GRADING_OUTPUT_INVALID",
            message="二维分级输出集合无效",
        )


def _request_fingerprint(
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    grading_config_id: uuid.UUID,
    f7_requests: Sequence[F7ManifestRequest],
) -> str:
    ordered = tuple(sorted(f7_requests, key=lambda item: str(item.correlation_finding_id)))
    payload = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "file_version_id": file_version_id,
        "validation_run_id": validation_run_id,
        "detection_run_id": detection_run_id,
        "grading_config_id": grading_config_id,
        "f7_requests": ordered,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _idempotency_key_hash(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 128
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise GradingInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须为 8 到 128 个可见 ASCII 字符",
        )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plain_fingerprint(value: object, maximum: int) -> str:
    encoded = canonical_bytes(value)
    if len(encoded) > maximum:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级快照无效")
    return hashlib.sha256(encoded).hexdigest()


async def _write_safe_failure_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    attempt: _RunAttempt,
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        async with db.begin():
            tenant_exists = await db.scalar(select(Tenant.id).where(Tenant.id == tenant_id))
            if tenant_exists is None:
                return
            actor = await db.scalar(
                select(AppUser.id).where(
                    AppUser.id == actor_id,
                    AppUser.tenant_id == tenant_id,
                )
            )
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor,
                action="grading.run_failed",
                target_type="file_version",
                target_id=str(file_version_id),
                payload={
                    "config_fingerprint": attempt.config_fingerprint,
                    "grading_config_id": str(attempt.grading_config_id),
                    "grading_run_id": (
                        str(attempt.grading_run_id) if attempt.grading_run_id is not None else None
                    ),
                    "input_fingerprint": attempt.input_fingerprint,
                    "reason_code": "INTERNAL_ERROR",
                },
            )


def _run_view(run: GradingRun, *, reused_existing: bool) -> GradingRunView:
    if run.completed_at is None:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级快照无效")
    return GradingRunView(
        id=run.id,
        tenant_id=run.tenant_id,
        file_version_id=run.file_version_id,
        validation_run_id=run.validation_run_id,
        detection_run_id=run.detection_run_id,
        grading_config_id=run.grading_config_id,
        created_by=run.created_by,
        config_version=run.config_version,
        config_fingerprint=run.config_fingerprint,
        algorithm_version=run.algorithm_version,
        f3_manifest_fingerprint=run.f3_manifest_fingerprint,
        f6_manifest_fingerprint=run.f6_manifest_fingerprint,
        f7_manifest_fingerprint=run.f7_manifest_fingerprint,
        input_fingerprint=run.input_fingerprint,
        deterministic_item_count=run.deterministic_item_count,
        correlation_item_count=run.correlation_item_count,
        high_attention_count=run.high_attention_count,
        manual_attention_count=run.manual_attention_count,
        cleared_count=run.cleared_count,
        created_at=run.created_at,
        completed_at=run.completed_at,
        reused_existing=reused_existing,
    )


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _sqlstate(exc: IntegrityError | OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
