"""F3 校验摘要与 finding 的只读查询服务。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.rules import RowVerdict
from app.db.models.batch import FileVersion, RowResult
from app.db.models.findings import Finding
from app.db.models.validation import ValidationRun, ValidationRunStatus


@dataclass(frozen=True)
class FindingView:
    id: uuid.UUID
    row_no: int
    rule_id: str
    rule_version: str | None
    rule_kind: str
    outcome: str
    reason_code: str
    reasoning: str
    evidence: dict[str, Any]
    verdict: RowVerdict


@dataclass(frozen=True)
class FindingsPage:
    file_version_id: uuid.UUID
    total: int
    page: int
    page_size: int
    items: tuple[FindingView, ...]


async def get_validation_summary(db: AsyncSession, file_version_id: uuid.UUID) -> ValidationRun:
    await _require_batch(db, file_version_id)
    run = await db.scalar(
        select(ValidationRun).where(
            ValidationRun.file_version_id == file_version_id,
            ValidationRun.status == ValidationRunStatus.COMPLETED,
        )
    )
    if run is None:
        raise NotFoundError(code="VALIDATION_NOT_FOUND", message="批次尚未完成首次校验")
    return run


async def list_findings(
    db: AsyncSession,
    *,
    file_version_id: uuid.UUID,
    page: int,
    page_size: int,
    verdict: Literal["flagged", "manual_review"] | None,
) -> FindingsPage:
    run = await get_validation_summary(db, file_version_id)
    filters = [Finding.validation_run_id == run.id]
    if verdict is not None:
        filters.append(RowResult.verdict == verdict)
    joined = (
        select(Finding, RowResult.verdict)
        .join(
            RowResult,
            (RowResult.file_version_id == Finding.file_version_id)
            & (RowResult.row_no == Finding.row_no),
        )
        .where(*filters)
    )
    total = int(
        await db.scalar(select(func.count()).select_from(joined.order_by(None).subquery())) or 0
    )
    rows = tuple(
        (
            await db.execute(
                joined.order_by(
                    Finding.row_no,
                    Finding.rule_id,
                    Finding.rule_version.nullsfirst(),
                    Finding.kind,
                    Finding.id,
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    items: list[FindingView] = []
    for finding, row_verdict in rows:
        evidence = dict(finding.evidence_json or {})
        items.append(
            FindingView(
                id=finding.id,
                row_no=finding.row_no,
                rule_id=finding.rule_id,
                rule_version=finding.rule_version,
                rule_kind=finding.rule_kind.value,
                outcome=str(evidence.get("outcome", "")),
                reason_code=finding.kind,
                reasoning=finding.reasoning or "",
                evidence=evidence,
                verdict=RowVerdict(row_verdict),
            )
        )
    return FindingsPage(file_version_id, total, page, page_size, tuple(items))


async def _require_batch(db: AsyncSession, file_version_id: uuid.UUID) -> FileVersion:
    batch = await db.get(FileVersion, file_version_id)
    if batch is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    return batch
