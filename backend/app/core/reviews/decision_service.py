"""One-time finding and clearance review decision services."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError
from app.core.policies.canonical import canonical_sha256
from app.core.reviews.errors import (
    ReviewError,
    ReviewInputError,
    ReviewInternalError,
    ReviewNotFoundError,
)
from app.core.reviews.models import (
    FindingReviewCommand,
    FindingReviewResult,
    SamplingReviewCommand,
    SamplingReviewResult,
)
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import FileVersion, RowResult
from app.db.models.findings import (
    Review,
    ReviewDecision,
    SamplingAudit,
    SamplingReview,
    SamplingReviewDecision,
)
from app.db.models.reports import ReportAttentionGroup, ReportItem, ReportRun, ReportRunStatus
from app.db.models.tenancy import AppUser

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
FaultHook = Callable[[str], None]


async def submit_finding_review(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report_item_id: uuid.UUID,
    decision: ReviewDecision | str,
    note: str | None,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> FindingReviewResult:
    command = _validate_finding_command(
        decision=decision,
        note=note,
        idempotency_key=idempotency_key,
    )
    try:
        async with db.begin_nested():
            return await _submit_finding_review(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_item_id=report_item_id,
                command=command,
                fault_hook=fault_hook,
            )
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="review.submit_failed",
            target_type="report_item",
            target_id=report_item_id,
            reason_code="INTERNAL_ERROR",
        )
        raise ReviewInternalError from exc


async def submit_sampling_review(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    sampling_audit_id: uuid.UUID,
    decision: SamplingReviewDecision | str,
    note: str | None,
    idempotency_key: str,
    fault_hook: FaultHook | None = None,
) -> SamplingReviewResult:
    command = _validate_sampling_command(
        decision=decision,
        note=note,
        idempotency_key=idempotency_key,
    )
    try:
        async with db.begin_nested():
            return await _submit_sampling_review(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                sampling_audit_id=sampling_audit_id,
                command=command,
                fault_hook=fault_hook,
            )
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="sampling.review_submit_failed",
            target_type="sampling_audit",
            target_id=sampling_audit_id,
            reason_code="INTERNAL_ERROR",
        )
        raise ReviewInternalError from exc


async def _submit_finding_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    report_item_id: uuid.UUID,
    command: FindingReviewCommand,
    fault_hook: FaultHook | None,
) -> FindingReviewResult:
    await _lock_tenant(db, tenant_id)
    item = await db.scalar(
        select(ReportItem).where(
            ReportItem.id == report_item_id,
            ReportItem.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    if item.attention_group not in {
        ReportAttentionGroup.HIGH_ATTENTION,
        ReportAttentionGroup.MANUAL_ATTENTION,
    }:
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    await _lock_file(db, tenant_id=tenant_id, file_version_id=item.file_version_id)
    item = await db.scalar(
        select(ReportItem)
        .where(ReportItem.id == report_item_id, ReportItem.tenant_id == tenant_id)
        .with_for_update(nowait=True)
    )
    if item is None:
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    await _require_completed_report(db, tenant_id=tenant_id, report_run_id=item.report_run_id)
    await _require_actor(db, tenant_id=tenant_id, actor_id=actor_id)

    key_hash = _sha256(command.idempotency_key)
    note_hash = _sha256(command.note) if command.note is not None else None
    request_fingerprint = _decision_fingerprint(
        tenant_id=tenant_id,
        target_kind="finding",
        target_id=item.id,
        decision=command.decision.value,
        note_hash=note_hash,
    )
    keyed = await db.scalar(
        select(Review).where(
            Review.tenant_id == tenant_id,
            Review.idempotency_key_hash == key_hash,
        )
    )
    if keyed is not None:
        if keyed.request_fingerprint != request_fingerprint:
            raise ReviewError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他复核请求",
            )
        return _finding_result(keyed, reused_existing=True)
    completed = await db.scalar(
        select(Review).where(
            Review.tenant_id == tenant_id,
            Review.report_item_id == item.id,
        )
    )
    if completed is not None:
        raise ReviewError(code="REVIEW_ALREADY_COMPLETED", message="该判定已经完成复核")

    review = Review(
        tenant_id=tenant_id,
        report_run_id=item.report_run_id,
        report_item_id=item.id,
        file_version_id=item.file_version_id,
        finding_id=item.finding_id,
        decision=command.decision,
        reviewer_id=actor_id,
        note=command.note,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    try:
        async with db.begin_nested():
            db.add(review)
            await db.flush()
            _fault(fault_hook, "decision_written")
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="review.submit",
                target_type="report_item",
                target_id=str(item.id),
                payload={
                    "decision": review.decision.value,
                    "finding_id": str(review.finding_id),
                    "note_sha256": note_hash,
                    "report_run_id": str(review.report_run_id),
                },
            )
            await db.flush()
            _fault(fault_hook, "success_audit_written")
    except IntegrityError as exc:
        raise ReviewError(code="REVIEW_CONFLICT", message="复核结论并发提交冲突") from exc
    return _finding_result(review, reused_existing=False)


async def _submit_sampling_review(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    sampling_audit_id: uuid.UUID,
    command: SamplingReviewCommand,
    fault_hook: FaultHook | None,
) -> SamplingReviewResult:
    await _lock_tenant(db, tenant_id)
    sample = await db.scalar(
        select(SamplingAudit).where(
            SamplingAudit.id == sampling_audit_id,
            SamplingAudit.tenant_id == tenant_id,
        )
    )
    if sample is None:
        raise ReviewNotFoundError(code="SAMPLE_NOT_FOUND", message="抽检样本不存在")
    verdict = await db.scalar(
        select(RowResult.verdict).where(
            RowResult.tenant_id == tenant_id,
            RowResult.file_version_id == sample.file_version_id,
            RowResult.row_no == sample.row_no,
        )
    )
    if verdict != "passed":
        raise ReviewNotFoundError(code="SAMPLE_NOT_FOUND", message="抽检样本不存在")
    await _lock_file(db, tenant_id=tenant_id, file_version_id=sample.file_version_id)
    sample = await db.scalar(
        select(SamplingAudit)
        .where(SamplingAudit.id == sampling_audit_id, SamplingAudit.tenant_id == tenant_id)
        .with_for_update(nowait=True)
    )
    if sample is None:
        raise ReviewNotFoundError(code="SAMPLE_NOT_FOUND", message="抽检样本不存在")
    await _require_completed_report(db, tenant_id=tenant_id, report_run_id=sample.report_run_id)
    await _require_actor(db, tenant_id=tenant_id, actor_id=actor_id)

    key_hash = _sha256(command.idempotency_key)
    note_hash = _sha256(command.note) if command.note is not None else None
    request_fingerprint = _decision_fingerprint(
        tenant_id=tenant_id,
        target_kind="clearance_sample",
        target_id=sample.id,
        decision=command.decision.value,
        note_hash=note_hash,
    )
    keyed = await db.scalar(
        select(SamplingReview).where(
            SamplingReview.tenant_id == tenant_id,
            SamplingReview.idempotency_key_hash == key_hash,
        )
    )
    if keyed is not None:
        if keyed.request_fingerprint != request_fingerprint:
            raise ReviewError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他抽检复核请求",
            )
        return _sampling_result(keyed, reused_existing=True)
    completed = await db.scalar(
        select(SamplingReview).where(
            SamplingReview.tenant_id == tenant_id,
            SamplingReview.sampling_audit_id == sample.id,
        )
    )
    if completed is not None:
        raise ReviewError(code="SAMPLE_ALREADY_REVIEWED", message="该抽检样本已经完成复核")

    review = SamplingReview(
        tenant_id=tenant_id,
        sampling_audit_id=sample.id,
        sampling_plan_id=sample.sampling_plan_id,
        report_run_id=sample.report_run_id,
        file_version_id=sample.file_version_id,
        decision=command.decision,
        reviewer_id=actor_id,
        note=command.note,
        idempotency_key_hash=key_hash,
        request_fingerprint=request_fingerprint,
    )
    try:
        async with db.begin_nested():
            db.add(review)
            await db.flush()
            _fault(fault_hook, "decision_written")
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action="sampling.review_submit",
                target_type="sampling_audit",
                target_id=str(sample.id),
                payload={
                    "decision": review.decision.value,
                    "note_sha256": note_hash,
                    "report_run_id": str(review.report_run_id),
                    "sampling_plan_id": str(review.sampling_plan_id),
                },
            )
            await db.flush()
            _fault(fault_hook, "success_audit_written")
    except IntegrityError as exc:
        raise ReviewError(code="REVIEW_CONFLICT", message="抽检结论并发提交冲突") from exc
    return _sampling_result(review, reused_existing=False)


async def _lock_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    try:
        await lock_tenant_nowait(db, tenant_id)
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise ReviewError(code="REVIEW_CONFLICT", message="该租户正在执行复核变更") from exc
        raise


async def _lock_file(db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID) -> None:
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


async def _require_completed_report(
    db: AsyncSession, *, tenant_id: uuid.UUID, report_run_id: uuid.UUID
) -> None:
    report = await db.scalar(
        select(ReportRun).where(
            ReportRun.id == report_run_id,
            ReportRun.tenant_id == tenant_id,
        )
    )
    if report is None or report.status is not ReportRunStatus.COMPLETED:
        raise ReviewNotFoundError(code="REPORT_NOT_FOUND", message="报告不存在")


async def _require_actor(db: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    actor = await db.scalar(
        select(AppUser).where(
            AppUser.id == actor_id,
            AppUser.tenant_id == tenant_id,
            AppUser.is_active.is_(True),
        )
    )
    if actor is None:
        raise ReviewError(code="REVIEW_ACTOR_NOT_FOUND", message="复核人不存在")


def _validate_finding_command(**values: object) -> FindingReviewCommand:
    try:
        return FindingReviewCommand.model_validate(values)
    except ValidationError as exc:
        if _note_required(exc):
            raise ReviewInputError(
                code="REVIEW_NOTE_REQUIRED", message="该复核结论必须填写说明"
            ) from exc
        raise ReviewInputError(code="REVIEW_DECISION_INVALID", message="复核结论无效") from exc


def _validate_sampling_command(**values: object) -> SamplingReviewCommand:
    try:
        return SamplingReviewCommand.model_validate(values)
    except ValidationError as exc:
        if _note_required(exc):
            raise ReviewInputError(
                code="REVIEW_NOTE_REQUIRED", message="该抽检结论必须填写说明"
            ) from exc
        raise ReviewInputError(code="REVIEW_DECISION_INVALID", message="抽检结论无效") from exc


def _note_required(exc: ValidationError) -> bool:
    return any(
        "note is required" in str(error.get("ctx", {}).get("error", "")) for error in exc.errors()
    )


def _decision_fingerprint(
    *,
    tenant_id: uuid.UUID,
    target_kind: str,
    target_id: uuid.UUID,
    decision: str,
    note_hash: str | None,
) -> str:
    return canonical_sha256(
        {
            "decision": decision,
            "note_sha256": note_hash,
            "schema_version": 1,
            "target_id": str(target_id),
            "target_kind": target_kind,
            "tenant_id": str(tenant_id),
        }
    )


def _finding_result(review: Review, *, reused_existing: bool) -> FindingReviewResult:
    return FindingReviewResult(
        id=review.id,
        tenant_id=review.tenant_id,
        report_run_id=review.report_run_id,
        report_item_id=review.report_item_id,
        file_version_id=review.file_version_id,
        finding_id=review.finding_id,
        decision=review.decision,
        reviewer_id=review.reviewer_id,
        reviewed_at=review.reviewed_at,
        note=review.note,
        reused_existing=reused_existing,
    )


def _sampling_result(review: SamplingReview, *, reused_existing: bool) -> SamplingReviewResult:
    return SamplingReviewResult(
        id=review.id,
        tenant_id=review.tenant_id,
        sampling_audit_id=review.sampling_audit_id,
        sampling_plan_id=review.sampling_plan_id,
        report_run_id=review.report_run_id,
        file_version_id=review.file_version_id,
        decision=review.decision,
        reviewer_id=review.reviewer_id,
        reviewed_at=review.reviewed_at,
        note=review.note,
        reused_existing=reused_existing,
    )


async def _rollback_and_record_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    reason_code: str,
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
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            payload={"reason_code": reason_code},
        )
        await audit_db.commit()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _sqlstate(exc: OperationalError) -> str | None:
    value: Any = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
