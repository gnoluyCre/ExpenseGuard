"""Tenant-scoped read models for immutable F6 snapshots."""

from __future__ import annotations

import uuid

from sqlalchemy import case, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.detection.config_service import get_latest_detection_config
from app.core.detection.errors import DetectionInputError, DetectionNotFoundError
from app.core.detection.models import DETECTOR_ORDER, CapabilityStatus, DetectorKind
from app.core.detection.run_service import run_result
from app.core.detection.service_models import (
    BatchDetectionResult,
    CapabilityResult,
    DetectionRunResult,
    FindingDetail,
    FindingPage,
    FindingSummary,
    ParticipatingRowResult,
)
from app.db.models.batch import ExpenseRow, FileVersion
from app.db.models.detection import CorrelationFindingRow, DetectionRun
from app.db.models.detection import DetectorKind as DbDetectorKind
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding


async def get_detection_run(
    db: AsyncSession, *, tenant_id: uuid.UUID, run_id: uuid.UUID
) -> DetectionRunResult:
    run = await db.scalar(
        select(DetectionRun).where(DetectionRun.id == run_id, DetectionRun.tenant_id == tenant_id)
    )
    if run is None:
        raise DetectionNotFoundError(code="DETECTION_RUN_NOT_FOUND", message="关联检测运行不存在")
    return run_result(run, reused_existing=True)


async def get_batch_detection(
    db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID
) -> BatchDetectionResult:
    exists = await db.scalar(
        select(FileVersion.id).where(
            FileVersion.id == file_version_id, FileVersion.tenant_id == tenant_id
        )
    )
    if exists is None:
        raise DetectionNotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    current = await get_latest_detection_config(db, tenant_id=tenant_id)
    run = await db.scalar(
        select(DetectionRun)
        .where(
            DetectionRun.tenant_id == tenant_id,
            DetectionRun.file_version_id == file_version_id,
        )
        .order_by(DetectionRun.created_at.desc(), DetectionRun.id.desc())
        .limit(1)
    )
    if run is None:
        return BatchDetectionResult(
            run=None,
            capabilities=(),
            current_config_fingerprint=current.config_fingerprint if current else None,
            config_stale=False,
        )
    declarations = tuple(
        (
            await db.scalars(
                select(CapabilityDeclaration)
                .where(
                    CapabilityDeclaration.tenant_id == tenant_id,
                    CapabilityDeclaration.detection_run_id == run.id,
                )
                .order_by(_detector_order(CapabilityDeclaration.detector), CapabilityDeclaration.id)
            )
        ).all()
    )
    if tuple(DetectorKind(item.detector) for item in declarations) != DETECTOR_ORDER:
        raise RuntimeError("completed detection run does not have exactly four declarations")
    capabilities = tuple(
        CapabilityResult(
            detector=DetectorKind(item.detector),
            detector_version=item.detector_version,
            status=CapabilityStatus(item.status),
            reason_code=item.reason_code,
            reason=item.reason,
            details=item.details_json,
            finding_count=item.finding_count,
        )
        for item in declarations
    )
    return BatchDetectionResult(
        run=run_result(run, reused_existing=True),
        capabilities=capabilities,
        current_config_fingerprint=current.config_fingerprint if current else None,
        config_stale=current is None or current.config_fingerprint != run.config_fingerprint,
    )


