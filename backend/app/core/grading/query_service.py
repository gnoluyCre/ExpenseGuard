"""Tenant-scoped queries over immutable F8 grading snapshots."""

from __future__ import annotations

import uuid

from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.detection.models import CapabilityStatus, DetectorKind
from app.core.grading.config_service import config_from_record
from app.core.grading.errors import GradingInputError, GradingInternalError, GradingNotFoundError
from app.core.grading.manifest_builder import source_row_fingerprint
from app.core.grading.models import (
    DISPOSITION_ORDER,
    SOURCE_KIND_ORDER,
    Disposition,
    GradingReasonCode,
    InvestigationGradingOutcome,
    SourceKind,
)
from app.core.grading.service_models import (
    BatchGradingView,
    GradingItemPage,
    GradingItemView,
    GradingRowPage,
    GradingRowView,
    GradingRunView,
)
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.rules.models import RuleKind, RuleOutcome
from app.db.models.batch import ExpenseRow, FileVersion
from app.db.models.detection import DetectionRun
from app.db.models.grading import GradingConfig, GradingItem, GradingItemRow, GradingRun
from app.db.models.validation import ValidationRun, ValidationRunStatus


async def get_grading_run(
    db: AsyncSession, *, tenant_id: uuid.UUID, grading_run_id: uuid.UUID
) -> GradingRunView:
    run = await _get_run(db, tenant_id=tenant_id, grading_run_id=grading_run_id)
    return grading_run_view(run, reused_existing=True)


async def get_batch_grading(
    db: AsyncSession, *, tenant_id: uuid.UUID, file_version_id: uuid.UUID
) -> BatchGradingView:
    file_exists = await db.scalar(
        select(FileVersion.id).where(
            FileVersion.id == file_version_id,
            FileVersion.tenant_id == tenant_id,
        )
    )
    if file_exists is None:
        raise GradingNotFoundError(code="GRADING_FILE_NOT_FOUND", message="批次不存在")

    current_config = await db.scalar(
        select(GradingConfig)
        .where(GradingConfig.tenant_id == tenant_id)
        .order_by(GradingConfig.version.desc(), GradingConfig.id.desc())
        .limit(1)
    )
    if current_config is not None:
        config_from_record(current_config)
    current_validation_id = await db.scalar(
        select(ValidationRun.id)
        .where(
            ValidationRun.tenant_id == tenant_id,
            ValidationRun.file_version_id == file_version_id,
            ValidationRun.status == ValidationRunStatus.COMPLETED,
        )
        .order_by(ValidationRun.created_at.desc(), ValidationRun.id.desc())
        .limit(1)
    )
    current_detection_id = await db.scalar(
        select(DetectionRun.id)
        .where(
            DetectionRun.tenant_id == tenant_id,
            DetectionRun.file_version_id == file_version_id,
        )
        .order_by(DetectionRun.created_at.desc(), DetectionRun.id.desc())
        .limit(1)
    )
    run = await db.scalar(
        select(GradingRun)
        .where(
            GradingRun.tenant_id == tenant_id,
            GradingRun.file_version_id == file_version_id,
        )
        .order_by(GradingRun.created_at.desc(), GradingRun.id.desc())
        .limit(1)
    )
    run_view = grading_run_view(run, reused_existing=True) if run is not None else None
    return BatchGradingView(
        file_version_id=file_version_id,
        run=run_view,
        current_config_id=current_config.id if current_config is not None else None,
        current_validation_run_id=current_validation_id,
        current_detection_run_id=current_detection_id,
        config_stale=(
            run is not None
            and (current_config is None or run.grading_config_id != current_config.id)
        ),
        validation_run_stale=(run is not None and run.validation_run_id != current_validation_id),
        detection_run_stale=(run is not None and run.detection_run_id != current_detection_id),
    )


