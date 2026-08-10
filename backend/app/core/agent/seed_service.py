"""Prepare the bounded, redacted starting state for one F7 investigation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.agent.sanitization import PiiTokenDraft, sanitize_expense_row
from app.core.agent.service_models import (
    InvestigationNotFoundError,
    InvestigationSeed,
    InvestigationServiceError,
)
from app.core.agent.tools import ToolContext
from app.core.detection.canonical import canonical_sha256
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow
from app.db.models.detection import CorrelationFindingRow
from app.db.models.findings import CorrelationFinding

INITIAL_ROW_LIMIT = 50


@dataclass(frozen=True)
class PreparedInvestigationSeed:
    file_version_id: uuid.UUID
    seed: InvestigationSeed


async def prepare_investigation_seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    pii_secret: bytes,
    token_version: int,
) -> PreparedInvestigationSeed:
    """Read and redact candidate facts in a transaction that ends before model I/O."""

    if len(pii_secret) < 32:
        raise InvestigationServiceError(
            code="INVESTIGATION_REDACTION_UNAVAILABLE",
            message="PII 脱敏密钥尚未安全配置",
        )
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        try:
            candidate = await db.scalar(
                select(CorrelationFinding).where(
                    CorrelationFinding.id == correlation_finding_id,
                    CorrelationFinding.detection_run_id == detection_run_id,
                    CorrelationFinding.tenant_id == tenant_id,
                )
            )
            if candidate is None:
                raise InvestigationNotFoundError(
                    code="CORRELATION_FINDING_NOT_FOUND",
                    message="关联候选不存在",
                )
            linked_rows = tuple(
                (
                    await db.execute(
                        select(CorrelationFindingRow, ExpenseRow)
                        .join(
                            ExpenseRow,
                            (ExpenseRow.file_version_id == CorrelationFindingRow.file_version_id)
                            & (ExpenseRow.row_no == CorrelationFindingRow.row_no)
                            & (ExpenseRow.tenant_id == CorrelationFindingRow.tenant_id),
                        )
                        .where(
                            CorrelationFindingRow.tenant_id == tenant_id,
                            CorrelationFindingRow.detection_run_id == detection_run_id,
                            CorrelationFindingRow.finding_id == correlation_finding_id,
                            CorrelationFindingRow.file_version_id == candidate.file_version_id,
                        )
                        .order_by(
                            CorrelationFindingRow.ordinal,
                            CorrelationFindingRow.row_no,
                            CorrelationFindingRow.id,
                        )
                    )
                ).all()
            )
            file_version_id = candidate.file_version_id
            detector = (
                candidate.detector.value
                if hasattr(candidate.detector, "value")
                else str(candidate.detector)
            )
            detector_version = candidate.detector_version
            evidence_fingerprint = canonical_sha256(candidate.evidence_json)
            row_snapshots = tuple(
                (
                    link.ordinal,
                    row.row_no,
                    dict(row.normalized_json) if row.normalized_json is not None else None,
                    row.parse_error_code,
                )
                for link, row in (item._tuple() for item in linked_rows)
            )
        finally:
            await db.rollback()

    if not row_snapshots:
        raise InvestigationServiceError(
            code="INVESTIGATION_PARTICIPANT_ROWS_MISSING",
            message="关联候选缺少物理参与行",
        )

    sanitized_rows: list[dict[str, object]] = []
    drafts_by_token: dict[str, PiiTokenDraft] = {}
    employee_tokens: set[str] = set()
    supplier_tokens: set[str] = set()
    for ordinal, row_no, normalized_json, parse_error_code in row_snapshots:
        result = sanitize_expense_row(
            tenant_id=tenant_id,
            row_no=row_no,
            record=_normalized_record(
                normalized_json=normalized_json,
                parse_error_code=parse_error_code,
            ),
            secret=pii_secret,
            token_version=token_version,
        )
        for draft in result.token_drafts:
            drafts_by_token[draft.token] = draft
        if result.row.employee_token is not None:
            employee_tokens.add(result.row.employee_token)
        if result.row.supplier_token is not None:
            supplier_tokens.add(result.row.supplier_token)
        if len(sanitized_rows) < INITIAL_ROW_LIMIT:
            sanitized_rows.append(
                {
                    "ordinal": ordinal,
                    "row": result.row.model_dump(mode="json"),
                }
            )

    evidence = {
        "candidate": {
            "detection_run_id": str(detection_run_id),
            "correlation_finding_id": str(correlation_finding_id),
            "file_version_id": str(file_version_id),
        },
        "detector": detector,
        "detector_version": detector_version,
        "evidence_fingerprint": evidence_fingerprint,
        "participant_rows": sanitized_rows,
        "participant_row_count": len(row_snapshots),
        "participant_rows_truncated": len(row_snapshots) > INITIAL_ROW_LIMIT,
    }
    return PreparedInvestigationSeed(
        file_version_id=file_version_id,
        seed=InvestigationSeed(
            evidence=evidence,
            tool_context=ToolContext(
                tenant_id=tenant_id,
                detection_run_id=detection_run_id,
                correlation_finding_id=correlation_finding_id,
                allowed_employee_tokens=frozenset(employee_tokens),
                allowed_supplier_tokens=frozenset(supplier_tokens),
            ),
            token_drafts=tuple(drafts_by_token[token] for token in sorted(drafts_by_token)),
        ),
    )


def _normalized_record(
    *, normalized_json: dict[str, object] | None, parse_error_code: str | None
) -> NormalizedExpenseRecord:
    if normalized_json is None or parse_error_code is not None:
        raise InvestigationServiceError(
            code="INVESTIGATION_ROW_UNAVAILABLE",
            message="调查参与行缺少可用的规范化记录",
        )
    try:
        return NormalizedExpenseRecord.model_validate(normalized_json)
    except ValueError as exc:
        raise InvestigationServiceError(
            code="INVESTIGATION_ROW_INVALID",
            message="调查参与行的规范化记录无效",
        ) from exc
