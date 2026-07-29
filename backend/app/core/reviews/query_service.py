"""Read-only joined projections for the F5 review workspace."""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, String, case, cast, func, literal, select, true, union_all
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.reports.service import load_report_snapshot
from app.core.reviews.errors import ReviewInputError, ReviewNotFoundError
from app.core.reviews.models import (
    ClearanceReviewDetail,
    ClearanceReviewQueueItem,
    FindingReviewDetail,
    FindingReviewQueueItem,
    FindingReviewResult,
    ReviewCoverage,
    ReviewItemEvidence,
    ReviewQueueItem,
    ReviewQueueKind,
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
    kind: ReviewQueueKind | None = None,
    report_run_id: uuid.UUID | None = None,
    file_version_id: uuid.UUID | None = None,
    sort_by: str = "default",
    limit: int = 50,
    offset: int = 0,
) -> ReviewQueuePage:
    """Return the stable union of finding reviews and clearance samples."""
    if status not in {"pending", "completed"}:
        raise ReviewInputError(code="REVIEW_FILTER_INVALID", message="复核队列筛选无效")
    if kind not in {None, "finding", "clearance_sample"} or sort_by != "default":
        raise ReviewInputError(code="REVIEW_FILTER_INVALID", message="复核队列筛选无效")
    if not 1 <= limit <= 200 or offset < 0:
        raise ReviewInputError(code="REVIEW_PAGINATION_INVALID", message="复核分页参数无效")

    uuid_type = PG_UUID(as_uuid=True)
    finding_query = (
        select(
            literal("finding").label("kind"),
            case(
                (ReviewSamplingPlan.id.is_not(None), "completed"),
                else_="legacy_not_initialized",
            ).label("sampling_status"),
            ReportItem.id.label("target_id"),
            ReportItem.report_run_id.label("report_run_id"),
            ReportItem.file_version_id.label("file_version_id"),
            ReportRun.completed_at.label("report_completed_at"),
            ReportItem.row_no.label("row_no"),
            cast(ReportItem.attention_group, String).label("attention_group"),
            ReportItem.finding_id.label("finding_id"),
            ReportItem.rule_id.label("rule_id"),
            ReportItem.rule_version.label("rule_version"),
            cast(literal(None), uuid_type).label("sampling_plan_id"),
            cast(literal(None), Integer).label("selection_rank"),
            cast(Review.decision, String).label("decision"),
            Review.reviewer_id.label("reviewer_id"),
            Review.reviewed_at.label("reviewed_at"),
            case(
                (ReportItem.attention_group == ReportAttentionGroup.HIGH_ATTENTION, 0),
                (ReportItem.attention_group == ReportAttentionGroup.MANUAL_ATTENTION, 1),
                else_=2,
            ).label("attention_rank"),
            case((ReportItem.rule_version.is_(None), 0), else_=1).label("version_rank"),
            func.coalesce(ReportItem.rule_version, "").label("rule_version_sort"),
            cast(ReportItem.finding_id, String).label("finding_sort_id"),
            literal(0).label("selection_sort"),
            cast(ReportItem.id, String).label("target_sort_id"),
        )
        .join(ReportRun, ReportRun.id == ReportItem.report_run_id)
        .outerjoin(Review, Review.report_item_id == ReportItem.id)
        .outerjoin(
            ReviewSamplingPlan,
            ReviewSamplingPlan.report_run_id == ReportItem.report_run_id,
        )
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
            ReportItem.report_run_id == report_run_id if report_run_id is not None else true(),
            ReportItem.file_version_id == file_version_id
            if file_version_id is not None
            else true(),
        )
    )
    sample_query = (
        select(
            literal("clearance_sample").label("kind"),
            literal("completed").label("sampling_status"),
            SamplingAudit.id.label("target_id"),
            SamplingAudit.report_run_id.label("report_run_id"),
            SamplingAudit.file_version_id.label("file_version_id"),
            ReportRun.completed_at.label("report_completed_at"),
            SamplingAudit.row_no.label("row_no"),
            cast(literal(None), String).label("attention_group"),
            cast(literal(None), uuid_type).label("finding_id"),
            cast(literal(None), String).label("rule_id"),
            cast(literal(None), String).label("rule_version"),
            SamplingAudit.sampling_plan_id.label("sampling_plan_id"),
            SamplingAudit.selection_rank.label("selection_rank"),
            cast(SamplingReview.decision, String).label("decision"),
            SamplingReview.reviewer_id.label("reviewer_id"),
            SamplingReview.reviewed_at.label("reviewed_at"),
            literal(2).label("attention_rank"),
            literal(0).label("version_rank"),
            literal("").label("rule_version_sort"),
            literal("").label("finding_sort_id"),
            SamplingAudit.selection_rank.label("selection_sort"),
            cast(SamplingAudit.id, String).label("target_sort_id"),
        )
        .join(ReportRun, ReportRun.id == SamplingAudit.report_run_id)
        .outerjoin(
            SamplingReview,
            SamplingReview.sampling_audit_id == SamplingAudit.id,
        )
        .where(
            SamplingAudit.tenant_id == tenant_id,
            ReportRun.status == ReportRunStatus.COMPLETED,
            SamplingReview.id.is_(None) if status == "pending" else SamplingReview.id.is_not(None),
            SamplingAudit.report_run_id == report_run_id if report_run_id is not None else true(),
            SamplingAudit.file_version_id == file_version_id
            if file_version_id is not None
            else true(),
        )
    )
    selected_queries = []
    if kind in {None, "finding"}:
        selected_queries.append(finding_query)
    if kind in {None, "clearance_sample"}:
        selected_queries.append(sample_query)
    queue = (
        union_all(*selected_queries).subquery()
        if len(selected_queries) > 1
        else selected_queries[0].subquery()
    )
    total = int(await db.scalar(select(func.count()).select_from(queue)) or 0)
    base_ordering = (
        queue.c.attention_rank,
        queue.c.report_completed_at,
        queue.c.row_no,
        queue.c.rule_id,
        queue.c.version_rank,
        queue.c.rule_version_sort,
        queue.c.finding_sort_id,
        queue.c.selection_sort,
        queue.c.target_sort_id,
    )
    ordering = (
        (queue.c.reviewed_at.desc(), *base_ordering) if status == "completed" else base_ordering
    )
    rows = tuple(
        (await db.execute(select(queue).order_by(*ordering).limit(limit).offset(offset))).mappings()
    )
    items: list[ReviewQueueItem] = []
    for row in rows:
        completed_at = row["report_completed_at"]
        if completed_at is None:
            raise RuntimeError("completed report is missing completed_at")
        if row["kind"] == "finding":
            items.append(
                FindingReviewQueueItem(
                    kind="finding",
                    status=status,
                    sampling_status=row["sampling_status"],
                    target_id=row["target_id"],
                    report_run_id=row["report_run_id"],
                    file_version_id=row["file_version_id"],
                    report_completed_at=completed_at,
                    row_no=row["row_no"],
                    attention_group=row["attention_group"],
                    finding_id=row["finding_id"],
                    rule_id=row["rule_id"],
                    rule_version=row["rule_version"],
                    decision=row["decision"],
                    reviewer_id=row["reviewer_id"],
                    reviewed_at=row["reviewed_at"],
                )
            )
        else:
            items.append(
                ClearanceReviewQueueItem(
                    kind="clearance_sample",
                    status=status,
                    sampling_status="completed",
                    target_id=row["target_id"],
                    report_run_id=row["report_run_id"],
                    file_version_id=row["file_version_id"],
                    report_completed_at=completed_at,
                    row_no=row["row_no"],
                    sampling_plan_id=row["sampling_plan_id"],
                    selection_rank=row["selection_rank"],
                    decision=row["decision"],
                    reviewer_id=row["reviewer_id"],
                    reviewed_at=row["reviewed_at"],
                )
            )
    return ReviewQueuePage(
        items=tuple(items),
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
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
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
            finding_review_coverage=ReviewCoverage(
                completed=finding_completed,
                total=finding_total,
            ),
            sample_eligible=0,
            sample_selected=0,
            sample_pending=0,
            sample_completed=0,
            sample_clearance_confirmed=0,
            sample_missed_issue=0,
            sample_review_coverage=ReviewCoverage(completed=0, total=0),
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
        finding_review_coverage=ReviewCoverage(
            completed=finding_completed,
            total=finding_total,
        ),
        sample_eligible=plan.eligible_count,
        sample_selected=plan.sample_size,
        sample_pending=plan.sample_size - sample_completed,
        sample_completed=sample_completed,
        sample_clearance_confirmed=clearance_confirmed,
        sample_missed_issue=missed_issue,
        sample_review_coverage=ReviewCoverage(
            completed=sample_completed,
            total=plan.sample_size,
        ),
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
        raise ReviewNotFoundError(code="REVIEW_TARGET_NOT_FOUND", message="复核目标不存在")
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
        raise ReviewNotFoundError(code="SAMPLE_NOT_FOUND", message="抽检样本不存在")
    sample, expense_row, _row_result, report, review = row
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
        ruleset_fingerprint=report.ruleset_fingerprint,
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