async def list_detection_findings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    detector: DetectorKind | None = None,
    capability_status: CapabilityStatus | None = None,
    sort_by: str = "default",
    limit: int = 50,
    offset: int = 0,
) -> FindingPage:
    _validate_page(limit=limit, offset=offset)
    if sort_by != "default":
        raise DetectionInputError(code="DETECTION_QUERY_INVALID", message="排序参数无效")
    await get_detection_run(db, tenant_id=tenant_id, run_id=run_id)
    filters: list[ColumnElement[bool]] = [
        CorrelationFinding.tenant_id == tenant_id,
        CorrelationFinding.detection_run_id == run_id,
    ]
    if detector is not None:
        filters.append(CorrelationFinding.detector == detector.value)
    if capability_status is not None:
        filters.append(
            exists().where(
                CapabilityDeclaration.tenant_id == tenant_id,
                CapabilityDeclaration.detection_run_id == run_id,
                CapabilityDeclaration.detector == CorrelationFinding.detector,
                CapabilityDeclaration.status == capability_status.value,
            )
        )
    total = int(
        await db.scalar(select(func.count()).select_from(CorrelationFinding).where(*filters)) or 0
    )
    row_count = func.count(CorrelationFindingRow.id).label("row_count")
    first_row = func.min(CorrelationFindingRow.row_no).label("first_row")
    records = (
        await db.execute(
            select(CorrelationFinding, row_count, first_row)
            .join(
                CorrelationFindingRow,
                (CorrelationFindingRow.finding_id == CorrelationFinding.id)
                & (CorrelationFindingRow.tenant_id == tenant_id),
            )
            .where(*filters)
            .group_by(CorrelationFinding.id)
            .order_by(
                _detector_order(CorrelationFinding.detector),
                first_row,
                CorrelationFinding.finding_key,
                CorrelationFinding.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = tuple(
        FindingSummary(
            id=finding.id,
            run_id=finding.detection_run_id,
            detector=DetectorKind(finding.detector),
            detector_version=finding.detector_version,
            finding_key=finding.finding_key,
            reasoning=finding.reasoning_snapshot,
            participating_row_count=int(count),
            first_row_no=int(first),
        )
        for finding, count, first in records
    )
    return FindingPage(items=items, total=total, limit=limit, offset=offset)


async def get_correlation_finding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    finding_id: uuid.UUID,
    row_limit: int = 50,
    row_offset: int = 0,
) -> FindingDetail:
    _validate_page(limit=row_limit, offset=row_offset)
    finding = await db.scalar(
        select(CorrelationFinding).where(
            CorrelationFinding.id == finding_id,
            CorrelationFinding.tenant_id == tenant_id,
        )
    )
    if finding is None:
        raise DetectionNotFoundError(
            code="CORRELATION_FINDING_NOT_FOUND", message="关联检测候选不存在"
        )
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(CorrelationFindingRow)
            .where(
                CorrelationFindingRow.tenant_id == tenant_id,
                CorrelationFindingRow.finding_id == finding_id,
            )
        )
        or 0
    )
    records = (
        await db.execute(
            select(CorrelationFindingRow, ExpenseRow)
            .join(
                ExpenseRow,
                (ExpenseRow.tenant_id == CorrelationFindingRow.tenant_id)
                & (ExpenseRow.file_version_id == CorrelationFindingRow.file_version_id)
                & (ExpenseRow.row_no == CorrelationFindingRow.row_no),
            )
            .where(
                CorrelationFindingRow.tenant_id == tenant_id,
                CorrelationFindingRow.finding_id == finding_id,
            )
            .order_by(
                CorrelationFindingRow.ordinal,
                CorrelationFindingRow.row_no,
                CorrelationFindingRow.id,
            )
            .limit(row_limit)
            .offset(row_offset)
        )
    ).all()
    rows = tuple(
        ParticipatingRowResult(
            ordinal=link.ordinal,
            row_no=link.row_no,
            raw=expense.raw_json,
            normalized=expense.normalized_json,
            parse_error_code=expense.parse_error_code,
        )
        for link, expense in records
    )
    return FindingDetail(
        id=finding.id,
        run_id=finding.detection_run_id,
        file_version_id=finding.file_version_id,
        detector=DetectorKind(finding.detector),
        detector_version=finding.detector_version,
        finding_key=finding.finding_key,
        evidence=finding.evidence_json,
        reasoning=finding.reasoning_snapshot,
        rows=rows,
        completed=min(row_offset + len(rows), total),
        total=total,
    )


def _validate_page(*, limit: int, offset: int) -> None:
    if not 1 <= limit <= 200 or offset < 0:
        raise DetectionInputError(code="DETECTION_QUERY_INVALID", message="分页参数无效")


def _detector_order(column: InstrumentedAttribute[DbDetectorKind]) -> ColumnElement[int]:
    return case(
        *[(column == detector.value, rank) for rank, detector in enumerate(DETECTOR_ORDER)],
        else_=len(DETECTOR_ORDER),
    )
