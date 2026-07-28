"""Atomic F4 report assembly from frozen F3 and PostgreSQL policy evidence."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import case, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.parsing.models import NormalizedExpenseRecord, RowErrorDetail
from app.core.policies.canonical import (
    canonical_binding_fingerprint,
    canonical_report_fingerprint,
    canonical_sha256,
)
from app.core.policies.citations import CitationVerificationError, verify_exact_quote
from app.core.reports.models import (
    CitationSnapshot,
    ParseErrorSnapshot,
    ReportItemSnapshot,
    ReportSnapshot,
    ReportSummary,
)
from app.core.rules import RowVerdict, RuleEvaluation, RuleOutcome
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.batch import ExpenseRow, FileVersion, RowResult
from app.db.models.findings import Finding
from app.db.models.policy import (
    PolicyClause,
    PolicyDocument,
    PolicyDocumentStatus,
    PolicyFamily,
    RulePolicyBinding,
)
from app.db.models.reports import (
    ReportAttentionGroup,
    ReportCitation,
    ReportCitationStatus,
    ReportItem,
    ReportParseError,
    ReportRequest,
    ReportRun,
    ReportRunStatus,
)
from app.db.models.tenancy import AppUser
from app.db.models.validation import ValidationRun, ValidationRunStatus

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"
REPORT_TEMPLATE_VERSION = "report-snapshot-v1"
ATTENTION_MAPPING_VERSION = "f3-verdict-v1"

FaultHook = Callable[[str], None]


class ReportError(ExpenseGuardError):
    """Stable report-domain failure."""

    status_code = 409


class InvalidReportIdempotencyKeyError(ReportError):
    status_code = 422

    def __init__(self) -> None:
        super().__init__(
            code="IDEMPOTENCY_KEY_INVALID",
            message="Idempotency-Key 长度必须为 8 到 128 个字符",
        )


class ReportInternalError(ReportError):
    status_code = 500

    def __init__(self) -> None:
        super().__init__(
            code="REPORT_GENERATE_INTERNAL_ERROR",
            message="报告生成遇到内部错误，本次业务写入已回滚",
        )


@dataclass(frozen=True)
class _BindingEvidence:
    binding: RulePolicyBinding
    family: PolicyFamily
    document: PolicyDocument
    clause: PolicyClause


@dataclass(frozen=True)
class _PreparedItem:
    finding: Finding
    row: ExpenseRow
    source_verdict: RowVerdict
    source_outcome: RuleOutcome
    attention_group: ReportAttentionGroup
    evidence_snapshot: dict[str, Any]
    citations: tuple[_BindingEvidence, ...]
    citation_failure_code: str | None


@dataclass(frozen=True)
class _PreparedReport:
    batch: FileVersion
    validation: ValidationRun
    rows: tuple[ExpenseRow, ...]
    items: tuple[_PreparedItem, ...]
    parse_errors: tuple[tuple[ExpenseRow, str, str, str], ...]
    policy_manifest: dict[str, Any]
    binding_manifest: dict[str, Any]
    report_fingerprint: str


def idempotency_key_hash(value: str) -> str:
    """Hash a validated request key; plaintext keys are never persisted."""
    if not 8 <= len(value) <= 128:
        raise InvalidReportIdempotencyKeyError
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def report_request_fingerprint(
    *,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    template_version: str,
    report_fingerprint: str,
) -> str:
    """Bind an idempotency key to all semantics of one report request."""
    return canonical_sha256(
        {
            "file_version_id": str(file_version_id),
            "report_fingerprint": report_fingerprint,
            "schema_version": 1,
            "template_version": template_version,
            "validation_run_id": str(validation_run_id),
        }
    )


def attention_group_for_verdict(verdict: RowVerdict) -> ReportAttentionGroup:
    """Map the frozen F3 row verdict to the F4 single-axis attention group."""
    if verdict is RowVerdict.FLAGGED:
        return ReportAttentionGroup.HIGH_ATTENTION
    if verdict is RowVerdict.MANUAL_REVIEW:
        return ReportAttentionGroup.MANUAL_ATTENTION
    return ReportAttentionGroup.CLEARED


def report_item_order_key(
    item: ReportItem | _PreparedItem,
) -> tuple[int, int, str, tuple[int, str], str]:
    """Shared stable ordering for service reads and future API/XLSX consumers."""
    if isinstance(item, ReportItem):
        attention = item.attention_group
        row_no = item.row_no
        rule_id = item.rule_id
        rule_version = item.rule_version
        finding_id = item.finding_id
    else:
        attention = item.attention_group
        row_no = cast("int", item.finding.row_no)
        rule_id = cast("str", item.finding.rule_id)
        rule_version = item.finding.rule_version
        finding_id = item.finding.id
    rank = {
        ReportAttentionGroup.HIGH_ATTENTION: 0,
        ReportAttentionGroup.MANUAL_ATTENTION: 1,
        ReportAttentionGroup.CLEARED: 2,
    }[attention]
    version_key = (0, "") if rule_version is None else (1, rule_version)
    return rank, row_no, rule_id, version_key, str(finding_id)


async def generate_report(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    idempotency_key: str,
    template_version: str = REPORT_TEMPLATE_VERSION,
    fault_hook: FaultHook | None = None,
) -> ReportSummary:
    """Create or replay one first-success report in a single business transaction."""
    key_hash = idempotency_key_hash(idempotency_key)
    try:
        async with db.begin_nested():
            return await _generate_report(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_version_id,
                key_hash=key_hash,
                template_version=template_version,
                fault_hook=fault_hook,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise ReportError(
                code="REPORT_GENERATION_IN_PROGRESS",
                message="该租户已有报告正在生成，请稍后重试",
            ) from exc
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
        )
        raise ReportInternalError from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
        )
        raise ReportInternalError from exc


async def _generate_report(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    key_hash: str,
    template_version: str,
    fault_hook: FaultHook | None,
) -> ReportSummary:
    await lock_tenant_nowait(db, tenant_id)
    request = await db.scalar(
        select(ReportRequest).where(ReportRequest.idempotency_key_hash == key_hash)
    )
    if request is not None and request.file_version_id != file_version_id:
        raise ReportError(
            code="IDEMPOTENCY_KEY_REUSED",
            message="该 Idempotency-Key 已绑定其他报告请求",
        )
    batch = await db.scalar(
        select(FileVersion).where(FileVersion.id == file_version_id).with_for_update(nowait=True)
    )
    if batch is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    actor = await db.scalar(select(AppUser).where(AppUser.id == actor_id))
    if actor is None:
        raise NotFoundError(code="REPORT_ACTOR_NOT_FOUND", message="操作人不存在")

    if request is not None:
        report = await db.get(ReportRun, request.report_run_id)
        if report is None or report.status is not ReportRunStatus.COMPLETED:
            raise RuntimeError("幂等请求未绑定 completed report")
        expected = report_request_fingerprint(
            file_version_id=report.file_version_id,
            validation_run_id=report.validation_run_id,
            template_version=template_version,
            report_fingerprint=report.report_fingerprint,
        )
        if request.request_fingerprint != expected:
            raise ReportError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="该 Idempotency-Key 已绑定其他报告请求",
            )
        return _summary(report, reused_existing=True)

    existing = await db.scalar(
        select(ReportRun).where(ReportRun.file_version_id == file_version_id)
    )
    if existing is not None:
        if existing.status is not ReportRunStatus.COMPLETED:
            raise ReportError(
                code="REPORT_GENERATION_IN_PROGRESS",
                message="该批次报告正在生成，请稍后重试",
            )
        request_fingerprint = report_request_fingerprint(
            file_version_id=existing.file_version_id,
            validation_run_id=existing.validation_run_id,
            template_version=template_version,
            report_fingerprint=existing.report_fingerprint,
        )
        if template_version != existing.template_version:
            raise ReportError(
                code="REPORT_REQUEST_CONFLICT",
                message="该批次已冻结其他模板版本的报告",
            )
        db.add(
            ReportRequest(
                tenant_id=tenant_id,
                file_version_id=file_version_id,
                report_run_id=existing.id,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fingerprint,
            )
        )
        await db.flush()
        return _summary(existing, reused_existing=True)

    prepared = await _prepare_report(
        db,
        batch=batch,
        template_version=template_version,
    )
    request_fingerprint = report_request_fingerprint(
        file_version_id=file_version_id,
        validation_run_id=prepared.validation.id,
        template_version=template_version,
        report_fingerprint=prepared.report_fingerprint,
    )
    report = ReportRun(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        validation_run_id=prepared.validation.id,
        mapping_version_id=prepared.validation.mapping_version_id,
        status=ReportRunStatus.IN_PROGRESS,
        report_fingerprint=prepared.report_fingerprint,
        request_fingerprint=request_fingerprint,
        idempotency_key_hash=key_hash,
        source_content_sha256=batch.content_hash,
        ruleset_fingerprint=prepared.validation.ruleset_fingerprint,
        template_version=template_version,
        attention_mapping_version=ATTENTION_MAPPING_VERSION,
        policy_manifest=prepared.policy_manifest,
        binding_manifest=prepared.binding_manifest,
        stored_row_count=prepared.validation.total_row_count,
        validated_row_count=prepared.validation.evaluated_row_count,
        flagged_row_count=prepared.validation.flagged_count,
        manual_review_row_count=prepared.validation.manual_review_count,
        passed_row_count=prepared.validation.passed_count,
        parse_error_row_count=prepared.validation.parse_failed_count,
        report_item_count=len(prepared.items),
        verified_citation_count=sum(len(item.citations) for item in prepared.items),
        unavailable_citation_count=sum(not item.citations for item in prepared.items),
        high_attention_row_count=prepared.validation.flagged_count,
        manual_attention_row_count=(
            prepared.validation.manual_review_count + prepared.validation.parse_failed_count
        ),
        cleared_row_count=prepared.validation.passed_count,
        created_by=actor_id,
        completed_at=None,
    )
    db.add(report)
    await db.flush()
    _fault(fault_hook, "report_created")
    db.add(
        ReportRequest(
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            report_run_id=report.id,
            idempotency_key_hash=key_hash,
            request_fingerprint=request_fingerprint,
        )
    )
    await db.flush()

    for prepared_item in prepared.items:
        await _persist_item(db, report=report, prepared=prepared_item)
        _fault(fault_hook, "item_persisted")
    for row, error_code, column_name, message in prepared.parse_errors:
        db.add(
            ReportParseError(
                tenant_id=tenant_id,
                report_run_id=report.id,
                file_version_id=file_version_id,
                row_no=row.row_no,
                error_code=error_code,
                column_name=column_name,
                message=message,
                source_content_sha256=batch.content_hash,
            )
        )
    await db.flush()
    _fault(fault_hook, "parse_errors_persisted")

    report.status = ReportRunStatus.COMPLETED
    report.completed_at = utc_now()
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="batch.report_generate",
        target_type="report_run",
        target_id=str(report.id),
        payload={
            "file_version_id": str(file_version_id),
            "report_fingerprint": report.report_fingerprint,
            "report_item_count": report.report_item_count,
            "verified_citation_count": report.verified_citation_count,
            "unavailable_citation_count": report.unavailable_citation_count,
        },
    )
    _fault(fault_hook, "success_audit_written")
    await db.flush()
    return ReportSummary(
        report_run_id=report.id,
        file_version_id=report.file_version_id,
        validation_run_id=report.validation_run_id,
        report_fingerprint=report.report_fingerprint,
        stored_row_count=report.stored_row_count,
        report_item_count=report.report_item_count,
        verified_citation_count=report.verified_citation_count,
        unavailable_citation_count=report.unavailable_citation_count,
        reused_existing=False,
    )


async def _prepare_report(
    db: AsyncSession,
    *,
    batch: FileVersion,
    template_version: str,
) -> _PreparedReport:
    validation = await db.scalar(
        select(ValidationRun).where(ValidationRun.file_version_id == batch.id)
    )
    if validation is None:
        raise ReportError(code="REPORT_VALIDATION_NOT_FOUND", message="批次尚未执行确定性校验")
    if validation.status is not ValidationRunStatus.COMPLETED:
        raise ReportError(code="REPORT_VALIDATION_INCOMPLETE", message="批次确定性校验尚未完成")
    if batch.mapping_version_id != validation.mapping_version_id:
        raise RuntimeError("批次与 validation mapping snapshot 不一致")

    rows = tuple(
        (
            await db.scalars(
                select(ExpenseRow)
                .where(ExpenseRow.file_version_id == batch.id)
                .order_by(ExpenseRow.row_no)
            )
        ).all()
    )
    if len(rows) != validation.total_row_count or batch.row_count != len(rows):
        raise RuntimeError("报告输入行数与冻结 validation 不一致")
    row_by_no = {row.row_no: row for row in rows}
    row_results = tuple(
        (
            await db.scalars(
                select(RowResult)
                .where(RowResult.file_version_id == batch.id)
                .order_by(RowResult.row_no)
            )
        ).all()
    )
    if len(row_results) != validation.evaluated_row_count:
        raise RuntimeError("row_result 数量与冻结 validation 不一致")
    verdict_by_no: dict[int, RowVerdict] = {}
    for result in row_results:
        if result.rule_version != validation.ruleset_fingerprint:
            raise RuntimeError("row_result ruleset fingerprint 漂移")
        verdict_by_no[result.row_no] = RowVerdict(result.verdict)

    findings = tuple(
        (
            await db.scalars(
                select(Finding)
                .where(
                    Finding.file_version_id == batch.id,
                    Finding.validation_run_id == validation.id,
                )
                .order_by(Finding.row_no, Finding.rule_id, Finding.rule_version, Finding.id)
            )
        ).all()
    )
    prepared_items: list[_PreparedItem] = []
    for finding in findings:
        if finding.row_no is None or finding.rule_id is None or finding.evidence_json is None:
            raise RuntimeError("F4 只接受完整的 F3 deterministic finding")
        row = row_by_no.get(finding.row_no)
        verdict = verdict_by_no.get(finding.row_no)
        if row is None or verdict is None or row.normalized_json is None:
            raise RuntimeError("finding 原始行证据链不完整")
        evaluation = _validate_finding_evidence(finding)
        expense_date = date.fromisoformat(
            NormalizedExpenseRecord.model_validate(row.normalized_json).expense_date
        )
        citations, failure = await _resolve_citations(
            db,
            rule_config_id=finding.rule_config_id,
            expense_date=expense_date,
        )
        evidence_snapshot = dict(finding.evidence_json)
        if failure is not None:
            evidence_snapshot["citation_unavailable_reason"] = failure
        prepared_items.append(
            _PreparedItem(
                finding=finding,
                row=row,
                source_verdict=verdict,
                source_outcome=evaluation.outcome,
                attention_group=attention_group_for_verdict(verdict),
                evidence_snapshot=evidence_snapshot,
                citations=citations,
                citation_failure_code=failure,
            )
        )
    prepared_items.sort(key=report_item_order_key)
    parse_errors = _prepare_parse_errors(rows)
    if len({row.row_no for row, _, _, _ in parse_errors}) != validation.parse_failed_count:
        raise RuntimeError("解析错误行数与冻结 validation 不一致")

    policy_manifest = _policy_manifest(prepared_items)
    binding_manifest = _binding_manifest(prepared_items)
    report_fingerprint = canonical_report_fingerprint(
        tenant_id=batch.tenant_id,
        file_version_id=batch.id,
        validation_run_id=validation.id,
        source_content_sha256=batch.content_hash,
        mapping_version=str(validation.mapping_version_id),
        ruleset_fingerprint=validation.ruleset_fingerprint,
        binding_policy_manifest={
            "policy_documents": cast("list[dict[str, object]]", policy_manifest["documents"]),
            "report_items": cast("list[dict[str, object]]", binding_manifest["items"]),
        },
        report_schema_version="1",
        template_version=template_version,
        attention_mapping_version=ATTENTION_MAPPING_VERSION,
    )
    return _PreparedReport(
        batch=batch,
        validation=validation,
        rows=rows,
        items=tuple(prepared_items),
        parse_errors=parse_errors,
        policy_manifest=policy_manifest,
        binding_manifest=binding_manifest,
        report_fingerprint=report_fingerprint,
    )


def _validate_finding_evidence(finding: Finding) -> RuleEvaluation:
    evidence = cast("dict[str, Any]", finding.evidence_json)
    try:
        evaluation = RuleEvaluation.model_validate(
            {
                "outcome": evidence.get("outcome"),
                "reason_code": evidence.get("reason_code"),
                "evidence": evidence,
            }
        )
    except ValidationError as exc:
        raise RuntimeError("finding evidence snapshot 无效") from exc
    if evaluation.outcome is RuleOutcome.PASSED or evaluation.reason_code != finding.kind:
        raise RuntimeError("finding outcome/reason 与冻结 evidence 不一致")
    return evaluation


async def _resolve_citations(
    db: AsyncSession,
    *,
    rule_config_id: uuid.UUID | None,
    expense_date: date,
) -> tuple[tuple[_BindingEvidence, ...], str | None]:
    if rule_config_id is None:
        return (), "POLICY_BINDING_NOT_FOUND"
    rows = tuple(
        (
            await db.execute(
                select(RulePolicyBinding, PolicyFamily, PolicyDocument, PolicyClause)
                .join(
                    PolicyFamily,
                    PolicyFamily.id == RulePolicyBinding.policy_family_id,
                )
                .join(
                    PolicyDocument,
                    PolicyDocument.id == RulePolicyBinding.policy_document_id,
                )
                .join(
                    PolicyClause,
                    PolicyClause.id == RulePolicyBinding.policy_clause_id,
                )
                .where(
                    RulePolicyBinding.rule_config_id == rule_config_id,
                    PolicyDocument.status == PolicyDocumentStatus.PUBLISHED,
                    PolicyDocument.effective_date <= expense_date,
                    (
                        PolicyDocument.expiry_date.is_(None)
                        | (PolicyDocument.expiry_date > expense_date)
                    ),
                )
                .order_by(RulePolicyBinding.citation_order, RulePolicyBinding.id)
            )
        ).all()
    )
    if not rows:
        return (), "POLICY_BINDING_NOT_FOUND"
    evidence = tuple(
        _BindingEvidence(binding=row[0], family=row[1], document=row[2], clause=row[3])
        for row in rows
    )
    orders = [item.binding.citation_order for item in evidence]
    if len(evidence) > 3 or orders != list(range(1, len(evidence) + 1)):
        return (), "POLICY_BINDING_AMBIGUOUS"
    for item in evidence:
        if not _binding_is_exact(item):
            return (), "POLICY_BINDING_INTEGRITY_FAILED"
    return evidence, None


def _binding_is_exact(item: _BindingEvidence) -> bool:
    binding = item.binding
    document = item.document
    clause = item.clause
    if (
        document.family_id != item.family.id
        or clause.document_id != document.id
        or clause.family_id != item.family.id
        or document.content_sha256 is None
        or clause.text_sha256 is None
        or _sha256(clause.text) != clause.text_sha256
        or binding.clause_text_sha256 != clause.text_sha256
        or _sha256(binding.quote) != binding.quote_sha256
        or binding.binding_fingerprint
        != canonical_binding_fingerprint(
            tenant_id=binding.tenant_id,
            rule_config_id=binding.rule_config_id,
            policy_family_id=binding.policy_family_id,
            policy_document_id=binding.policy_document_id,
            policy_clause_id=binding.policy_clause_id,
            quote_start=binding.quote_start,
            quote_end=binding.quote_end,
            quote_sha256=binding.quote_sha256,
            clause_text_sha256=binding.clause_text_sha256,
            citation_order=binding.citation_order,
        )
    ):
        return False
    try:
        verify_exact_quote(
            clause_id=clause.id,
            clause_text=clause.text,
            quote_start=binding.quote_start,
            quote_end=binding.quote_end,
            exact_quote=binding.quote,
        )
    except CitationVerificationError:
        return False
    return True


def _prepare_parse_errors(
    rows: Sequence[ExpenseRow],
) -> tuple[tuple[ExpenseRow, str, str, str], ...]:
    prepared: list[tuple[ExpenseRow, str, str, str]] = []
    for row in rows:
        if row.normalized_json is not None:
            continue
        if row.parse_error_detail is not None:
            try:
                detail = RowErrorDetail.model_validate(row.parse_error_detail)
            except ValidationError as exc:
                raise RuntimeError("parse error snapshot 无效") from exc
            for error in detail.errors:
                prepared.append((row, error.code, error.source_column, error.message))
            continue
        if row.parse_error_code is None:
            raise RuntimeError("未解析行缺少稳定错误码")
        prepared.append((row, row.parse_error_code, "", row.parse_error or "行解析失败"))
    prepared.sort(key=lambda item: (item[0].row_no, item[1], item[2]))
    return tuple(prepared)


def _policy_manifest(items: Sequence[_PreparedItem]) -> dict[str, Any]:
    entries: dict[uuid.UUID, dict[str, Any]] = {}
    for item in items:
        for citation in item.citations:
            document = citation.document
            entries[document.id] = {
                "content_sha256": document.content_sha256,
                "document_id": str(document.id),
                "effective_date": document.effective_date.isoformat(),
                "expiry_date": (
                    document.expiry_date.isoformat() if document.expiry_date is not None else None
                ),
                "family_id": str(citation.family.id),
                "family_stable_key": citation.family.stable_key,
                "version": document.version,
            }
    return {
        "documents": [entries[key] for key in sorted(entries, key=str)],
        "schema_version": 1,
    }


def _binding_manifest(items: Sequence[_PreparedItem]) -> dict[str, Any]:
    entries = []
    for item in items:
        entries.append(
            {
                "citations": [
                    {
                        "binding_fingerprint": citation.binding.binding_fingerprint,
                        "binding_id": str(citation.binding.id),
                        "citation_order": citation.binding.citation_order,
                        "clause_id": str(citation.clause.id),
                        "clause_text_sha256": citation.clause.text_sha256,
                        "quote_sha256": citation.binding.quote_sha256,
                    }
                    for citation in item.citations
                ],
                "failure_code": item.citation_failure_code,
                "finding_id": str(item.finding.id),
                "rule_config_id": (
                    str(item.finding.rule_config_id)
                    if item.finding.rule_config_id is not None
                    else None
                ),
            }
        )
    return {"items": entries, "schema_version": 1}


async def _persist_item(
    db: AsyncSession,
    *,
    report: ReportRun,
    prepared: _PreparedItem,
) -> None:
    finding = prepared.finding
    item = ReportItem(
        tenant_id=report.tenant_id,
        report_run_id=report.id,
        finding_id=finding.id,
        file_version_id=report.file_version_id,
        row_no=cast("int", finding.row_no),
        rule_config_id=finding.rule_config_id,
        rule_id=cast("str", finding.rule_id),
        rule_version=finding.rule_version,
        source_outcome=prepared.source_outcome.value,
        source_verdict=prepared.source_verdict.value,
        reason_code=finding.kind,
        reasoning_snapshot=finding.reasoning,
        evidence_snapshot=prepared.evidence_snapshot,
        attention_group=prepared.attention_group,
        citation_status=(
            ReportCitationStatus.VERIFIED
            if prepared.citations
            else ReportCitationStatus.UNAVAILABLE
        ),
        requires_manual_citation=not prepared.citations,
        source_content_sha256=report.source_content_sha256,
    )
    db.add(item)
    await db.flush()
    for citation in prepared.citations:
        binding = citation.binding
        document = citation.document
        clause = citation.clause
        db.add(
            ReportCitation(
                tenant_id=report.tenant_id,
                report_run_id=report.id,
                report_item_id=item.id,
                binding_id=binding.id,
                policy_family_id=citation.family.id,
                policy_document_id=document.id,
                policy_clause_id=clause.id,
                family_stable_key=citation.family.stable_key,
                document_title=document.title,
                document_version=document.version,
                effective_date=document.effective_date,
                expiry_date=document.expiry_date,
                document_content_sha256=cast("str", document.content_sha256),
                clause_no=clause.clause_no,
                hierarchy_path=clause.hierarchy_path,
                clause_text=clause.text,
                clause_text_sha256=cast("str", clause.text_sha256),
                quote=binding.quote,
                quote_start=binding.quote_start,
                quote_end=binding.quote_end,
                quote_sha256=binding.quote_sha256,
                verification_status="verified_exact",
                citation_order=binding.citation_order,
            )
        )
    await db.flush()


async def load_report_snapshot(
    db: AsyncSession,
    *,
    report_run_id: uuid.UUID,
) -> ReportSnapshot:
    """Read a completed report solely from immutable PostgreSQL snapshots."""
    report = await db.get(ReportRun, report_run_id)
    if report is None or report.status is not ReportRunStatus.COMPLETED:
        raise NotFoundError(code="REPORT_NOT_FOUND", message="报告不存在")
    items = tuple(
        (
            await db.scalars(
                select(ReportItem)
                .where(ReportItem.report_run_id == report.id)
                .order_by(
                    case(
                        (ReportItem.attention_group == ReportAttentionGroup.HIGH_ATTENTION, 0),
                        (ReportItem.attention_group == ReportAttentionGroup.MANUAL_ATTENTION, 1),
                        else_=2,
                    ),
                    ReportItem.row_no,
                    ReportItem.rule_id,
                    ReportItem.rule_version.asc().nulls_first(),
                    ReportItem.finding_id,
                )
            )
        ).all()
    )
    citations = tuple(
        (
            await db.scalars(
                select(ReportCitation)
                .where(ReportCitation.report_run_id == report.id)
                .order_by(ReportCitation.report_item_id, ReportCitation.citation_order)
            )
        ).all()
    )
    citations_by_item: dict[uuid.UUID, list[ReportCitation]] = {}
    for citation in citations:
        citations_by_item.setdefault(citation.report_item_id, []).append(citation)
    parse_errors = tuple(
        (
            await db.scalars(
                select(ReportParseError)
                .where(ReportParseError.report_run_id == report.id)
                .order_by(
                    ReportParseError.row_no,
                    ReportParseError.error_code,
                    ReportParseError.column_name,
                )
            )
        ).all()
    )
    return ReportSnapshot(
        summary=_summary(report, reused_existing=True),
        policy_manifest=dict(report.policy_manifest),
        binding_manifest=dict(report.binding_manifest),
        items=tuple(
            ReportItemSnapshot(
                id=item.id,
                finding_id=item.finding_id,
                row_no=item.row_no,
                rule_id=item.rule_id,
                rule_version=item.rule_version,
                source_outcome=item.source_outcome,
                source_verdict=item.source_verdict,
                reason_code=item.reason_code,
                evidence_snapshot=(
                    dict(item.evidence_snapshot) if item.evidence_snapshot is not None else None
                ),
                attention_group=item.attention_group,
                citation_status=item.citation_status,
                requires_manual_citation=item.requires_manual_citation,
                citations=tuple(
                    CitationSnapshot(
                        id=citation.id,
                        report_item_id=citation.report_item_id,
                        binding_id=citation.binding_id,
                        citation_order=citation.citation_order,
                        family_stable_key=citation.family_stable_key,
                        document_title=citation.document_title,
                        document_version=citation.document_version,
                        clause_no=citation.clause_no,
                        quote=citation.quote,
                        quote_start=citation.quote_start,
                        quote_end=citation.quote_end,
                        quote_sha256=citation.quote_sha256,
                    )
                    for citation in citations_by_item.get(item.id, ())
                ),
            )
            for item in items
        ),
        parse_errors=tuple(
            ParseErrorSnapshot(
                id=error.id,
                row_no=error.row_no,
                error_code=error.error_code,
                column_name=error.column_name,
                message=error.message,
            )
            for error in parse_errors
        ),
    )


def _summary(report: ReportRun, *, reused_existing: bool) -> ReportSummary:
    return ReportSummary(
        report_run_id=report.id,
        file_version_id=report.file_version_id,
        validation_run_id=report.validation_run_id,
        report_fingerprint=report.report_fingerprint,
        stored_row_count=report.stored_row_count,
        report_item_count=report.report_item_count,
        verified_citation_count=report.verified_citation_count,
        unavailable_citation_count=report.unavailable_citation_count,
        reused_existing=reused_existing,
    )


async def _rollback_and_record_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
) -> None:
    await db.rollback()
    async with session_factory() as audit_db:
        bind_tenant(audit_db.sync_session, tenant_id)
        actor = await audit_db.scalar(select(AppUser).where(AppUser.id == actor_id))
        await write_audit(
            audit_db,
            tenant_id=tenant_id,
            actor_id=actor.id if actor is not None else None,
            action="batch.report_failed",
            target_type="file_version",
            target_id=str(file_version_id),
            payload={
                "error_category": "internal_error",
                "file_version_id": str(file_version_id),
            },
        )
        await audit_db.commit()


def _fault(hook: FaultHook | None, stage: str) -> None:
    if hook is not None:
        hook(stage)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlstate(exc: OperationalError) -> str | None:
    value: Any = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
