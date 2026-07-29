"""CP-F5.2 plan, joined-query, decision, idempotency, and recovery tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.reports.service import ReportInternalError, generate_report
from app.core.reviews.config_service import create_sampling_config
from app.core.reviews.decision_service import submit_finding_review, submit_sampling_review
from app.core.reviews.errors import ReviewError, ReviewInputError, ReviewInternalError
from app.core.reviews.plan_service import create_legacy_sampling_plan
from app.core.reviews.query_service import (
    get_finding_review_detail,
    get_review_summary,
    get_sampling_review_detail,
    list_review_queue,
)
from app.core.tenancy.scope import bind_tenant
from app.db.base import utc_now
from app.db.models.audit import AuditLog
from app.db.models.batch import FileVersion, RowResult
from app.db.models.findings import (
    Finding,
    Review,
    ReviewPlanRequest,
    ReviewSamplingPlan,
    SamplingAudit,
    SamplingReview,
)
from app.db.models.reports import ReportItem, ReportRun, ReportRunStatus
from app.db.models.validation import ValidationRun
from tests.integration.test_report_service import (
    _seed_policy_bindings,
    _seed_validated_batch,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


async def _create_report(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug=slug)
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        report = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key=f"report-{slug}",
        )
        await session.commit()
    return tenant_id, actor_id, batch_id, report.report_run_id


async def _create_legacy_report(
    session_factory: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug=slug)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        batch = await session.get(FileVersion, batch_id)
        validation = await session.scalar(
            select(ValidationRun).where(ValidationRun.file_version_id == batch_id)
        )
        assert batch is not None and validation is not None
        legacy = ReportRun(
            tenant_id=tenant_id,
            file_version_id=batch_id,
            validation_run_id=validation.id,
            mapping_version_id=validation.mapping_version_id,
            status=ReportRunStatus.COMPLETED,
            report_fingerprint="d" * 64,
            request_fingerprint="e" * 64,
            idempotency_key_hash="f" * 64,
            source_content_sha256=batch.content_hash,
            ruleset_fingerprint=validation.ruleset_fingerprint,
            template_version="legacy-fault-v1",
            attention_mapping_version="f4-attention-v1",
            policy_manifest={},
            binding_manifest={},
            stored_row_count=validation.total_row_count,
            validated_row_count=validation.evaluated_row_count,
            flagged_row_count=validation.flagged_count,
            manual_review_row_count=validation.manual_review_count,
            passed_row_count=validation.passed_count,
            parse_error_row_count=validation.parse_failed_count,
            report_item_count=0,
            verified_citation_count=0,
            unavailable_citation_count=0,
            high_attention_row_count=validation.flagged_count,
            manual_attention_row_count=(
                validation.manual_review_count + validation.parse_failed_count
            ),
            cleared_row_count=validation.passed_count,
            created_by=actor_id,
            completed_at=utc_now(),
        )
        session.add(legacy)
        await session.commit()
    return tenant_id, actor_id, legacy.id


async def test_new_report_auto_plan_is_atomic_reproducible_and_queryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, _actor_id, batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-auto-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        plan = await session.scalar(
            select(ReviewSamplingPlan).where(ReviewSamplingPlan.report_run_id == report_id)
        )
        assert plan is not None
        samples = tuple(
            (
                await session.scalars(
                    select(SamplingAudit)
                    .where(SamplingAudit.sampling_plan_id == plan.id)
                    .order_by(SamplingAudit.selection_rank)
                )
            ).all()
        )
        audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(AuditLog.action == "sampling.plan_create")
                )
            ).all()
        )
        queue = await list_review_queue(session, tenant_id=tenant_id)
        finding_detail = await get_finding_review_detail(
            session,
            tenant_id=tenant_id,
            report_item_id=queue.items[0].target_id,
        )
        sample_detail = await get_sampling_review_detail(
            session,
            tenant_id=tenant_id,
            sampling_audit_id=samples[0].id,
        )
        summary = await get_review_summary(
            session,
            tenant_id=tenant_id,
            report_run_id=report_id,
        )
    assert plan.file_version_id == batch_id
    assert plan.eligible_count == plan.sample_size == 1
    assert [sample.row_no for sample in samples] == [2]
    assert [sample.selection_rank for sample in samples] == [1]
    assert all(sample.decision is None and sample.reviewer_id is None for sample in samples)
    assert len(audits) == 1
    assert "seed" not in str(audits[0].payload_json)
    assert queue.items[-1].kind == "clearance_sample"
    assert all(item.kind == "finding" for item in queue.items[:-1])
    assert finding_detail.report_item.id == queue.items[0].target_id
    assert finding_detail.raw_row == {"row": finding_detail.report_item.row_no}
    assert sample_detail.source_verdict == "passed"
    assert sample_detail.row_no == 2
    assert sample_detail.cleared_items == ()
    assert summary.sampling_status == "completed"
    assert summary.sample_eligible == summary.sample_selected == summary.sample_pending == 1


async def test_review_queue_sql_pagination_preserves_union_order_and_kind_totals(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-queue-page-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        full = await list_review_queue(
            session,
            tenant_id=tenant_id,
            report_run_id=report_id,
            limit=200,
        )
        first = await list_review_queue(
            session,
            tenant_id=tenant_id,
            report_run_id=report_id,
            limit=2,
        )
        second = await list_review_queue(
            session,
            tenant_id=tenant_id,
            report_run_id=report_id,
            limit=2,
            offset=2,
        )
        findings = await list_review_queue(
            session,
            tenant_id=tenant_id,
            kind="finding",
            report_run_id=report_id,
            limit=200,
        )
        samples = await list_review_queue(
            session,
            tenant_id=tenant_id,
            kind="clearance_sample",
            report_run_id=report_id,
            limit=200,
        )
    assert full.total == len(full.items) == findings.total + samples.total
    assert tuple(item.target_id for item in first.items + second.items) == tuple(
        item.target_id for item in full.items
    )
    assert all(item.kind == "finding" for item in findings.items)
    assert all(item.kind == "clearance_sample" for item in samples.items)

    finding_target = findings.items[0].target_id
    sample_target = samples.items[0].target_id
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await submit_finding_review(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_item_id=finding_target,
            decision="confirmed",
            note=None,
            idempotency_key="queue-page-finding",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await submit_sampling_review(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            sampling_audit_id=sample_target,
            decision="clearance_confirmed",
            note=None,
            idempotency_key="queue-page-sample",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        completed = await list_review_queue(
            session,
            tenant_id=tenant_id,
            status="completed",
            report_run_id=report_id,
            limit=1,
        )
        completed_tail = await list_review_queue(
            session,
            tenant_id=tenant_id,
            status="completed",
            report_run_id=report_id,
            limit=1,
            offset=1,
        )
    assert completed.total == 2
    assert {completed.items[0].target_id, completed_tail.items[0].target_id} == {
        finding_target,
        sample_target,
    }


async def test_missing_sampling_config_prevents_all_report_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"f5-no-config-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory,
        slug=slug,
        with_sampling_config=False,
    )
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as exc_info:
            await generate_report(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=batch_id,
                idempotency_key="missing-config-report",
            )
        assert exc_info.value.code == "SAMPLING_CONFIG_REQUIRED"
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReportRun)) == 0
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingPlan)) == 0
        assert await session.scalar(select(func.count()).select_from(SamplingAudit)) == 0


async def test_finding_and_sampling_decisions_are_one_time_and_do_not_rewrite_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-decisions-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        item = await session.scalar(
            select(ReportItem)
            .where(ReportItem.report_run_id == report_id)
            .order_by(ReportItem.row_no, ReportItem.rule_id, ReportItem.id)
        )
        sample = await session.scalar(
            select(SamplingAudit).where(SamplingAudit.report_run_id == report_id)
        )
        finding_count_before = int(
            await session.scalar(select(func.count()).select_from(Finding)) or 0
        )
        row_result_before = tuple(
            (
                await session.scalars(
                    select(RowResult)
                    .where(RowResult.file_version_id == batch_id)
                    .order_by(RowResult.row_no)
                )
            ).all()
        )
    assert item is not None and sample is not None

    finding_arguments = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "report_item_id": item.id,
        "decision": "false_positive",
        "note": "policy exception verified manually",
        "idempotency_key": "finding-review-key-1",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await submit_finding_review(session, session_factory, **finding_arguments)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replayed = await submit_finding_review(session, session_factory, **finding_arguments)
        await session.commit()
    assert replayed.id == created.id
    assert replayed.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as duplicate:
            await submit_finding_review(
                session,
                session_factory,
                **(finding_arguments | {"idempotency_key": "finding-review-key-2"}),
            )
        assert duplicate.value.code == "REVIEW_ALREADY_COMPLETED"
        await session.rollback()

    sampling_arguments = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "sampling_audit_id": sample.id,
        "decision": "missed_issue",
        "note": "receipt lacked required supporting evidence",
        "idempotency_key": "sampling-review-key-1",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        sampling_result = await submit_sampling_review(
            session,
            session_factory,
            **sampling_arguments,
        )
        await session.commit()
    assert sampling_result.decision.value == "missed_issue"

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        persisted_sample = await session.get(SamplingAudit, sample.id)
        assert persisted_sample is not None
        assert persisted_sample.decision is None
        assert persisted_sample.reviewer_id is None
        assert persisted_sample.reviewed_at is None
        assert int(await session.scalar(select(func.count()).select_from(Finding)) or 0) == (
            finding_count_before
        )
        row_result_after = tuple(
            (
                await session.scalars(
                    select(RowResult)
                    .where(RowResult.file_version_id == batch_id)
                    .order_by(RowResult.row_no)
                )
            ).all()
        )
        assert [row.verdict for row in row_result_after] == [
            row.verdict for row in row_result_before
        ]
        summary = await get_review_summary(
            session,
            tenant_id=tenant_id,
            report_run_id=report_id,
        )
        success_audits = tuple(
            (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.action.in_(["review.submit", "sampling.review_submit"])
                    )
                )
            ).all()
        )
    assert summary.finding_false_positive == 1
    assert summary.sample_missed_issue == 1
    assert len(success_audits) == 2
    assert all("policy exception" not in str(audit.payload_json) for audit in success_audits)
    assert all("receipt lacked" not in str(audit.payload_json) for audit in success_audits)


async def test_decision_validation_fault_recovery_and_nowait_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-recovery-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        items = tuple(
            (
                await session.scalars(
                    select(ReportItem)
                    .where(ReportItem.report_run_id == report_id)
                    .order_by(ReportItem.rule_id, ReportItem.id)
                )
            ).all()
        )
    assert len(items) >= 2

    with pytest.raises(ReviewInputError):
        async with session_factory() as session:
            bind_tenant(session.sync_session, tenant_id)
            await submit_finding_review(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_item_id=items[0].id,
                decision="false_positive",
                note=None,
                idempotency_key="invalid-note-key",
            )

    def fail_after_success_audit(stage: str) -> None:
        if stage == "success_audit_written":
            raise RuntimeError("simulated process loss")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewInternalError):
            await submit_finding_review(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_item_id=items[0].id,
                decision="confirmed",
                note=None,
                idempotency_key="finding-fault-key",
                fault_hook=fail_after_success_audit,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert (
            await session.scalar(
                select(func.count()).select_from(Review).where(Review.report_item_id == items[0].id)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "review.submit_failed")
            )
            == 1
        )

    first_session = session_factory()
    second_session = session_factory()
    try:
        bind_tenant(first_session.sync_session, tenant_id)
        bind_tenant(second_session.sync_session, tenant_id)
        first = await submit_finding_review(
            first_session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_item_id=items[0].id,
            decision="confirmed",
            note=None,
            idempotency_key="finding-retry-key",
        )
        with pytest.raises(ReviewError) as conflict:
            await submit_finding_review(
                second_session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_item_id=items[1].id,
                decision="confirmed",
                note=None,
                idempotency_key="finding-concurrent-key",
            )
        assert conflict.value.code == "REVIEW_CONFLICT"
        await second_session.rollback()
        await first_session.commit()
        assert first.reused_existing is False
    finally:
        await first_session.close()
        await second_session.close()


async def test_legacy_plan_request_ledger_reuses_plan_without_resampling(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, actor_id, batch_id = await _seed_validated_batch(
        session_factory, slug=f"f5-legacy-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        batch = await session.get(FileVersion, batch_id)
        validation = await session.scalar(
            select(ValidationRun).where(ValidationRun.file_version_id == batch_id)
        )
        assert batch is not None and validation is not None
        legacy = ReportRun(
            tenant_id=tenant_id,
            file_version_id=batch_id,
            validation_run_id=validation.id,
            mapping_version_id=validation.mapping_version_id,
            status=ReportRunStatus.COMPLETED,
            report_fingerprint="a" * 64,
            request_fingerprint="b" * 64,
            idempotency_key_hash="c" * 64,
            source_content_sha256=batch.content_hash,
            ruleset_fingerprint=validation.ruleset_fingerprint,
            template_version="legacy-test-v1",
            attention_mapping_version="f4-attention-v1",
            policy_manifest={},
            binding_manifest={},
            stored_row_count=validation.total_row_count,
            validated_row_count=validation.evaluated_row_count,
            flagged_row_count=validation.flagged_count,
            manual_review_row_count=validation.manual_review_count,
            passed_row_count=validation.passed_count,
            parse_error_row_count=validation.parse_failed_count,
            report_item_count=0,
            verified_citation_count=0,
            unavailable_citation_count=0,
            high_attention_row_count=validation.flagged_count,
            manual_attention_row_count=(
                validation.manual_review_count + validation.parse_failed_count
            ),
            cleared_row_count=validation.passed_count,
            created_by=actor_id,
            completed_at=utc_now(),
        )
        session.add(legacy)
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await create_legacy_sampling_plan(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_run_id=legacy.id,
            idempotency_key="legacy-plan-key-1",
        )
        await session.commit()

    def reject_resampling() -> str:
        raise AssertionError("completed plan replay accessed CSPRNG")

    monkeypatch.setattr("app.core.reviews.plan_service.generate_seed_hex", reject_resampling)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replayed = await create_legacy_sampling_plan(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_run_id=legacy.id,
            idempotency_key="legacy-plan-key-2",
        )
        await session.commit()
    assert replayed.id == created.id
    assert replayed.seed_hex == created.seed_hex
    assert replayed.selections == created.selections
    assert replayed.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingPlan)) == 1
        assert await session.scalar(select(func.count()).select_from(ReviewPlanRequest)) == 2
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "sampling.plan_create")
            )
            == 1
        )
        with pytest.raises(ReviewError) as reused:
            await create_legacy_sampling_plan(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_run_id=uuid.uuid4(),
                idempotency_key="legacy-plan-key-1",
            )
        assert reused.value.code == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize(
    "failure_stage", ["plan_written", "sample_written", "success_audit_written"]
)
async def test_auto_plan_faults_roll_back_report_plan_samples_and_success_audits(
    session_factory: async_sessionmaker[AsyncSession], failure_stage: str
) -> None:
    slug = f"f5-plan-fault-{failure_stage}-{uuid.uuid4().hex[:8]}"
    tenant_id, actor_id, batch_id = await _seed_validated_batch(session_factory, slug=slug)
    await _seed_policy_bindings(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        batch_id=batch_id,
    )

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError("simulated plan process loss")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReportInternalError):
            await generate_report(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                file_version_id=batch_id,
                idempotency_key=f"fault-{failure_stage}-key",
                fault_hook=fail,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReportRun)) == 0
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingPlan)) == 0
        assert await session.scalar(select(func.count()).select_from(SamplingAudit)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action.in_(["batch.report_generate", "sampling.plan_create"]))
            )
            == 0
        )
        failed = await session.scalar(
            select(AuditLog).where(AuditLog.action == "batch.report_failed")
        )
        assert failed is not None
        assert failed.payload_json is not None
        assert failed.payload_json["sampling_reason_code"] == "SAMPLING_PLAN_INTERNAL_ERROR"

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        recovered = await generate_report(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=batch_id,
            idempotency_key=f"recovered-{failure_stage}-key",
        )
        await session.commit()
    assert recovered.reused_existing is False


async def test_historical_plan_survives_config_change_and_sampling_review_replays_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-history-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        plan = await session.scalar(
            select(ReviewSamplingPlan).where(ReviewSamplingPlan.report_run_id == report_id)
        )
        sample = await session.scalar(
            select(SamplingAudit).where(SamplingAudit.report_run_id == report_id)
        )
        assert plan is not None and sample is not None
        frozen = (plan.config_fingerprint, plan.seed_hex, plan.sample_size)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await create_sampling_config(
            session,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=1,
            rate_bps=1,
            min_sample_size=1,
            max_sample_size=1,
            change_reason="future reports use a smaller sample",
            idempotency_key="history-config-version-2",
        )
        await session.commit()

    arguments = {
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "sampling_audit_id": sample.id,
        "decision": "clearance_confirmed",
        "note": None,
        "idempotency_key": "history-sampling-review-key",
    }
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        created = await submit_sampling_review(session, session_factory, **arguments)
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        replayed = await submit_sampling_review(session, session_factory, **arguments)
        await session.commit()
    assert replayed.id == created.id
    assert replayed.reused_existing is True

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as reused_key:
            await submit_sampling_review(
                session,
                session_factory,
                **(arguments | {"decision": "missed_issue", "note": "different request"}),
            )
        assert reused_key.value.code == "IDEMPOTENCY_KEY_REUSED"
        await session.rollback()
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as new_key:
            await submit_sampling_review(
                session,
                session_factory,
                **(arguments | {"idempotency_key": "history-sampling-review-new"}),
            )
        assert new_key.value.code == "SAMPLE_ALREADY_REVIEWED"
        await session.rollback()
        persisted_plan = await session.get(ReviewSamplingPlan, plan.id)
        assert persisted_plan is not None
        assert (
            persisted_plan.config_fingerprint,
            persisted_plan.seed_hex,
            persisted_plan.sample_size,
        ) == frozen


async def test_legacy_plan_failure_audit_is_independent_and_retryable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, report_id = await _create_legacy_report(
        session_factory, slug=f"f5-legacy-fault-{uuid.uuid4().hex[:8]}"
    )

    def fail(stage: str) -> None:
        if stage == "success_audit_written":
            raise RuntimeError("simulated legacy plan process loss")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewInternalError):
            await create_legacy_sampling_plan(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                report_run_id=report_id,
                idempotency_key="legacy-fault-plan-key",
                fault_hook=fail,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(ReviewSamplingPlan)) == 0
        assert await session.scalar(select(func.count()).select_from(SamplingAudit)) == 0
        failed = await session.scalar(
            select(AuditLog).where(AuditLog.action == "sampling.plan_failed")
        )
        assert failed is not None
        assert failed.payload_json == {"reason_code": "INTERNAL_ERROR"}

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        recovered = await create_legacy_sampling_plan(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            report_run_id=report_id,
            idempotency_key="legacy-retry-plan-key",
        )
        await session.commit()
    assert recovered.reused_existing is False


async def test_sampling_decision_kill_recovery_and_concurrent_submit_are_at_most_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, _batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-sample-recovery-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        sample = await session.scalar(
            select(SamplingAudit).where(SamplingAudit.report_run_id == report_id)
        )
    assert sample is not None

    def fail(stage: str) -> None:
        if stage == "success_audit_written":
            raise RuntimeError("simulated sampling review process loss")

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewInternalError):
            await submit_sampling_review(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                sampling_audit_id=sample.id,
                decision="clearance_confirmed",
                note=None,
                idempotency_key="sampling-fault-decision-key",
                fault_hook=fail,
            )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(SamplingReview)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "sampling.review_submit_failed")
            )
            == 1
        )

    first_session = session_factory()
    second_session = session_factory()
    try:
        bind_tenant(first_session.sync_session, tenant_id)
        bind_tenant(second_session.sync_session, tenant_id)
        created = await submit_sampling_review(
            first_session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            sampling_audit_id=sample.id,
            decision="clearance_confirmed",
            note=None,
            idempotency_key="sampling-recovery-decision-key",
        )
        with pytest.raises(ReviewError) as conflict:
            await submit_sampling_review(
                second_session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                sampling_audit_id=sample.id,
                decision="missed_issue",
                note="concurrent different conclusion",
                idempotency_key="sampling-concurrent-decision-key",
            )
        assert conflict.value.code == "REVIEW_CONFLICT"
        await second_session.rollback()
        await first_session.commit()
        assert created.reused_existing is False
    finally:
        await first_session.close()
        await second_session.close()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        assert await session.scalar(select(func.count()).select_from(SamplingReview)) == 1
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "sampling.review_submit")
            )
            == 1
        )


async def test_cross_tenant_review_targets_are_not_disclosed_or_modified(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a, actor_a, _batch_a, _report_a = await _create_report(
        session_factory, slug=f"f5-tenant-a-{uuid.uuid4().hex[:8]}"
    )
    tenant_b, _actor_b, _batch_b, report_b = await _create_report(
        session_factory, slug=f"f5-tenant-b-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_b)
        foreign_item = await session.scalar(
            select(ReportItem).where(ReportItem.report_run_id == report_b)
        )
    assert foreign_item is not None

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_a)
        with pytest.raises(ReviewError) as detail_error:
            await get_finding_review_detail(
                session,
                tenant_id=tenant_a,
                report_item_id=foreign_item.id,
            )
        assert detail_error.value.code == "REVIEW_TARGET_NOT_FOUND"
        with pytest.raises(ReviewError) as decision_error:
            await submit_finding_review(
                session,
                session_factory,
                tenant_id=tenant_a,
                actor_id=actor_a,
                report_item_id=foreign_item.id,
                decision="confirmed",
                note=None,
                idempotency_key="cross-tenant-review-key",
            )
        assert decision_error.value.code == "REVIEW_TARGET_NOT_FOUND"
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_b)
        assert await session.scalar(select(func.count()).select_from(Review)) == 0


async def test_sampling_decision_rejects_non_passed_row_even_if_selection_was_injected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, actor_id, batch_id, report_id = await _create_report(
        session_factory, slug=f"f5-invalid-sample-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        plan = await session.scalar(
            select(ReviewSamplingPlan).where(ReviewSamplingPlan.report_run_id == report_id)
        )
        assert plan is not None
        injected = SamplingAudit(
            tenant_id=tenant_id,
            sampling_plan_id=plan.id,
            report_run_id=report_id,
            file_version_id=batch_id,
            row_no=3,
            selection_rank=2,
            selection_score_sha256="9" * 64,
            decision=None,
            reviewer_id=None,
            reviewed_at=None,
        )
        session.add(injected)
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(ReviewError) as rejected:
            await submit_sampling_review(
                session,
                session_factory,
                tenant_id=tenant_id,
                actor_id=actor_id,
                sampling_audit_id=injected.id,
                decision="clearance_confirmed",
                note=None,
                idempotency_key="invalid-sample-decision-key",
            )
        assert rejected.value.code == "SAMPLE_NOT_FOUND"
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(SamplingReview)) == 0
