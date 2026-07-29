"""Atomic F5 sampling-plan creation and legacy-plan idempotency."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError
from app.core.policies.canonical import canonical_sha256
from app.core.reviews.config_service import require_latest_sampling_config
from app.core.reviews.errors import (
    ReviewError,
    ReviewInputError,
    ReviewInternalError,
    ReviewNotFoundError,
)
from app.core.reviews.models import (
    SamplingConfigParameters,
    SamplingPlanResult,
    SamplingSelection,
)
from app.core.reviews.sampling import (
    generate_seed_hex,
    select_sampling_rows,
    verify_sampling_selection,
)
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow, FileVersion, RowResult
from app.db.models.findings import (
    ReviewPlanRequest,
    ReviewSamplingConfig,
    ReviewSamplingPlan,
    SamplingAudit,
)
from app.db.models.reports import ReportRun, ReportRunStatus
from app.db.models.tenancy import AppUser

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
FaultHook = Callable[[str], None]


async def create_plan_for_new_report(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report: ReportRun,
    config: ReviewSamplingConfig,
    fault_hook: FaultHook | None = None,
) -> SamplingPlanResult:
    """Create plan/sample/audit inside the caller's uncommitted report transaction."""
    if report.tenant_id != tenant_id:
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    existing = await _find_plan(db, tenant_id=tenant_id, report_run_id=report.id)
    if existing is not None:
        return await _plan_result(db, existing, reused_existing=True)
    return await _create_plan(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        report=report,
        config=config,
        fault_hook=fault_hook,
    )


async def create_legacy_sampling_plan(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report_run_id: uuid.UUID,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> SamplingPlanResult:
    try:
        async with db.begin_nested():
            return await _create_legacy_sampling_plan(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_run_id=report_run_id,
                idempotency_key=idempotency_key,
                fault_hook=fault_hook,
            )
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_plan_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_run_id=report_run_id,
        )
        raise ReviewInternalError(
            code="REVIEW_PLAN_FAILED",
            message="抽样计划创建暂时不可用",
        ) from exc


async def get_sampling_plan(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    report_run_id: uuid.UUID,
) -> SamplingPlanResult | None:
    """Read a completed report's frozen plan without creating one."""
    report = await db.scalar(
        select(ReportRun).where(
            ReportRun.id == report_run_id,
            ReportRun.tenant_id == tenant_id,
            ReportRun.status == ReportRunStatus.COMPLETED,
        )
    )
    if report is None:
        raise ReviewNotFoundError(code="REPORT_NOT_FOUND", message="报告不存在")
    plan = await _find_plan(db, tenant_id=tenant_id, report_run_id=report_run_id)
    return None if plan is None else await _plan_result(db, plan, reused_existing=True)


