"""Replay-safe, short-transaction orchestration for CP-F7.3 investigations."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import func, null, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.agent.models import InvestigationOutcome as DomainOutcome
from app.core.agent.models import TerminateAction
from app.core.agent.provider import LlmProvider, LlmProviderError
from app.core.agent.query_service import get_investigation
from app.core.agent.sanitization import PiiTokenDraft
from app.core.agent.service_models import (
    CheckpointReconciler,
    EvidenceStepView,
    InvestigationDetail,
    InvestigationInputError,
    InvestigationInternalError,
    InvestigationNotFoundError,
    InvestigationRunOptions,
    InvestigationRunView,
    InvestigationSeed,
    InvestigationServiceError,
    NoopCheckpointReconciler,
    ReconciliationSnapshot,
)
from app.core.agent.step_runner import StepExecution, execute_agent_step
from app.core.agent.tools import ReadOnlyToolBackend, ToolExecutionError
from app.core.detection.canonical import canonical_sha256
from app.core.observability.model_usage import ModelPricing, record_model_usage
from app.core.policies.citations import CitationVerificationError, verify_exact_quote
from app.core.security.auth_service import write_audit
from app.core.tenancy.scope import bind_tenant
from app.db.models.findings import CorrelationFinding, EvidenceStep
from app.db.models.investigation import (
    InvestigationRequest,
    InvestigationResult,
    InvestigationRun,
    PiiToken,
)
from app.db.models.policy import PolicyClause
from app.db.models.tenancy import AppUser

FaultHook = Callable[[str], None]
StopRequested = Callable[[], bool]


async def run_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    file_version_id: uuid.UUID,
    idempotency_key: str,
    options: InvestigationRunOptions,
    seed: InvestigationSeed,
    provider: LlmProvider | None,
    tools: ReadOnlyToolBackend,
    reconciler: CheckpointReconciler | None = None,
    unavailable_reason_code: str = "PROVIDER_UNAVAILABLE",
    fault_hook: FaultHook | None = None,
    stop_requested: StopRequested | None = None,
    model_pricing: ModelPricing | None = None,
) -> InvestigationDetail:
    """Create, resume, or replay one explicit F6-candidate investigation.

    No transaction spans a provider or tool call. Public-schema business facts
    are authoritative; an optional checkpoint adapter only reconciles to them.
    """

    _validate_seed_identity(
        seed=seed,
        tenant_id=tenant_id,
        detection_run_id=detection_run_id,
        correlation_finding_id=correlation_finding_id,
    )
    key_hash = _idempotency_key_hash(idempotency_key)
    request_fingerprint = _request_fingerprint(
        tenant_id=tenant_id,
        actor_id=actor_id,
        detection_run_id=detection_run_id,
        correlation_finding_id=correlation_finding_id,
        file_version_id=file_version_id,
    )
    try:
        detail = await _create_or_load_run(
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            detection_run_id=detection_run_id,
            correlation_finding_id=correlation_finding_id,
            file_version_id=file_version_id,
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
            options=options,
            evidence=seed.evidence,
            token_drafts=seed.token_drafts,
            fault_hook=fault_hook,
        )
    except IntegrityError:
        # A concurrent creator may win the tenant/key unique constraint after
        # our initial lookup. Re-read the committed ledger and apply the same
        # fingerprint rule as an ordinary replay.
        detail = await _load_keyed_request(
            session_factory,
            tenant_id=tenant_id,
            key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
    if detail.result is not None:
        return detail

    run = detail.run
    expected_input = _input_fingerprint(
        run=run,
        evidence=seed.evidence,
        token_drafts=seed.token_drafts,
    )
    if expected_input != run.input_fingerprint:
        raise InvestigationServiceError(
            code="INVESTIGATION_INPUT_DRIFT",
            message="调查输入与已创建运行的冻结身份不一致",
        )

    checkpoint = reconciler or NoopCheckpointReconciler()
    await checkpoint.reconcile(
        ReconciliationSnapshot(
            investigation_run_id=run.id,
            committed_steps=detail.steps,
            terminal=False,
        )
    )

    resumed_terminal = await _finish_committed_terminal(
        session_factory,
        run=run,
        actor_id=actor_id,
        steps=detail.steps,
        fault_hook=fault_hook,
    )
    if resumed_terminal is not None:
        await checkpoint.reconcile(
            ReconciliationSnapshot(
                investigation_run_id=run.id,
                committed_steps=resumed_terminal.steps,
                terminal=True,
            )
        )
        return resumed_terminal

    if len(detail.steps) >= run.max_steps:
        return await _finish(
            session_factory,
            run=run,
            actor_id=actor_id,
            outcome="max_steps",
            evidence_sufficient=None,
            summary="调查达到最大步骤仍未形成充分性判断，已转人工处理",
            reason_code="MAX_STEPS_REACHED",
            citations=(),
            fault_hook=fault_hook,
        )

    if provider is None:
        return await _finish(
            session_factory,
            run=run,
            actor_id=actor_id,
            outcome="unavailable",
            evidence_sufficient=None,
            summary="异常取证模型未配置，已转人工处理",
            reason_code=_safe_reason_code(unavailable_reason_code),
            citations=(),
            fault_hook=fault_hook,
        )

    if stop_requested is not None and stop_requested():
        return await _finish(
            session_factory,
            run=run,
            actor_id=actor_id,
            outcome="failed",
            evidence_sufficient=None,
            summary="服务正在安全退出，调查已在持久化步骤边界转人工处理",
            reason_code="SHUTDOWN_REQUESTED",
            citations=(),
            fault_hook=fault_hook,
        )

    try:
        while True:
            detail = await _load_detail(session_factory, tenant_id=tenant_id, run_id=run.id)
            if detail.result is not None:
                return _with_reused(detail, reused=True)
            if len(detail.steps) >= run.max_steps:
                return await _finish(
                    session_factory,
                    run=run,
                    actor_id=actor_id,
                    outcome="max_steps",
                    evidence_sufficient=None,
                    summary="调查达到最大步骤仍未形成充分性判断，已转人工处理",
                    reason_code="MAX_STEPS_REACHED",
                    citations=(),
                    fault_hook=fault_hook,
                )

            execution = await execute_agent_step(
                provider=provider,
                tools=tools,
                context=seed.tool_context,
                step_no=len(detail.steps) + 1,
                evidence=seed.evidence,
                prior_steps=tuple(step.prompt_fact() for step in detail.steps),
            )
            persisted = await _append_step(
                session_factory,
                run=run,
                execution=execution,
                fault_hook=fault_hook,
            )
            _fault(fault_hook, "step_committed")
            await checkpoint.reconcile(
                ReconciliationSnapshot(
                    investigation_run_id=run.id,
                    committed_steps=(*detail.steps, persisted),
                    terminal=False,
                )
            )
            record_model_usage(
                investigation_run_id=run.id,
                file_version_id=run.file_version_id,
                step_no=persisted.step_no,
                provider=run.provider_kind,
                model=run.provider_model,
                duration_ms=execution.provider_duration_ms,
                usage=execution.provider_response.usage,
                pricing=model_pricing or ModelPricing(),
            )
            if execution.terminal is None:
                if stop_requested is not None and stop_requested():
                    return await _finish(
                        session_factory,
                        run=run,
                        actor_id=actor_id,
                        outcome="failed",
                        evidence_sufficient=None,
                        summary="服务正在安全退出，调查已在持久化步骤边界转人工处理",
                        reason_code="SHUTDOWN_REQUESTED",
                        citations=(),
                        fault_hook=fault_hook,
                    )
                continue
            terminal = execution.terminal
            citations = await _verify_citations(
                session_factory,
                tenant_id=tenant_id,
                clause_id=terminal.clause_id,
                quote=terminal.quote,
            )
            finished = await _finish(
                session_factory,
                run=run,
                actor_id=actor_id,
                outcome=terminal.outcome.value,
                evidence_sufficient=terminal.evidence_sufficient,
                summary=terminal.summary,
                reason_code=terminal.reason_code,
                citations=citations,
                fault_hook=fault_hook,
            )
            await checkpoint.reconcile(
                ReconciliationSnapshot(
                    investigation_run_id=run.id,
                    committed_steps=finished.steps,
                    terminal=True,
                )
            )
            return finished
    except (LlmProviderError, ToolExecutionError, CitationVerificationError) as exc:
        reason_code = getattr(exc, "code", "INVESTIGATION_DEPENDENCY_FAILED")
        return await _finish(
            session_factory,
            run=run,
            actor_id=actor_id,
            outcome="failed",
            evidence_sufficient=None,
            summary="模型或只读取证工具失败，已转人工处理",
            reason_code=_safe_reason_code(reason_code),
            citations=(),
            fault_hook=fault_hook,
        )
    except IntegrityError as exc:
        await _write_safe_failure_audit(
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            run_id=run.id,
            detection_run_id=detection_run_id,
            correlation_finding_id=correlation_finding_id,
        )
        raise InvestigationInternalError(
            code="INVESTIGATION_INTEGRITY_ERROR",
            message="调查事实完整性校验失败",
        ) from exc
    except InvestigationServiceError:
        raise
    except Exception as exc:
        try:
            return await _finish(
                session_factory,
                run=run,
                actor_id=actor_id,
                outcome="failed",
                evidence_sufficient=None,
                summary="异常取证发生内部错误，已转人工处理",
                reason_code="INTERNAL_ERROR",
                citations=(),
                fault_hook=None,
            )
        except Exception:
            await _write_safe_failure_audit(
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                run_id=run.id,
                detection_run_id=detection_run_id,
                correlation_finding_id=correlation_finding_id,
            )
            raise InvestigationInternalError() from exc


async def _create_or_load_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    file_version_id: uuid.UUID,
    key_hash: str,
    request_fingerprint: str,
    options: InvestigationRunOptions,
    evidence: dict[str, Any],
    token_drafts: tuple[PiiTokenDraft, ...],
    fault_hook: FaultHook | None,
) -> InvestigationDetail:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        async with db.begin():
            keyed = await db.scalar(
                select(InvestigationRequest).where(
                    InvestigationRequest.tenant_id == tenant_id,
                    InvestigationRequest.idempotency_key_hash == key_hash,
                )
            )
            if keyed is not None:
                if keyed.request_fingerprint != request_fingerprint:
                    raise InvestigationServiceError(
                        code="IDEMPOTENCY_KEY_REUSED",
                        message="该 Idempotency-Key 已绑定其他调查请求",
                    )
                return await get_investigation(
                    db,
                    tenant_id=tenant_id,
                    investigation_run_id=keyed.investigation_run_id,
                    reused_existing=True,
                )

            actor = await db.scalar(
                select(AppUser).where(
                    AppUser.id == actor_id,
                    AppUser.tenant_id == tenant_id,
                    AppUser.is_active.is_(True),
                )
            )
            if actor is None:
                raise InvestigationInputError(
                    code="INVESTIGATION_ACTOR_INVALID", message="操作用户无效"
                )
            finding = await db.scalar(
                select(CorrelationFinding).where(
                    CorrelationFinding.id == correlation_finding_id,
                    CorrelationFinding.detection_run_id == detection_run_id,
                    CorrelationFinding.file_version_id == file_version_id,
                    CorrelationFinding.tenant_id == tenant_id,
                )
            )
            if finding is None:
                raise InvestigationNotFoundError(
                    code="CORRELATION_FINDING_NOT_FOUND", message="关联候选不存在"
                )
            _validate_options(options)
            await _persist_token_drafts(
                db,
                tenant_id=tenant_id,
                drafts=token_drafts,
            )
            _fault(fault_hook, "pii_tokens_written")
            config_fingerprint = _config_fingerprint(options)
            provisional = InvestigationRunView(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                correlation_finding_id=correlation_finding_id,
                detection_run_id=detection_run_id,
                file_version_id=file_version_id,
                actor_id=actor_id,
                provider_kind=options.provider_kind,
                provider_model=options.provider_model,
                max_steps=options.max_steps,
                timeout_seconds=options.timeout_seconds,
                redaction_version=options.redaction_version,
                agent_version=options.agent_version,
                action_schema_version=options.action_schema_version,
                prompt_template_version=options.prompt_template_version,
                input_fingerprint="",
                config_fingerprint=config_fingerprint,
                created_at=cast(datetime, await db.scalar(select(func.clock_timestamp()))),
            )
            input_fingerprint = _input_fingerprint(
                run=provisional,
                evidence=evidence,
                token_drafts=token_drafts,
            )
            run = InvestigationRun(
                id=provisional.id,
                tenant_id=tenant_id,
                correlation_finding_id=correlation_finding_id,
                detection_run_id=detection_run_id,
                file_version_id=file_version_id,
                actor_id=actor_id,
                agent_version=options.agent_version,
                action_schema_version=options.action_schema_version,
                prompt_template_version=options.prompt_template_version,
                provider_kind=options.provider_kind,
                provider_model=options.provider_model,
                max_steps=options.max_steps,
                timeout_seconds=options.timeout_seconds,
                redaction_version=options.redaction_version,
                input_fingerprint=input_fingerprint,
                config_fingerprint=config_fingerprint,
            )
            db.add(run)
            await db.flush()
            db.add(
                InvestigationRequest(
                    tenant_id=tenant_id,
                    investigation_run_id=run.id,
                    correlation_finding_id=correlation_finding_id,
                    detection_run_id=detection_run_id,
                    file_version_id=file_version_id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fingerprint,
                )
            )
            await db.flush()
            _fault(fault_hook, "run_request_written")
            detail = await get_investigation(
                db,
                tenant_id=tenant_id,
                investigation_run_id=run.id,
            )
    _fault(fault_hook, "run_request_committed")
    return detail


async def _persist_token_drafts(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    drafts: tuple[PiiTokenDraft, ...],
) -> None:
    """Persist stable tokens in the same transaction as the first run ledger."""

    by_source: dict[tuple[str, str, str], PiiTokenDraft] = {}
    by_token: dict[str, PiiTokenDraft] = {}
    for draft in drafts:
        source_identity = (
            draft.token_version,
            draft.pii_kind.value,
            draft.source_hmac,
        )
        source_match = by_source.get(source_identity)
        token_match = by_token.get(draft.token)
        if source_match is not None and source_match.token != draft.token:
            raise InvestigationServiceError(
                code="PII_TOKEN_COLLISION",
                message="PII token 身份发生冲突，已停止调查",
            )
        if (
            token_match is not None
            and (
                token_match.token_version,
                token_match.pii_kind.value,
                token_match.source_hmac,
            )
            != source_identity
        ):
            raise InvestigationServiceError(
                code="PII_TOKEN_COLLISION",
                message="PII token 身份发生冲突，已停止调查",
            )
        by_source[source_identity] = draft
        by_token[draft.token] = draft

    if not by_source:
        return
    source_hmacs = tuple(identity[2] for identity in by_source)
    tokens = tuple(by_token)
    existing_rows = tuple(
        (
            await db.scalars(
                select(PiiToken).where(
                    PiiToken.tenant_id == tenant_id,
                    or_(
                        PiiToken.source_hmac.in_(source_hmacs),
                        PiiToken.token.in_(tokens),
                    ),
                )
            )
        ).all()
    )
    existing_sources: set[tuple[str, str, str]] = set()
    for existing in existing_rows:
        kind = (
            existing.pii_kind.value
            if hasattr(existing.pii_kind, "value")
            else str(existing.pii_kind)
        )
        existing_identity = (existing.token_version, kind, existing.source_hmac)
        source_draft = by_source.get(existing_identity)
        token_draft = by_token.get(existing.token)
        if source_draft is not None and source_draft.token != existing.token:
            raise InvestigationServiceError(
                code="PII_TOKEN_COLLISION",
                message="PII token 身份发生冲突，已停止调查",
            )
        if (
            token_draft is not None
            and (
                token_draft.token_version,
                token_draft.pii_kind.value,
                token_draft.source_hmac,
            )
            != existing_identity
        ):
            raise InvestigationServiceError(
                code="PII_TOKEN_COLLISION",
                message="PII token 身份发生冲突，已停止调查",
            )
        if source_draft is not None:
            existing_sources.add(existing_identity)

    for source_identity, draft in by_source.items():
        if source_identity in existing_sources:
            continue
        db.add(
            PiiToken(
                tenant_id=tenant_id,
                token_version=draft.token_version,
                pii_kind=draft.pii_kind.value,
                source_hmac=draft.source_hmac,
                token=draft.token,
            )
        )
    await db.flush()


async def _finish_committed_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run: InvestigationRunView,
    actor_id: uuid.UUID,
    steps: tuple[EvidenceStepView, ...],
    fault_hook: FaultHook | None,
) -> InvestigationDetail | None:
    """Recover a committed terminate fact without another provider/tool call."""

    terminal_steps = tuple(step for step in steps if step.action_kind == "terminate")
    if not terminal_steps:
        return None
    if len(terminal_steps) != 1 or terminal_steps[0] is not steps[-1]:
        raise InvestigationInternalError(
            code="INVESTIGATION_STEP_LEDGER_CORRUPT",
            message="调查步骤账本中的终止动作无效",
        )
    step = terminal_steps[0]
    try:
        terminal = TerminateAction.model_validate(step.model_action)
    except ValidationError as exc:
        raise InvestigationInternalError(
            code="INVESTIGATION_STEP_LEDGER_CORRUPT",
            message="调查步骤账本中的终止动作无效",
        ) from exc
    expected_fingerprint = canonical_sha256(
        {
            "schema_version": 1,
            "step_no": step.step_no,
            "action": terminal.model_dump(mode="json"),
        }
    )
    if expected_fingerprint != step.payload_fingerprint:
        raise InvestigationInternalError(
            code="INVESTIGATION_STEP_LEDGER_CORRUPT",
            message="调查步骤账本指纹不一致",
        )
    citations = await _verify_citations(
        session_factory,
        tenant_id=run.tenant_id,
        clause_id=terminal.clause_id,
        quote=terminal.quote,
    )
    return await _finish(
        session_factory,
        run=run,
        actor_id=actor_id,
        outcome=terminal.outcome.value,
        evidence_sufficient=terminal.evidence_sufficient,
        summary=terminal.summary,
        reason_code=terminal.reason_code,
        citations=citations,
        fault_hook=fault_hook,
    )


async def _append_step(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run: InvestigationRunView,
    execution: StepExecution,
    fault_hook: FaultHook | None,
) -> EvidenceStepView:
    draft = execution.draft
    try:
        async with session_factory() as db:
            bind_tenant(db.sync_session, run.tenant_id)
            async with db.begin():
                existing = await db.scalar(
                    select(EvidenceStep).where(
                        EvidenceStep.tenant_id == run.tenant_id,
                        EvidenceStep.investigation_run_id == run.id,
                        EvidenceStep.step_no == draft.step_no,
                    )
                )
                if existing is not None:
                    return _reuse_step(existing, expected_fingerprint=draft.payload_fingerprint)
                step = EvidenceStep(
                    tenant_id=run.tenant_id,
                    investigation_run_id=run.id,
                    correlation_finding_id=run.correlation_finding_id,
                    detection_run_id=run.detection_run_id,
                    file_version_id=run.file_version_id,
                    step_no=draft.step_no,
                    action_kind=draft.action_kind,
                    action_schema_version=run.action_schema_version,
                    model_action=draft.model_action,
                    decision_summary=draft.decision_summary,
                    payload_fingerprint=draft.payload_fingerprint,
                    tool_name=draft.tool_name,
                    tool_input=(
                        draft.tool_input if draft.tool_input is not None else cast(Any, null())
                    ),
                    tool_output=(
                        draft.tool_output if draft.tool_output is not None else cast(Any, null())
                    ),
                )
                db.add(step)
                await db.flush()
                await db.refresh(step)
                _fault(fault_hook, "step_written")
                return _step_view(step)
    except IntegrityError:
        existing = await _load_existing_step(
            session_factory,
            tenant_id=run.tenant_id,
            run_id=run.id,
            step_no=draft.step_no,
        )
        if existing is None:
            raise
        return _reuse_step(existing, expected_fingerprint=draft.payload_fingerprint)


async def _load_existing_step(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    step_no: int,
) -> EvidenceStep | None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        return cast(
            EvidenceStep | None,
            await db.scalar(
                select(EvidenceStep).where(
                    EvidenceStep.tenant_id == tenant_id,
                    EvidenceStep.investigation_run_id == run_id,
                    EvidenceStep.step_no == step_no,
                )
            ),
        )


def _reuse_step(step: EvidenceStep, *, expected_fingerprint: str) -> EvidenceStepView:
    if step.payload_fingerprint != expected_fingerprint:
        raise InvestigationServiceError(
            code="INVESTIGATION_STEP_CONFLICT",
            message="调查步骤与已提交事实冲突",
        )
    return _step_view(step)


async def _finish(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run: InvestigationRunView,
    actor_id: uuid.UUID,
    outcome: str,
    evidence_sufficient: bool | None,
    summary: str,
    reason_code: str,
    citations: tuple[dict[str, Any], ...],
    fault_hook: FaultHook | None,
) -> InvestigationDetail:
    _validate_terminal(outcome=outcome, evidence_sufficient=evidence_sufficient)
    payload = {
        "citations": list(citations),
        "evidence_sufficient": evidence_sufficient,
        "investigation_run_id": str(run.id),
        "outcome": outcome,
        "reason_code": reason_code,
        "schema_version": 1,
        "summary": summary,
    }
    fingerprint = canonical_sha256(payload)
    async with session_factory() as db:
        bind_tenant(db.sync_session, run.tenant_id)
        async with db.begin():
            locked = await db.scalar(
                select(InvestigationRun)
                .where(
                    InvestigationRun.id == run.id,
                    InvestigationRun.tenant_id == run.tenant_id,
                )
                .with_for_update()
            )
            if locked is None:
                raise InvestigationNotFoundError(
                    code="INVESTIGATION_NOT_FOUND", message="调查记录不存在"
                )
            existing = await db.scalar(
                select(InvestigationResult).where(
                    InvestigationResult.tenant_id == run.tenant_id,
                    InvestigationResult.investigation_run_id == run.id,
                )
            )
            if existing is not None:
                return await get_investigation(
                    db,
                    tenant_id=run.tenant_id,
                    investigation_run_id=run.id,
                    reused_existing=True,
                )
            completed_at = cast(datetime, await db.scalar(select(func.clock_timestamp())))
            result = InvestigationResult(
                tenant_id=run.tenant_id,
                investigation_run_id=run.id,
                correlation_finding_id=run.correlation_finding_id,
                detection_run_id=run.detection_run_id,
                file_version_id=run.file_version_id,
                outcome=outcome,
                evidence_sufficient=evidence_sufficient,
                summary=summary,
                reason_code=reason_code,
                citations_json=list(citations),
                result_fingerprint=fingerprint,
                completed_at=completed_at,
            )
            db.add(result)
            await db.flush()
            _fault(fault_hook, "result_written")
            await write_audit(
                db,
                tenant_id=run.tenant_id,
                actor_id=actor_id,
                action="investigation.run_terminal",
                target_type="investigation_run",
                target_id=str(run.id),
                payload={
                    "correlation_finding_id": str(run.correlation_finding_id),
                    "detection_run_id": str(run.detection_run_id),
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "result_fingerprint": fingerprint,
                },
            )
            _fault(fault_hook, "terminal_audit_written")
            detail = await get_investigation(
                db,
                tenant_id=run.tenant_id,
                investigation_run_id=run.id,
            )
    _fault(fault_hook, "terminal_committed")
    return detail


async def _verify_citations(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    clause_id: str | None,
    quote: str | None,
) -> tuple[dict[str, Any], ...]:
    if clause_id is None or quote is None:
        return ()
    try:
        parsed_id = uuid.UUID(clause_id)
    except ValueError as exc:
        raise CitationVerificationError from exc
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        clause = await db.scalar(
            select(PolicyClause).where(
                PolicyClause.id == parsed_id,
                PolicyClause.tenant_id == tenant_id,
            )
        )
        if clause is None:
            raise CitationVerificationError
        start = clause.text.find(quote)
        if start < 0 or clause.text.find(quote, start + 1) >= 0:
            raise CitationVerificationError
        verified = verify_exact_quote(
            clause_id=parsed_id,
            clause_text=clause.text,
            quote_start=start,
            quote_end=start + len(quote),
            exact_quote=quote,
        )
        return (
            {
                "clause_id": str(verified.clause_id),
                "quote": verified.exact_quote,
                "quote_end": verified.quote_end,
                "quote_start": verified.quote_start,
                "schema_version": 1,
            },
        )


async def _load_detail(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
) -> InvestigationDetail:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        return await get_investigation(
            db,
            tenant_id=tenant_id,
            investigation_run_id=run_id,
        )


async def _load_keyed_request(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    key_hash: str,
    request_fingerprint: str,
) -> InvestigationDetail:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        keyed = await db.scalar(
            select(InvestigationRequest).where(
                InvestigationRequest.tenant_id == tenant_id,
                InvestigationRequest.idempotency_key_hash == key_hash,
            )
        )
        if keyed is None:
            raise InvestigationServiceError(
                code="INVESTIGATION_CONFLICT",
                message="调查请求并发创建冲突，请重试",
            )
        if keyed.request_fingerprint != request_fingerprint:
            raise InvestigationServiceError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他调查请求",
            )
        return await get_investigation(
            db,
            tenant_id=tenant_id,
            investigation_run_id=keyed.investigation_run_id,
            reused_existing=True,
        )


async def _write_safe_failure_audit(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
) -> None:
    async with session_factory() as db:
        bind_tenant(db.sync_session, tenant_id)
        async with db.begin():
            actor_exists = await db.scalar(
                select(AppUser.id).where(
                    AppUser.id == actor_id,
                    AppUser.tenant_id == tenant_id,
                )
            )
            await write_audit(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id if actor_exists is not None else None,
                action="investigation.run_failed",
                target_type="investigation_run",
                target_id=str(run_id),
                payload={
                    "correlation_finding_id": str(correlation_finding_id),
                    "detection_run_id": str(detection_run_id),
                    "reason_code": "INTERNAL_ERROR",
                },
            )


def _step_view(step: EvidenceStep) -> EvidenceStepView:
    action_kind = (
        step.action_kind.value if hasattr(step.action_kind, "value") else str(step.action_kind)
    )
    return EvidenceStepView(
        id=step.id,
        investigation_run_id=step.investigation_run_id,
        tenant_id=step.tenant_id,
        correlation_finding_id=step.correlation_finding_id,
        detection_run_id=step.detection_run_id,
        file_version_id=step.file_version_id,
        step_no=step.step_no,
        action_kind=action_kind,
        model_action=dict(step.model_action),
        decision_summary=step.decision_summary,
        payload_fingerprint=step.payload_fingerprint,
        tool_name=step.tool_name,
        tool_input=dict(step.tool_input) if step.tool_input is not None else None,
        tool_output=dict(step.tool_output) if step.tool_output is not None else None,
        created_at=step.created_at,
    )


def _validate_seed_identity(
    *,
    seed: InvestigationSeed,
    tenant_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
) -> None:
    context = seed.tool_context
    if (
        context.tenant_id != tenant_id
        or context.detection_run_id != detection_run_id
        or context.correlation_finding_id != correlation_finding_id
    ):
        raise InvestigationInputError(
            code="INVESTIGATION_TOOL_CONTEXT_INVALID",
            message="只读工具上下文与调查身份不一致",
        )


def _validate_options(options: InvestigationRunOptions) -> None:
    if not 1 <= options.max_steps <= 12 or not 1 <= options.timeout_seconds <= 120:
        raise InvestigationInputError(code="INVESTIGATION_CONFIG_INVALID", message="调查配置无效")
    if options.action_schema_version != 1 or not options.provider_model.strip():
        raise InvestigationInputError(code="INVESTIGATION_CONFIG_INVALID", message="调查配置无效")


def _validate_terminal(*, outcome: str, evidence_sufficient: bool | None) -> None:
    expected: dict[str, bool | None] = {
        DomainOutcome.SUFFICIENT.value: True,
        DomainOutcome.INSUFFICIENT.value: False,
        DomainOutcome.UNAVAILABLE.value: None,
        DomainOutcome.MAX_STEPS.value: None,
        DomainOutcome.FAILED.value: None,
    }
    if outcome not in expected or evidence_sufficient is not expected[outcome]:
        raise InvestigationInternalError(
            code="INVESTIGATION_TERMINAL_INVALID", message="调查终态无效"
        )


def _request_fingerprint(
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    correlation_finding_id: uuid.UUID,
    file_version_id: uuid.UUID,
) -> str:
    return canonical_sha256(
        {
            "actor_id": str(actor_id),
            "correlation_finding_id": str(correlation_finding_id),
            "detection_run_id": str(detection_run_id),
            "file_version_id": str(file_version_id),
            "schema_version": 1,
            "tenant_id": str(tenant_id),
        }
    )


def _config_fingerprint(options: InvestigationRunOptions) -> str:
    return canonical_sha256(
        {
            "action_schema_version": options.action_schema_version,
            "agent_version": options.agent_version,
            "max_steps": options.max_steps,
            "prompt_template_version": options.prompt_template_version,
            "provider_kind": options.provider_kind,
            "provider_model": options.provider_model,
            "redaction_version": options.redaction_version,
            "timeout_seconds": options.timeout_seconds,
        }
    )


def _input_fingerprint(
    *,
    run: InvestigationRunView,
    evidence: dict[str, Any],
    token_drafts: tuple[PiiTokenDraft, ...],
) -> str:
    return canonical_sha256(
        {
            "config_fingerprint": run.config_fingerprint,
            "correlation_finding_id": str(run.correlation_finding_id),
            "detection_run_id": str(run.detection_run_id),
            "evidence": evidence,
            "file_version_id": str(run.file_version_id),
            "schema_version": 1,
            "tenant_id": str(run.tenant_id),
            "token_drafts": sorted(
                (draft.model_dump(mode="json") for draft in token_drafts),
                key=lambda draft: (
                    draft["token_version"],
                    draft["pii_kind"],
                    draft["source_hmac"],
                    draft["token"],
                ),
            ),
        }
    )


def _idempotency_key_hash(value: str) -> str:
    if not 8 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        raise InvestigationInputError(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 必须为 8 到 128 个可见 ASCII 字符",
        )
    return hashlib.sha256(value.encode()).hexdigest()


def _safe_reason_code(value: object) -> str:
    text = value if isinstance(value, str) else "INVESTIGATION_DEPENDENCY_FAILED"
    if (
        not text
        or len(text) > 64
        or not text[0].isalpha()
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in text)
    ):
        return "INVESTIGATION_DEPENDENCY_FAILED"
    return text


def _with_reused(detail: InvestigationDetail, *, reused: bool) -> InvestigationDetail:
    return InvestigationDetail(
        run=detail.run,
        steps=detail.steps,
        result=detail.result,
        reused_existing=reused,
    )


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)
