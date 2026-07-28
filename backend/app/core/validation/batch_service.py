"""F3 规则快照、批次编排、幂等与审计。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ExpenseGuardError, NotFoundError
from app.core.orchestration.idempotency import process_row_once
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.rules import (
    DuplicateMatch,
    InvoiceOccurrence,
    RowVerdict,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    RuleVersion,
    aggregate_verdict,
    evaluate_rule_selection,
    render_reasoning,
    ruleset_fingerprint,
    select_duplicate_match,
    select_effective_rule_version,
    validate_rule_definition,
)
from app.core.rules.canonical import SELECTION_ALGORITHM, rule_config_fingerprint
from app.core.rules.models import RuleFamilyManifest, RuleSelection, SelectedRuleVersion
from app.core.security.auth_service import write_audit
from app.core.tenancy.locking import lock_tenant_nowait
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.batch import ExpenseRow, FileVersion, ParseStatus
from app.db.models.config import RuleConfig
from app.db.models.findings import Finding
from app.db.models.findings import RuleKind as DbRuleKind
from app.db.models.validation import (
    ValidationDependency,
    ValidationRun,
    ValidationRunStatus,
)

LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


class BatchValidationError(ExpenseGuardError):
    """可稳定映射到 API 的确定性校验领域错误。"""

    status_code = 409


class BatchValidationInternalError(BatchValidationError):
    """未分类系统异常；业务事务已整体回滚。"""

    status_code = 500


@dataclass(frozen=True)
class ValidationSummary:
    """一次已完成校验的稳定摘要。"""

    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    ruleset_fingerprint: str
    total_row_count: int
    evaluated_row_count: int
    passed_count: int
    flagged_count: int
    manual_review_count: int
    parse_failed_count: int
    reused_existing: bool


@dataclass
class _ValidationAttempt:
    mapping_version_id: uuid.UUID | None = None
    ruleset_fingerprint: str | None = None


@dataclass(frozen=True)
class _RuleFamily:
    rule_id: str
    kind: RuleKind
    candidates: tuple[RuleVersion, ...]


@dataclass(frozen=True)
class _DuplicateIndex:
    current_root_id: uuid.UUID
    current_root_uploaded_at: datetime
    current_batch: tuple[InvoiceOccurrence, ...]
    historical: tuple[InvoiceOccurrence, ...]
    depended_file_version_ids: tuple[uuid.UUID, ...]


async def validate_batch(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
) -> ValidationSummary:
    """在单个整批事务中冻结快照并校验全部成功解析行。

    领域错误只回滚本服务的保存点；未分类系统错误会回滚调用方主事务，
    随后用独立短事务追加无 PII 的失败审计。
    """
    attempt = _ValidationAttempt()
    try:
        async with db.begin_nested():
            return await _validate_batch(
                db,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=file_version_id,
                attempt=attempt,
            )
    except OperationalError as exc:
        if _sqlstate(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            raise BatchValidationError(
                code="BATCH_VALIDATION_IN_PROGRESS",
                message="该租户已有批次正在校验，请稍后重试",
            ) from exc
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise BatchValidationInternalError(
            code="BATCH_VALIDATE_INTERNAL_ERROR",
            message="批次校验遇到内部错误，本次业务写入已回滚",
        ) from exc
    except ExpenseGuardError:
        raise
    except Exception as exc:
        await _rollback_and_record_failure(
            db,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_version_id,
            attempt=attempt,
        )
        raise BatchValidationInternalError(
            code="BATCH_VALIDATE_INTERNAL_ERROR",
            message="批次校验遇到内部错误，本次业务写入已回滚",
        ) from exc


async def _validate_batch(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    attempt: _ValidationAttempt,
) -> ValidationSummary:
    await lock_tenant_nowait(db, tenant_id)
    batch = await _lock_batch(db, file_version_id)
    existing = await db.scalar(
        select(ValidationRun).where(ValidationRun.file_version_id == file_version_id)
    )
    if existing is not None:
        if existing.status is not ValidationRunStatus.COMPLETED:
            raise BatchValidationError(
                code="BATCH_VALIDATION_IN_PROGRESS",
                message="该批次正在校验，请稍后重试",
            )
        return _summary(existing, reused_existing=True)

    rows = tuple(
        (
            await db.scalars(
                select(ExpenseRow)
                .where(ExpenseRow.file_version_id == file_version_id)
                .order_by(ExpenseRow.row_no)
            )
        ).all()
    )
    normalized_rows = _validate_batch_prerequisites(batch, rows)
    mapping_version_id = cast("uuid.UUID", batch.mapping_version_id)
    attempt.mapping_version_id = mapping_version_id

    families = await _load_rule_families(db)
    selections_by_row, manifest = _freeze_rule_manifest(families, normalized_rows)
    fingerprint = ruleset_fingerprint(
        mapping_version_id=mapping_version_id,
        rule_families=manifest,
    )
    attempt.ruleset_fingerprint = fingerprint
    run = ValidationRun(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        mapping_version_id=mapping_version_id,
        ruleset_fingerprint=fingerprint,
        ruleset_manifest=_manifest_json(mapping_version_id, manifest),
        status=ValidationRunStatus.IN_PROGRESS,
        total_row_count=0,
        evaluated_row_count=0,
        passed_count=0,
        flagged_count=0,
        manual_review_count=0,
        parse_failed_count=0,
        completed_at=None,
        triggered_by=actor_id,
    )
    db.add(run)
    await db.flush()

    duplicate_index = await _load_duplicate_index(db, batch, normalized_rows)
    await _persist_dependencies(db, run, duplicate_index.depended_file_version_ids)
    verdicts: list[RowVerdict] = []
    for row, record in normalized_rows:
        selections = selections_by_row[row.row_no]

        async def compute(
            *,
            current_row: ExpenseRow = row,
            current_record: NormalizedExpenseRecord = record,
            current_selections: tuple[RuleSelection, ...] = selections,
        ) -> tuple[str, str]:
            evaluations = []
            for selection in current_selections:
                duplicate_match = None
                if selection.rule_kind is RuleKind.INVOICE_DUPLICATE:
                    duplicate_match = _duplicate_match(
                        batch=batch,
                        row=current_row,
                        record=current_record,
                        index=duplicate_index,
                    )
                evaluation = evaluate_rule_selection(
                    selection,
                    current_record,
                    duplicate_match=duplicate_match,
                )
                evaluations.append(evaluation)
                if evaluation.outcome is not RuleOutcome.PASSED:
                    await _persist_finding(
                        db,
                        run=run,
                        batch=batch,
                        row=current_row,
                        selection=selection,
                        outcome=evaluation,
                    )
            verdict = aggregate_verdict(evaluations)
            verdicts.append(verdict)
            return verdict.value, fingerprint

        outcome = await process_row_once(
            db,
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            row_no=row.row_no,
            compute=compute,
        )
        if not outcome.created or outcome.rule_version != fingerprint:
            raise RuntimeError("校验快照与既有行级结果不一致")

    counts = {verdict: verdicts.count(verdict) for verdict in RowVerdict}
    total = len(rows)
    evaluated = len(normalized_rows)
    run.total_row_count = total
    run.evaluated_row_count = evaluated
    run.passed_count = counts[RowVerdict.PASSED]
    run.flagged_count = counts[RowVerdict.FLAGGED]
    run.manual_review_count = counts[RowVerdict.MANUAL_REVIEW]
    run.parse_failed_count = total - evaluated
    run.status = ValidationRunStatus.COMPLETED
    run.completed_at = utc_now()
    await write_audit(
        db,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action="batch.validate",
        target_type="file_version",
        target_id=str(batch.id),
        payload={
            "file_version_id": str(batch.id),
            "mapping_version_id": str(mapping_version_id),
            "ruleset_fingerprint": fingerprint,
            "total_row_count": total,
            "evaluated_row_count": evaluated,
            "passed_count": run.passed_count,
            "flagged_count": run.flagged_count,
            "manual_review_count": run.manual_review_count,
            "parse_failed_count": run.parse_failed_count,
        },
    )
    await db.flush()
    return _summary(run, reused_existing=False)


async def _lock_batch(db: AsyncSession, file_version_id: uuid.UUID) -> FileVersion:
    batch = await db.scalar(
        select(FileVersion).where(FileVersion.id == file_version_id).with_for_update(nowait=True)
    )
    if batch is None:
        raise NotFoundError(code="BATCH_NOT_FOUND", message="批次不存在")
    return batch


def _validate_batch_prerequisites(
    batch: FileVersion,
    rows: tuple[ExpenseRow, ...],
) -> tuple[tuple[ExpenseRow, NormalizedExpenseRecord], ...]:
    if batch.parse_status not in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS} or (
        batch.mapping_version_id is None
    ):
        raise BatchValidationError(code="BATCH_NOT_PARSED", message="批次尚未成功解析")
    if batch.row_count is None or batch.row_count != len(rows):
        raise RuntimeError("批次 row_count 与 expense_row 数量不一致")
    normalized: list[tuple[ExpenseRow, NormalizedExpenseRecord]] = []
    for row in rows:
        if row.normalized_json is None:
            continue
        record = NormalizedExpenseRecord.model_validate(row.normalized_json)
        if record.mapping_version_id != batch.mapping_version_id:
            raise RuntimeError("规范化行绑定的映射版本与批次不一致")
        normalized.append((row, record))
    if not normalized:
        raise BatchValidationError(code="BATCH_NOT_PARSED", message="批次没有成功解析行")
    return tuple(normalized)


async def _load_rule_families(db: AsyncSession) -> tuple[_RuleFamily, ...]:
    configs = tuple(
        (
            await db.scalars(
                select(RuleConfig)
                .where(
                    RuleConfig.backfilled_legacy.is_(False),
                    RuleConfig.config_fingerprint.is_not(None),
                    RuleConfig.effective_from.is_not(None),
                    RuleConfig.created_by.is_not(None),
                )
                .order_by(RuleConfig.rule_id, RuleConfig.version)
            )
        ).all()
    )
    grouped: dict[tuple[RuleKind, str], list[RuleVersion]] = {}
    kinds_by_rule_id: dict[str, set[RuleKind]] = {}
    for config in configs:
        try:
            definition = validate_rule_definition(config.definition)
        except (ValidationError, ValueError) as exc:
            raise BatchValidationError(
                code="RULESET_INVALID", message="规则集包含无效配置"
            ) from exc
        effective_from = cast("date", config.effective_from)
        stored_fingerprint = cast("str", config.config_fingerprint)
        if (
            rule_config_fingerprint(
                rule_id=config.rule_id,
                effective_from=effective_from,
                definition=definition,
            )
            != stored_fingerprint
        ):
            raise BatchValidationError(code="RULESET_INVALID", message="规则配置指纹不一致")
        kind = definition.kind
        kinds_by_rule_id.setdefault(config.rule_id, set()).add(kind)
        grouped.setdefault((kind, config.rule_id), []).append(
            RuleVersion(
                id=config.id,
                rule_id=config.rule_id,
                version=config.version,
                effective_from=effective_from,
                config_fingerprint=stored_fingerprint,
                definition=definition,
            )
        )
    if any(len(kinds) != 1 for kinds in kinds_by_rule_id.values()):
        raise BatchValidationError(code="RULESET_INVALID", message="逻辑规则跨版本 kind 不一致")
    families: list[_RuleFamily] = []
    for kind in RuleKind:
        matches = [
            _RuleFamily(rule_id=rule_id, kind=family_kind, candidates=tuple(candidates))
            for (family_kind, rule_id), candidates in grouped.items()
            if family_kind is kind
        ]
        if len(matches) != 1:
            raise BatchValidationError(code="RULESET_INVALID", message="规则集不完整或存在歧义")
        families.append(matches[0])
    return tuple(sorted(families, key=lambda item: (item.kind.value, item.rule_id)))


def _freeze_rule_manifest(
    families: tuple[_RuleFamily, ...],
    rows: tuple[tuple[ExpenseRow, NormalizedExpenseRecord], ...],
) -> tuple[dict[int, tuple[RuleSelection, ...]], tuple[RuleFamilyManifest, ...]]:
    selections: dict[int, tuple[RuleSelection, ...]] = {}
    selected_by_family: dict[tuple[RuleKind, str], dict[int, str]] = {
        (family.kind, family.rule_id): {} for family in families
    }
    for row, record in rows:
        expense_date = date.fromisoformat(record.expense_date)
        row_selections = tuple(
            select_effective_rule_version(
                rule_id=family.rule_id,
                rule_kind=family.kind,
                candidates=family.candidates,
                expense_date=expense_date,
            )
            for family in families
        )
        selections[row.row_no] = row_selections
        for selection in row_selections:
            if selection.selected is not None:
                selected_by_family[(selection.rule_kind, selection.rule_id)][
                    selection.selected.version
                ] = selection.selected.config_fingerprint
    manifest = tuple(
        RuleFamilyManifest(
            rule_id=family.rule_id,
            kind=family.kind,
            selected_versions=tuple(
                SelectedRuleVersion(version=version, config_fingerprint=fingerprint)
                for version, fingerprint in selected_by_family[
                    (family.kind, family.rule_id)
                ].items()
            ),
        )
        for family in families
    )
    return selections, manifest


def _manifest_json(
    mapping_version_id: uuid.UUID,
    manifest: tuple[RuleFamilyManifest, ...],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "selection_algorithm": SELECTION_ALGORITHM,
        "mapping_version_id": str(mapping_version_id),
        "rule_families": [family.model_dump(mode="json") for family in manifest],
    }


async def _load_duplicate_index(
    db: AsyncSession,
    batch: FileVersion,
    rows: tuple[tuple[ExpenseRow, NormalizedExpenseRecord], ...],
) -> _DuplicateIndex:
    all_versions = tuple((await db.scalars(select(FileVersion))).all())
    by_id = {version.id: version for version in all_versions}
    current_root_id = batch.root_file_version_id or batch.id
    root = by_id.get(current_root_id)
    if root is None or root.revision_no != 1:
        raise RuntimeError("当前批次 root lineage 不完整")
    current_batch = tuple(
        InvoiceOccurrence(
            file_version_id=batch.id,
            root_file_version_id=current_root_id,
            root_uploaded_at=root.uploaded_at,
            row_no=row.row_no,
            invoice_no=record.invoice_no,
        )
        for row, record in rows
        if record.invoice_no is not None
    )
    highest_by_root: dict[uuid.UUID, FileVersion] = {}
    for version in all_versions:
        if version.parse_status not in {ParseStatus.PARSED, ParseStatus.PARSED_WITH_ERRORS}:
            continue
        logical_root = version.root_file_version_id or version.id
        if logical_root == current_root_id:
            continue
        existing = highest_by_root.get(logical_root)
        if existing is None or version.revision_no > existing.revision_no:
            highest_by_root[logical_root] = version
    selected_versions = tuple(
        sorted(
            highest_by_root.values(),
            key=lambda item: str(item.root_file_version_id or item.id),
        )
    )
    selected_ids = tuple(version.id for version in selected_versions)
    historical_rows: tuple[ExpenseRow, ...] = ()
    if selected_ids:
        historical_rows = tuple(
            (
                await db.scalars(
                    select(ExpenseRow)
                    .where(
                        ExpenseRow.file_version_id.in_(selected_ids),
                        ExpenseRow.normalized_json.is_not(None),
                    )
                    .order_by(ExpenseRow.file_version_id, ExpenseRow.row_no)
                )
            ).all()
        )
    selected_by_id = {version.id: version for version in selected_versions}
    historical: list[InvoiceOccurrence] = []
    for row in historical_rows:
        record = NormalizedExpenseRecord.model_validate(row.normalized_json)
        if record.invoice_no is None:
            continue
        version = selected_by_id[row.file_version_id]
        root_id = version.root_file_version_id or version.id
        root_version = by_id.get(root_id)
        if root_version is None or root_version.revision_no != 1:
            raise RuntimeError("历史查重 root lineage 不完整")
        historical.append(
            InvoiceOccurrence(
                file_version_id=version.id,
                root_file_version_id=root_id,
                root_uploaded_at=root_version.uploaded_at,
                row_no=row.row_no,
                invoice_no=record.invoice_no,
            )
        )
    return _DuplicateIndex(
        current_root_id=current_root_id,
        current_root_uploaded_at=root.uploaded_at,
        current_batch=current_batch,
        historical=tuple(historical),
        depended_file_version_ids=selected_ids,
    )


async def _persist_dependencies(
    db: AsyncSession,
    run: ValidationRun,
    depended_file_version_ids: tuple[uuid.UUID, ...],
) -> None:
    db.add_all(
        ValidationDependency(
            tenant_id=run.tenant_id,
            validation_run_id=run.id,
            depended_file_version_id=file_version_id,
        )
        for file_version_id in depended_file_version_ids
    )
    await db.flush()


def _duplicate_match(
    *,
    batch: FileVersion,
    row: ExpenseRow,
    record: NormalizedExpenseRecord,
    index: _DuplicateIndex,
) -> DuplicateMatch | None:
    if record.invoice_no is None:
        return None
    current = InvoiceOccurrence(
        file_version_id=batch.id,
        root_file_version_id=index.current_root_id,
        root_uploaded_at=index.current_root_uploaded_at,
        row_no=row.row_no,
        invoice_no=record.invoice_no,
    )
    return select_duplicate_match(
        current=current,
        current_batch_occurrences=index.current_batch,
        historical_occurrences=index.historical,
    )


async def _persist_finding(
    db: AsyncSession,
    *,
    run: ValidationRun,
    batch: FileVersion,
    row: ExpenseRow,
    selection: RuleSelection,
    outcome: RuleEvaluation,
) -> None:
    evidence = outcome.evidence
    if evidence is None or outcome.reason_code is None:
        raise RuntimeError("非 passed 求值缺少 evidence")
    selected = selection.selected
    db.add(
        Finding(
            tenant_id=batch.tenant_id,
            file_version_id=batch.id,
            row_no=row.row_no,
            kind=outcome.reason_code,
            severity_impact=0,
            severity_confidence=0,
            rule_id=selection.rule_id,
            rule_version=str(selected.version) if selected is not None else None,
            reasoning=render_reasoning(evidence),
            validation_run_id=run.id,
            rule_kind=DbRuleKind(selection.rule_kind.value),
            rule_config_id=selected.id if selected is not None else None,
            evidence_json=evidence.model_dump(mode="json"),
        )
    )
    await db.flush()


def _summary(run: ValidationRun, *, reused_existing: bool) -> ValidationSummary:
    return ValidationSummary(
        file_version_id=run.file_version_id,
        mapping_version_id=run.mapping_version_id,
        ruleset_fingerprint=run.ruleset_fingerprint,
        total_row_count=run.total_row_count,
        evaluated_row_count=run.evaluated_row_count,
        passed_count=run.passed_count,
        flagged_count=run.flagged_count,
        manual_review_count=run.manual_review_count,
        parse_failed_count=run.parse_failed_count,
        reused_existing=reused_existing,
    )


async def _rollback_and_record_failure(
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    file_version_id: uuid.UUID,
    attempt: _ValidationAttempt,
) -> None:
    await db.rollback()
    async with session_factory() as audit_db:
        bind_tenant(audit_db.sync_session, tenant_id)
        await write_audit(
            audit_db,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action="batch.validate_failed",
            target_type="file_version",
            target_id=str(file_version_id),
            payload={
                "file_version_id": str(file_version_id),
                "mapping_version_id": (
                    str(attempt.mapping_version_id) if attempt.mapping_version_id else None
                ),
                "ruleset_fingerprint": attempt.ruleset_fingerprint,
                "error_category": "internal_error",
            },
        )
        await audit_db.commit()


def _sqlstate(exc: OperationalError) -> str | None:
    value = getattr(exc.orig, "sqlstate", None)
    return value if isinstance(value, str) else None
