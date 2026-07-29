"""Read-only report queries and stable pagination over immutable snapshots."""

from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.reports.models import ParseErrorSnapshot, ReportItemSnapshot, ReportSnapshot
from app.core.reports.service import load_report_snapshot
from app.db.models.reports import (
    ReportAttentionGroup,
    ReportCitationStatus,
    ReportRun,
    ReportRunStatus,
)


class ReportItemSort(StrEnum):
    DEFAULT = "default"
    ROW_NO = "row_no"
    RULE_ID = "rule_id"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class ReportItemPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ReportItemSnapshot, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class ParseErrorPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[ParseErrorSnapshot, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


async def load_report_for_file(db: AsyncSession, *, file_version_id: uuid.UUID) -> ReportSnapshot:
    report_id = await db.scalar(
        select(ReportRun.id).where(
            ReportRun.file_version_id == file_version_id,
            ReportRun.status == ReportRunStatus.COMPLETED,
        )
    )
    if report_id is None:
        raise NotFoundError(code="REPORT_NOT_FOUND", message="该批次尚未生成报告")
    return await load_report_snapshot(db, report_run_id=report_id)


async def list_report_items(
    db: AsyncSession,
    *,
    report_run_id: uuid.UUID,
    attention_group: ReportAttentionGroup | None,
    citation_status: ReportCitationStatus | None,
    sort_by: ReportItemSort,
    direction: SortDirection,
    limit: int,
    offset: int,
) -> ReportItemPage:
    snapshot = await load_report_snapshot(db, report_run_id=report_run_id)
    items = [
        item
        for item in snapshot.items
        if (attention_group is None or item.attention_group is attention_group)
        and (citation_status is None or item.citation_status is citation_status)
    ]
    if sort_by is ReportItemSort.ROW_NO:
        items.sort(key=lambda item: (item.row_no, item.rule_id, str(item.finding_id)))
    elif sort_by is ReportItemSort.RULE_ID:
        items.sort(key=lambda item: (item.rule_id, item.row_no, str(item.finding_id)))
    if direction is SortDirection.DESC:
        items.reverse()
    return ReportItemPage(
        items=tuple(items[offset : offset + limit]),
        total=len(items),
        limit=limit,
        offset=offset,
    )


async def list_report_parse_errors(
    db: AsyncSession,
    *,
    report_run_id: uuid.UUID,
    limit: int,
    offset: int,
) -> ParseErrorPage:
    snapshot = await load_report_snapshot(db, report_run_id=report_run_id)
    errors = snapshot.parse_errors
    return ParseErrorPage(
        items=errors[offset : offset + limit],
        total=len(errors),
        limit=limit,
        offset=offset,
    )
