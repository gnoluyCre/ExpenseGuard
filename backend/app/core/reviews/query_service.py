"""Read-only joined projections for the F5 review workspace."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.reports.service import load_report_snapshot
from app.core.reviews.errors import ReviewError, ReviewInputError
from app.core.reviews.models import (
    ClearanceReviewDetail,
    FindingReviewDetail,
    FindingReviewResult,
    ReviewItemEvidence,
    ReviewQueueItem,
    ReviewQueuePage,
    ReviewQueueStatus,
    ReviewSummary,
    SamplingReviewResult,
)
from app.db.models.batch import ExpenseRow, RowResult
from app.db.models.findings import (
    Review,
    ReviewDecision,
    ReviewSamplingPlan,
    SamplingAudit,
    SamplingReview,
    SamplingReviewDecision,
)
from app.db.models.reports import ReportAttentionGroup, ReportItem, ReportRun, ReportRunStatus


async def list_review_queue(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: ReviewQueueStatus = "pending",
    limit: int = 50,
    offset: int = 0,
) -> ReviewQueuePage:
    """Return the stable union of finding reviews and clearance samples."""
    if status not in {"pending", "completed"}:
        raise ReviewInputError(code="REVIEW_FILTER_INVALID", message="复核队列筛选无效")
    if not 1 <= limit <= 200 or offset < 0:
        raise ReviewInputError(code="REVIEW_PAGINATION_INVALID", message="复核分页参数无效")

    finding_rows = tuple(
        (
            await db.execute(
                select(ReportItem, ReportRun, Review)
                .join(ReportRun, ReportRun.id == ReportItem.report_run_id)
                .outerjoin(Review, Review.report_item_id == ReportItem.id)
                .where(
                    ReportItem.tenant_id == tenant_id,
                    ReportRun.status == ReportRunStatus.COMPLETED,
                    ReportItem.attention_group.in_(
                        [
                            ReportAttentionGroup.HIGH_ATTENTION,
                            ReportAttentionGroup.MANUAL_ATTENTION,
                        ]
                    ),
                    Review.id.is_(None) if status == "pending" else Review.id.is_not(None),
                )
            )
        ).all()
    )
    sample_rows = tuple(
        (
            await db.execute(
                select(SamplingAudit, ReportRun, SamplingReview)
                .join(ReportRun, ReportRun.id == SamplingAudit.report_run_id)
                .outerjoin(
                    SamplingReview,
                    SamplingReview.sampling_audit_id == SamplingAudit.id,
                )
                .where(
                    SamplingAudit.tenant_id == tenant_id,
                    ReportRun.status == ReportRunStatus.COMPLETED,
                    (
                        SamplingReview.id.is_(None)
                        if status == "pending"
                        else SamplingReview.id.is_not(None)
                    ),
                )
            )
        ).all()
    )

    items = [
        ReviewQueueItem(
            kind="finding",
            status=status,
            target_id=item.id,
            report_run_id=item.report_run_id,
            file_version_id=item.file_version_id,
            report_completed_at=_completed_at(report),
            row_no=item.row_no,
            attention_group=item.attention_group,
            finding_id=item.finding_id,
            rule_id=item.rule_id,
            rule_version=item.rule_version,
            sampling_plan_id=None,
            selection_rank=None,
            decision=review.decision if review is not None else None,
            reviewer_id=review.reviewer_id if review is not None else None,
            reviewed_at=review.reviewed_at if review is not None else None,
        )
        for item, report, review in finding_rows
    ]
    items.extend(
        ReviewQueueItem(
            kind="clearance_sample",
            status=status,
            target_id=sample.id,
            report_run_id=sample.report_run_id,
            file_version_id=sample.file_version_id,
            report_completed_at=_completed_at(report),
            row_no=sample.row_no,
            attention_group=None,
            finding_id=None,
            rule_id=None,
            rule_version=None,
            sampling_plan_id=sample.sampling_plan_id,
            selection_rank=sample.selection_rank,
            decision=review.decision if review is not None else None,
            reviewer_id=review.reviewer_id if review is not None else None,
            reviewed_at=review.reviewed_at if review is not None else None,
        )
        for sample, report, review in sample_rows
    )
    items.sort(key=_pending_order if status == "pending" else _completed_order)
    total = len(items)
    return ReviewQueuePage(
        items=tuple(items[offset : offset + limit]),
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_review_summary(
    db: AsyncSession, *, tenant_id: uuid.UUID, report_run_id: uuid.UUID
) -> ReviewSummary:
    report = await db.scalar(
        select(ReportRun).where(
            ReportRun.id == report_run_id,
            ReportRun.tenant_id == tenant_id,
            ReportRun.status == ReportRunStatus.COMPLETED,
        )
    )
    if report is None:
        raise ReviewError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    plan = await db.scalar(
        select(ReviewSamplingPlan).where(
            ReviewSamplingPlan.tenant_id == tenant_id,
            ReviewSamplingPlan.report_run_id == report_run_id,
        )
    )
    finding_base = (
        select(func.count())
        .select_from(ReportItem)
        .where(
            ReportItem.tenant_id == tenant_id,
            ReportItem.report_run_id == report_run_id,
            ReportItem.attention_group.in_(
                [ReportAttentionGroup.HIGH_ATTENTION, ReportAttentionGroup.MANUAL_ATTENTION]
            ),
        )
    )
    finding_total = int(await db.scalar(finding_base) or 0)
    finding_completed = int(
        await db.scalar(
            select(func.count())
            .select_from(Review)
            .where(Review.tenant_id == tenant_id, Review.report_run_id == report_run_id)
        )
        or 0
    )
    confirmed = await _decision_count(
        db,
        tenant_id=tenant_id,
        report_run_id=report_run_id,
        model=Review,
        decision=ReviewDecision.CONFIRMED,
    )
    false_positive = await _decision_count(
        db,
        tenant_id=tenant_id,
        report_run_id=report_run_id,
        model=Review,
        decision=ReviewDecision.FALSE_POSITIVE,
    )
    if plan is None:
        return ReviewSummary(
            report_run_id=report_run_id,
            sampling_status="legacy_not_initialized",
            finding_pending=finding_total - finding_completed,
            finding_completed=finding_completed,
            finding_confirmed=confirmed,
            finding_false_positive=false_positive,
            sample_eligible=0,
            sample_selected=0,
            sample_pending=0,
            sample_completed=0,
            sample_clearance_confirmed=0,
            sample_missed_issue=0,
        )
    sample_completed = int(
        await db.scalar(
            select(func.count())
            .select_from(SamplingReview)
            .where(
                SamplingReview.tenant_id == tenant_id,
                SamplingReview.report_run_id == report_run_id,
            )
        )
        or 0
    )
    clearance_confirmed = await _decision_count(
        db,
        tenant_id=tenant_id,
        report_run_id=report_run_id,
        model=SamplingReview,
        decision=SamplingReviewDecision.CLEARANCE_CONFIRMED,
    )
    missed_issue = await _decision_count(
        db,
        tenant_id=tenant_id,
        report_run_id=report_run_id,
        model=SamplingReview,
        decision=SamplingReviewDecision.MISSED_ISSUE,
    )
    return ReviewSummary(
        report_run_id=report_run_id,
        sampling_status="completed",
        finding_pending=finding_total - finding_completed,
        finding_completed=finding_completed,
        finding_confirmed=confirmed,
        finding_false_positive=false_positive,
        sample_eligible=plan.eligible_count,
        sample_selected=plan.sample_size,
        sample_pending=plan.sample_size - sample_completed,
        sample_completed=sample_completed,
        sample_clearance_confirmed=clearance_confirmed,
        sample_missed_issue=missed_issue,
    )


async def get_finding_review_detail(
    db: AsyncSession, *, tenant_id: uuid.UUID, report_item_id: uuid.UUID
) -> FindingReviewDetail:
    row = (
        await db.execute(
            select(ReportItem, ExpenseRow, Review)
            .join(
                ExpenseRow,
                (ExpenseRow.file_version_id == ReportItem.file_version_id)
                & (ExpenseRow.row_no == ReportItem.row_no)
                & (ExpenseRow.tenant_id == ReportItem.tenant_id),
            )
            .outerjoin(Review, Review.report_item_id == ReportItem.id)
            .where(
                ReportItem.id == report_item_id,
                ReportItem.tenant_id == tenant_id,
                ReportItem.attention_group.in_(
                    [ReportAttentionGroup.HIGH_ATTENTION, ReportAttentionGroup.MANUAL_ATTENTION]
                ),
            )
        )
    ).one_or_none()
    if row is None:
        raise ReviewError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    item, expense_row, review = row
    snapshot = await load_report_snapshot(db, report_run_id=item.report_run_id)
    item_snapshot = next(
        (snapshot_item for snapshot_item in snapshot.items if snapshot_item.id == item.id),
        None,
    )
    if item_snapshot is None:
        raise RuntimeError("completed report item is missing from immutable snapshot")
    return FindingReviewDetail(
        report_run_id=item.report_run_id,
        report_item=ReviewItemEvidence.model_validate(item_snapshot.model_dump()),
        raw_row=expense_row.raw_json,
        normalized_row=expense_row.normalized_json,
        existing_review=(
            FindingReviewResult(
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
                reused_existing=True,
            )
            if review is not None
            else None
        ),
    )


async def get_sampling_review_detail(
    db: AsyncSession, *, tenant_id: uuid.UUID, sampling_audit_id: uuid.UUID
) -> ClearanceReviewDetail:
    row = (
        await db.execute(
            select(SamplingAudit, ExpenseRow, RowResult, ReportRun, SamplingReview)
            .join(
                ExpenseRow,
                (ExpenseRow.file_version_id == SamplingAudit.file_version_id)
                & (ExpenseRow.row_no == SamplingAudit.row_no)
                & (ExpenseRow.tenant_id == SamplingAudit.tenant_id),
            )
            .join(
                RowResult,
                (RowResult.file_version_id == SamplingAudit.file_version_id)
                & (RowResult.row_no == SamplingAudit.row_no)
                & (RowResult.tenant_id == SamplingAudit.tenant_id),
            )
            .join(ReportRun, ReportRun.id == SamplingAudit.report_run_id)
            .outerjoin(SamplingReview, SamplingReview.sampling_audit_id == SamplingAudit.id)
            .where(
                SamplingAudit.id == sampling_audit_id,
                SamplingAudit.tenant_id == tenant_id,
                RowResult.verdict == "passed",
                ReportRun.status == ReportRunStatus.COMPLETED,
            )
        )
    ).one_or_none()
    if row is None:
        raise ReviewError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
    sample, expense_row, row_result, report, review = row
    snapshot = await load_report_snapshot(db, report_run_id=sample.report_run_id)
    cleared_items = tuple(
        ReviewItemEvidence.model_validate(item.model_dump())
        for item in snapshot.items
        if item.row_no == sample.row_no and item.attention_group is ReportAttentionGroup.CLEARED
    )
    return ClearanceReviewDetail(
        report_run_id=sample.report_run_id,
        sampling_audit_id=sample.id,
        sampling_plan_id=sample.sampling_plan_id,
        file_version_id=sample.file_version_id,
        row_no=sample.row_no,
        raw_row=expense_row.raw_json,
        normalized_row=expense_row.normalized_json,
        source_verdict="passed",
        ruleset_fingerprint=row_result.rule_version,
        report_fingerprint=report.report_fingerprint,
        cleared_items=cleared_items,
        existing_review=(
            SamplingReviewResult(
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
                reused_existing=True,
            )
            if review is not None
            else None
        ),
    )


async def _decision_count(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    report_run_id: uuid.UUID,
    model: type[Review] | type[SamplingReview],
    decision: ReviewDecision | SamplingReviewDecision,
) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.tenant_id == tenant_id,
                model.report_run_id == report_run_id,
                model.decision == decision,
            )
        )
        or 0
    )


def _pending_order(item: ReviewQueueItem) -> tuple[object, ...]:
    attention_rank = {
        ReportAttentionGroup.HIGH_ATTENTION: 0,
        ReportAttentionGroup.MANUAL_ATTENTION: 1,
        None: 2,
    }[item.attention_group]
    version_key = (0, "") if item.rule_version is None else (1, item.rule_version)
    return (
        attention_rank,
        item.report_completed_at,
        item.row_no,
        item.rule_id or "",
        version_key,
        str(item.finding_id or ""),
        item.selection_rank or 0,
        str(item.target_id),
    )


def _completed_order(item: ReviewQueueItem) -> tuple[object, ...]:
    reviewed_at = item.reviewed_at
    if reviewed_at is None:
        raise RuntimeError("completed queue item is missing reviewed_at")
    return (-reviewed_at.timestamp(), *_pending_order(item))


def _completed_at(report: ReportRun) -> datetime:
    if report.completed_at is None:
        raise RuntimeError("completed report is missing completed_at")
    return report.completed_at