async def _create_legacy_sampling_plan(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report_run_id: uuid.UUID,
    idempotency_key: str,
    fault_hook: FaultHook | None,
) -> SamplingPlanResult:
    """Explicitly create or reuse a plan for a pre-F5 completed report."""
    if not 8 <= len(idempotency_key) <= 128:
        raise ReviewInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 长度必须为 8 到 128 个字符",
        )
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    request_fingerprint = canonical_sha256(
        {
            "report_run_id": str(report_run_id),
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )
    try:
        await lock_tenant_nowait(db, tenant_id)
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise ReviewError(code="REVIEW_CONFLICT", message="该租户正在执行复核变更") from exc
        raise

    keyed = await db.scalar(
        select(ReviewPlanRequest).where(
            ReviewPlanRequest.tenant_id == tenant_id,
            ReviewPlanRequest.idempotency_key_hash == key_hash,
        )
    )
    if keyed is not None:
        if keyed.request_fingerprint != request_fingerprint:
            raise ReviewError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他抽样计划请求",
            )
        plan = await db.get(ReviewSamplingPlan, keyed.sampling_plan_id)
        if plan is None:
            raise RuntimeError("plan request ledger references a missing plan")
        return await _plan_result(db, plan, reused_existing=True)

    report = await db.scalar(
        select(ReportRun).where(
            ReportRun.id == report_run_id,
            ReportRun.tenant_id == tenant_id,
        )
    )
    if report is None:
        raise ReviewNotFoundError(code="REPORT_NOT_FOUND", message="报告不存在")
    await _lock_file(db, tenant_id=tenant_id, file_version_id=report.file_version_id)
    report = await db.scalar(
        select(ReportRun)
        .where(ReportRun.id == report_run_id, ReportRun.tenant_id == tenant_id)
        .with_for_update(nowait=True)
    )
    if report is None:
        raise ReviewNotFoundError(code="REPORT_NOT_FOUND", message="报告不存在")
    if report.status is not ReportRunStatus.COMPLETED:
        raise ReviewError(code="REPORT_NOT_COMPLETED", message="报告尚未完成")

    plan = await _find_plan(db, tenant_id=tenant_id, report_run_id=report.id)
    if plan is None:
        config = await require_latest_sampling_config(db, tenant_id=tenant_id)
        try:
            async with db.begin_nested():
                result = await _create_plan(
                    db,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    report=report,
                    config=config,
                    fault_hook=fault_hook,
                )
                db.add(
                    ReviewPlanRequest(
                        tenant_id=tenant_id,
                        report_run_id=report.id,
                        sampling_plan_id=result.id,
                        idempotency_key_hash=key_hash,
                        request_fingerprint=request_fingerprint,
                    )
                )
                await db.flush()
                _fault(fault_hook, "request_ledger_written")
                return result
        except IntegrityError as exc:
            raise ReviewError(
                code="REVIEW_CONFLICT", message="抽样计划并发创建冲突，请重试"
            ) from exc

    try:
        async with db.begin_nested():
            db.add(
                ReviewPlanRequest(
                    tenant_id=tenant_id,
                    report_run_id=report.id,
                    sampling_plan_id=plan.id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fingerprint,
                )
            )
            await db.flush()
    except IntegrityError as exc:
        raise ReviewError(code="REVIEW_CONFLICT", message="抽样计划请求并发冲突，请重试") from exc
    return await _plan_result(db, plan, reused_existing=True)


async def _create_plan(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report: ReportRun,
    config: ReviewSamplingConfig,
    fault_hook: FaultHook | None,
) -> SamplingPlanResult:
    parameters = _config_parameters(config)
    eligible_rows = tuple(
        (
            await db.scalars(
                select(RowResult.row_no)
                .join(
                    ExpenseRow,
                    (ExpenseRow.file_version_id == RowResult.file_version_id)
                    & (ExpenseRow.row_no == RowResult.row_no)
                    & (ExpenseRow.tenant_id == RowResult.tenant_id),
                )
                .where(
                    RowResult.tenant_id == tenant_id,
                    RowResult.file_version_id == report.file_version_id,
                    RowResult.verdict == "passed",
                    ExpenseRow.parse_error_code.is_(None),
                )
                .order_by(RowResult.row_no)
            )
        ).all()
    )
    seed_hex = generate_seed_hex()
    selections = select_sampling_rows(
        eligible_row_nos=eligible_rows,
        parameters=parameters,
        seed_hex=seed_hex,
        tenant_id=tenant_id,
        report_run_id=report.id,
    )
    verify_sampling_selection(
        eligible_row_nos=eligible_rows,
        parameters=parameters,
        seed_hex=seed_hex,
        tenant_id=tenant_id,
        report_run_id=report.id,
        selections=selections,
    )
    plan = ReviewSamplingPlan(
        tenant_id=tenant_id,
        report_run_id=report.id,
        file_version_id=report.file_version_id,
        sampling_config_id=config.id,
        config_version=config.version,
        config_fingerprint=config.config_fingerprint,
        rate_bps=config.rate_bps,
        min_sample_size=config.min_sample_size,
        max_sample_size=config.max_sample_size,
        algorithm_version=config.algorithm_version,
        seed_hex=seed_hex,
        eligible_count=len(eligible_rows),
        sample_size=len(selections),
        created_by=actor_id,
    )
    db.add(plan)
    await db.flush()
    _fault(fault_hook, "plan_written")
    for selection in selections:
        db.add(
            SamplingAudit(
                tenant_id=tenant_id,
                sampling_plan_id=plan.id,
                report_run_id=report.id,
                file_version_id=report.file_version_id,
                row_no=selection.row_no,
                selection_rank=selection.selection_rank,
                selection_score_sha256=selection.selection_score_sha256,
                decision=None,
                reviewer_id=None,
                reviewed_at=None,
            )
        )
        await db.flush()
        _fault(fault_hook, "sample_written")
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="sampling.plan_create",
        target_type="review_sampling_plan",
        target_id=str(plan.id),
        payload={
            "algorithm_version": plan.algorithm_version,
            "config_fingerprint": plan.config_fingerprint,
            "config_version": plan.config_version,
            "eligible_count": plan.eligible_count,
            "file_version_id": str(plan.file_version_id),
            "report_run_id": str(plan.report_run_id),
            "sample_size": plan.sample_size,
        },
    )
    await db.flush()
    _fault(fault_hook, "success_audit_written")
    return await _plan_result(db, plan, reused_existing=False, selections=selections)