async def list_grading_items(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    grading_run_id: uuid.UUID,
    source_kind: SourceKind | None = None,
    rule_kind: RuleKind | None = None,
    detector: DetectorKind | None = None,
    severity_impact: int | None = None,
    severity_confidence: int | None = None,
    disposition: Disposition | None = None,
    f7_outcome: InvestigationGradingOutcome | None = None,
    sort_by: str = "default",
    limit: int = 50,
    offset: int = 0,
) -> GradingItemPage:
    _validate_page(limit=limit, offset=offset)
    if sort_by != "default":
        raise GradingInputError(code="GRADING_QUERY_INVALID", message="排序参数无效")
    _validate_level(severity_impact)
    _validate_level(severity_confidence)
    await _get_run(db, tenant_id=tenant_id, grading_run_id=grading_run_id)

    filters: list[ColumnElement[bool]] = [
        GradingItem.tenant_id == tenant_id,
        GradingItem.grading_run_id == grading_run_id,
    ]
    if source_kind is not None:
        filters.append(GradingItem.source_kind == source_kind.value)
    if rule_kind is not None:
        filters.append(GradingItem.rule_kind == rule_kind.value)
    if detector is not None:
        filters.append(GradingItem.detector == detector.value)
    if severity_impact is not None:
        filters.append(GradingItem.severity_impact == severity_impact)
    if severity_confidence is not None:
        filters.append(GradingItem.severity_confidence == severity_confidence)
    if disposition is not None:
        filters.append(GradingItem.disposition == disposition.value)
    if f7_outcome is not None:
        filters.append(GradingItem.f7_outcome == f7_outcome.value)

    total = int(await db.scalar(select(func.count()).select_from(GradingItem).where(*filters)) or 0)
    records = tuple(
        (
            await db.scalars(
                select(GradingItem)
                .where(*filters)
                .order_by(
                    _enum_order(GradingItem.disposition, DISPOSITION_ORDER),
                    GradingItem.severity_impact.desc(),
                    GradingItem.severity_confidence.desc(),
                    GradingItem.first_row_no,
                    _enum_order(GradingItem.source_kind, SOURCE_KIND_ORDER),
                    func.coalesce(GradingItem.finding_id, GradingItem.correlation_finding_id),
                    GradingItem.id,
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return GradingItemPage(
        items=tuple(grading_item_view(record) for record in records),
        total=total,
        limit=limit,
        offset=offset,
    )


async def get_grading_item(
    db: AsyncSession, *, tenant_id: uuid.UUID, grading_item_id: uuid.UUID
) -> GradingItemView:
    item = await db.scalar(
        select(GradingItem).where(
            GradingItem.id == grading_item_id,
            GradingItem.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise GradingNotFoundError(code="GRADING_ITEM_NOT_FOUND", message="二维分级项目不存在")
    return grading_item_view(item)


async def list_grading_item_rows(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    grading_item_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> GradingRowPage:
    _validate_page(limit=limit, offset=offset)
    item = await db.scalar(
        select(GradingItem).where(
            GradingItem.id == grading_item_id,
            GradingItem.tenant_id == tenant_id,
        )
    )
    if item is None:
        raise GradingNotFoundError(code="GRADING_ITEM_NOT_FOUND", message="二维分级项目不存在")
    filters = (
        GradingItemRow.tenant_id == tenant_id,
        GradingItemRow.grading_item_id == grading_item_id,
    )
    total = int(
        await db.scalar(select(func.count()).select_from(GradingItemRow).where(*filters)) or 0
    )
    records = (
        await db.execute(
            select(GradingItemRow, ExpenseRow)
            .join(
                ExpenseRow,
                (ExpenseRow.tenant_id == GradingItemRow.tenant_id)
                & (ExpenseRow.file_version_id == GradingItemRow.file_version_id)
                & (ExpenseRow.row_no == GradingItemRow.row_no),
            )
            .where(*filters)
            .order_by(
                GradingItemRow.ordinal,
                GradingItemRow.row_no,
                GradingItemRow.id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    views: list[GradingRowView] = []
    for link, expense in records:
        try:
            normalized = NormalizedExpenseRecord.model_validate(expense.normalized_json)
        except ValidationError as exc:
            raise GradingInternalError(
                code="GRADING_SOURCE_ROW_CORRUPT", message="二维分级来源行快照无效"
            ) from exc
        actual_fingerprint = source_row_fingerprint(
            tenant_id=tenant_id,
            file_version_id=link.file_version_id,
            row_no=link.row_no,
            normalized=normalized,
        )
        if actual_fingerprint != link.source_row_fingerprint:
            raise GradingInternalError(
                code="GRADING_SOURCE_ROW_DRIFT", message="二维分级来源行身份不一致"
            )
        views.append(
            GradingRowView(
                id=link.id,
                tenant_id=link.tenant_id,
                grading_run_id=link.grading_run_id,
                grading_item_id=link.grading_item_id,
                file_version_id=link.file_version_id,
                row_no=link.row_no,
                ordinal=link.ordinal,
                source_row_fingerprint=link.source_row_fingerprint,
                raw=dict(expense.raw_json),
                normalized=normalized,
                created_at=link.created_at,
            )
        )
    return GradingRowPage(items=tuple(views), total=total, limit=limit, offset=offset)


async def _get_run(
    db: AsyncSession, *, tenant_id: uuid.UUID, grading_run_id: uuid.UUID
) -> GradingRun:
    run = await db.scalar(
        select(GradingRun).where(
            GradingRun.id == grading_run_id,
            GradingRun.tenant_id == tenant_id,
        )
    )
    if run is None:
        raise GradingNotFoundError(code="GRADING_RUN_NOT_FOUND", message="二维分级运行不存在")
    if run.status != "completed" or run.completed_at is None:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级运行快照无效")
    return run


def grading_run_view(run: GradingRun, *, reused_existing: bool) -> GradingRunView:
    if run.completed_at is None:
        raise GradingInternalError(code="GRADING_RUN_CORRUPT", message="二维分级运行快照无效")
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


def grading_item_view(item: GradingItem) -> GradingItemView:
    try:
        reasons = tuple(GradingReasonCode(value) for value in item.reason_codes_json)
        return GradingItemView(
            id=item.id,
            tenant_id=item.tenant_id,
            grading_run_id=item.grading_run_id,
            file_version_id=item.file_version_id,
            source_kind=SourceKind(item.source_kind),
            finding_id=item.finding_id,
            correlation_finding_id=item.correlation_finding_id,
            investigation_run_id=item.investigation_run_id,
            rule_kind=RuleKind(item.rule_kind) if item.rule_kind is not None else None,
            detector=DetectorKind(item.detector) if item.detector is not None else None,
            f3_outcome=RuleOutcome(item.f3_outcome) if item.f3_outcome is not None else None,
            f6_capability_status=(
                CapabilityStatus(item.f6_capability_status)
                if item.f6_capability_status is not None
                else None
            ),
            f7_outcome=(
                InvestigationGradingOutcome(item.f7_outcome)
                if item.f7_outcome is not None
                else None
            ),
            severity_impact=item.severity_impact,
            severity_confidence=item.severity_confidence,
            disposition=Disposition(item.disposition),
            reason_codes=reasons,
            evidence_snapshot=dict(item.evidence_snapshot),
            item_fingerprint=item.item_fingerprint,
            first_row_no=item.first_row_no,
            created_at=item.created_at,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise GradingInternalError(
            code="GRADING_ITEM_CORRUPT", message="二维分级项目快照无效"
        ) from exc


def _validate_page(*, limit: int, offset: int) -> None:
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 200 or offset < 0:
        raise GradingInputError(code="GRADING_QUERY_INVALID", message="分页参数无效")


def _validate_level(value: int | None) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= 3):
        raise GradingInputError(code="GRADING_QUERY_INVALID", message="分级过滤参数无效")


def _enum_order(
    column: InstrumentedAttribute[object], values: tuple[object, ...]
) -> ColumnElement[int]:
    return case(
        *[
            (column == str(getattr(value, "value", value)), rank)
            for rank, value in enumerate(values)
        ],
        else_=len(values),
    )
