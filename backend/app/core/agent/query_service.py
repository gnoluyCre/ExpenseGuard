"""Tenant-scoped read services for immutable F7 investigation facts."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent.service_models import (
    EvidenceStepPage,
    EvidenceStepView,
    InvestigationDetail,
    InvestigationNotFoundError,
    InvestigationPage,
    InvestigationResultView,
    InvestigationRunView,
)
from app.db.models.findings import EvidenceStep
from app.db.models.investigation import InvestigationResult, InvestigationRun


async def get_investigation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    investigation_run_id: uuid.UUID,
    reused_existing: bool = False,
) -> InvestigationDetail:
    run = await db.scalar(
        select(InvestigationRun).where(
            InvestigationRun.id == investigation_run_id,
            InvestigationRun.tenant_id == tenant_id,
        )
    )
    if run is None:
        raise InvestigationNotFoundError(code="INVESTIGATION_NOT_FOUND", message="调查记录不存在")
    steps = tuple(
        _step_view(step)
        for step in (
            await db.scalars(
                select(EvidenceStep)
                .where(
                    EvidenceStep.tenant_id == tenant_id,
                    EvidenceStep.investigation_run_id == investigation_run_id,
                )
                .order_by(EvidenceStep.step_no, EvidenceStep.id)
            )
        ).all()
    )
    _validate_contiguous_steps(steps, max_steps=run.max_steps)
    result = await db.scalar(
        select(InvestigationResult).where(
            InvestigationResult.tenant_id == tenant_id,
            InvestigationResult.investigation_run_id == investigation_run_id,
        )
    )
    return InvestigationDetail(
        run=_run_view(run),
        steps=steps,
        result=_result_view(result) if result is not None else None,
        reused_existing=reused_existing,
    )


async def list_candidate_investigations(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    limit: int,
    offset: int,
) -> InvestigationPage:
    _validate_page(limit=limit, offset=offset)
    filters = (
        InvestigationRun.tenant_id == tenant_id,
        InvestigationRun.detection_run_id == detection_run_id,
        InvestigationRun.correlation_finding_id == correlation_finding_id,
    )
    total = int(
        await db.scalar(select(func.count()).select_from(InvestigationRun).where(*filters)) or 0
    )
    runs = (
        await db.scalars(
            select(InvestigationRun)
            .where(*filters)
            .order_by(InvestigationRun.created_at.desc(), InvestigationRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items: list[InvestigationDetail] = []
    for run in runs:
        items.append(
            await get_investigation(
                db,
                tenant_id=tenant_id,
                investigation_run_id=run.id,
            )
        )
    return InvestigationPage(items=tuple(items), total=total, limit=limit, offset=offset)


async def list_investigation_steps(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    investigation_run_id: uuid.UUID,
    limit: int,
    offset: int,
) -> EvidenceStepPage:
    _validate_page(limit=limit, offset=offset)
    exists = await db.scalar(
        select(InvestigationRun.id).where(
            InvestigationRun.id == investigation_run_id,
            InvestigationRun.tenant_id == tenant_id,
        )
    )
    if exists is None:
        raise InvestigationNotFoundError(code="INVESTIGATION_NOT_FOUND", message="调查记录不存在")
    filters = (
        EvidenceStep.tenant_id == tenant_id,
        EvidenceStep.investigation_run_id == investigation_run_id,
    )
    total = int(
        await db.scalar(select(func.count()).select_from(EvidenceStep).where(*filters)) or 0
    )
    rows = (
        await db.scalars(
            select(EvidenceStep)
            .where(*filters)
            .order_by(EvidenceStep.step_no, EvidenceStep.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return EvidenceStepPage(
        items=tuple(_step_view(step) for step in rows),
        total=total,
        limit=limit,
        offset=offset,
    )


def _run_view(run: InvestigationRun) -> InvestigationRunView:
    return InvestigationRunView(
        id=run.id,
        tenant_id=run.tenant_id,
        correlation_finding_id=run.correlation_finding_id,
        detection_run_id=run.detection_run_id,
        file_version_id=run.file_version_id,
        actor_id=run.actor_id,
        provider_kind=run.provider_kind.value
        if hasattr(run.provider_kind, "value")
        else str(run.provider_kind),
        provider_model=run.provider_model,
        max_steps=run.max_steps,
        timeout_seconds=run.timeout_seconds,
        redaction_version=run.redaction_version,
        agent_version=run.agent_version,
        action_schema_version=run.action_schema_version,
        prompt_template_version=run.prompt_template_version,
        input_fingerprint=run.input_fingerprint,
        config_fingerprint=run.config_fingerprint,
        created_at=run.created_at,
    )


def _step_view(step: EvidenceStep) -> EvidenceStepView:
    return EvidenceStepView(
        id=step.id,
        investigation_run_id=step.investigation_run_id,
        tenant_id=step.tenant_id,
        correlation_finding_id=step.correlation_finding_id,
        detection_run_id=step.detection_run_id,
        file_version_id=step.file_version_id,
        step_no=step.step_no,
        action_kind=step.action_kind.value
        if hasattr(step.action_kind, "value")
        else str(step.action_kind),
        model_action=dict(step.model_action),
        decision_summary=step.decision_summary,
        payload_fingerprint=step.payload_fingerprint,
        tool_name=step.tool_name,
        tool_input=dict(step.tool_input) if step.tool_input is not None else None,
        tool_output=dict(step.tool_output) if step.tool_output is not None else None,
        created_at=step.created_at,
    )


def _result_view(result: InvestigationResult) -> InvestigationResultView:
    outcome = result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome)
    return InvestigationResultView(
        id=result.id,
        investigation_run_id=result.investigation_run_id,
        tenant_id=result.tenant_id,
        correlation_finding_id=result.correlation_finding_id,
        detection_run_id=result.detection_run_id,
        file_version_id=result.file_version_id,
        outcome=outcome,
        evidence_sufficient=result.evidence_sufficient,
        summary=result.summary,
        reason_code=result.reason_code,
        citations=tuple(dict(item) for item in result.citations_json),
        result_fingerprint=result.result_fingerprint,
        completed_at=result.completed_at,
    )


def _validate_contiguous_steps(steps: tuple[EvidenceStepView, ...], *, max_steps: int) -> None:
    actual = tuple(step.step_no for step in steps)
    expected = tuple(range(1, len(steps) + 1))
    if actual != expected or len(steps) > max_steps:
        from app.core.agent.service_models import InvestigationInternalError

        raise InvestigationInternalError(
            code="INVESTIGATION_STEP_LEDGER_CORRUPT",
            message="调查步骤账本不连续",
        )


def _validate_page(*, limit: int, offset: int) -> None:
    if not 1 <= limit <= 100 or offset < 0:
        from app.core.agent.service_models import InvestigationInputError

        raise InvestigationInputError(code="INVESTIGATION_PAGE_INVALID", message="分页参数无效")