async def _find_plan(
    db: AsyncSession, *, tenant_id: uuid.UUID, report_run_id: uuid.UUID
) -> ReviewSamplingPlan | None:
    plan: ReviewSamplingPlan | None = await db.scalar(
        select(ReviewSamplingPlan).where(
            ReviewSamplingPlan.tenant_id == tenant_id,
            ReviewSamplingPlan.report_run_id == report_run_id,
        )
    )
    return plan


async def _plan_result(
    db: AsyncSession,
    plan: ReviewSamplingPlan,
    *,
    reused_existing: bool,
    selections: tuple[SamplingSelection, ...] | None = None,
) -> SamplingPlanResult:
    if selections is None:
        samples = tuple(
            (
                await db.scalars(
                    select(SamplingAudit)
                    .where(SamplingAudit.sampling_plan_id == plan.id)
                    .order_by(SamplingAudit.selection_rank)
                )
            ).all()
        )
        selections = tuple(
            SamplingSelection(
                row_no=sample.row_no,
                selection_rank=sample.selection_rank,
                selection_score_sha256=sample.selection_score_sha256,
            )
            for sample in samples
        )
    return SamplingPlanResult(
        id=plan.id,
        tenant_id=plan.tenant_id,
        report_run_id=plan.report_run_id,
        file_version_id=plan.file_version_id,
        sampling_config_id=plan.sampling_config_id,
        config_version=plan.config_version,
        config_fingerprint=plan.config_fingerprint,
        rate_bps=plan.rate_bps,
        min_sample_size=plan.min_sample_size,
        max_sample_size=plan.max_sample_size,
        algorithm_version=plan.algorithm_version,
        seed_hex=plan.seed_hex,
        eligible_count=plan.eligible_count,
        sample_size=plan.sample_size,
        created_by=plan.created_by,
        created_at=plan.created_at,
        selections=selections,
        reused_existing=reused_existing,
    )


def _config_parameters(config: ReviewSamplingConfig) -> SamplingConfigParameters:
    return SamplingConfigParameters(
        rate_bps=config.rate_bps,
        min_sample_size=config.min_sample_size,
        max_sample_size=config.max_sample_size,
        algorithm_version=config.algorithm_version,
    )


async def _lock_file(
    db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID
) -> FileVersion:
    try:
        batch = await db.scalar(
            select(FileVersion)
            .where(FileVersion.id == file_version_id, FileVersion.tenant_id == tenant_id)
            .with_for_update(nowait=True)
        )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise ReviewError(code="REVIEW_CONFLICT", message="该批次正在执行复核变更") from exc
        raise
    if batch is None:
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    return batch


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


async def _rollback_and_record_plan_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report_run_id: uuid.UUID,
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
            action="sampling.plan_failed",
            target_type="report_run",
            target_id=str(report_run_id),
            payload={"reason_code": "INTERNAL_ERROR"},
        )
        await audit_db.commit()


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
