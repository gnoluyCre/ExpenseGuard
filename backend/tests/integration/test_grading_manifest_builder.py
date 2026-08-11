"""Focused CP-F8.3A manifest boundary tests."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.detection.run_service import run_detection
from app.core.grading.canonical import canonical_bytes
from app.core.grading.errors import GradingInputError, GradingNotFoundError
from app.core.grading.manifest_builder import (
    SOURCE_ROW_DOMAIN,
    build_grading_manifests,
    source_row_fingerprint,
)
from app.core.grading.models import InvestigationGradingOutcome
from app.core.grading.service_models import F7NotRunManifestRequest, F7RunManifestRequest
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.rules.models import LimitEvidence, RuleKind, RuleOutcome
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import FileVersion
from app.db.models.detection import CorrelationFindingRow
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding, Finding
from app.db.models.investigation import (
    InvestigationOutcome,
    InvestigationProviderKind,
    InvestigationResult,
    InvestigationRun,
)
from app.db.models.validation import ValidationRun, ValidationRunStatus
from tests.integration.test_detection_services import _create_profile, _seed_batch
from tests.unit.detection.helpers import record

pytestmark = pytest.mark.integration


def _normalized(mapping_version_id: uuid.UUID) -> NormalizedExpenseRecord:
    return record(amount="12.50").model_copy(update={"mapping_version_id": mapping_version_id})


async def _seed_complete_sources(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[
    uuid.UUID,
    uuid.UUID,
    uuid.UUID,
    uuid.UUID,
    uuid.UUID,
    tuple[F7RunManifestRequest | F7NotRunManifestRequest, ...],
]:
    tenant_id, actor_id, file_id = await _seed_batch(
        session_factory, slug=f"f8-manifest-{uuid.uuid4().hex[:8]}"
    )
    await _create_profile(session_factory, tenant_id, actor_id)

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        file = await session.scalar(select(FileVersion).where(FileVersion.id == file_id))
        assert file is not None and file.mapping_version_id is not None
        validation = ValidationRun(
            tenant_id=tenant_id,
            file_version_id=file_id,
            mapping_version_id=file.mapping_version_id,
            ruleset_fingerprint="4" * 64,
            ruleset_manifest={"schema_version": 1, "families": []},
            status=ValidationRunStatus.COMPLETED,
            total_row_count=3,
            evaluated_row_count=3,
            passed_count=2,
            flagged_count=1,
            manual_review_count=0,
            parse_failed_count=0,
            completed_at=datetime.now(UTC),
            triggered_by=actor_id,
        )
        session.add(validation)
        await session.flush()
        evidence = LimitEvidence(
            outcome=RuleOutcome.FLAGGED,
            rule_kind=RuleKind.LIMIT,
            reason_code="limit_exceeded",
            required_fields=("amount", "expense_type", "currency"),
            provenance={},
            amount="600",
            expense_type="差旅",
            currency="CNY",
            max_amount="500",
        )
        session.add(
            Finding(
                tenant_id=tenant_id,
                file_version_id=file_id,
                row_no=1,
                kind="limit_exceeded",
                severity_impact=0,
                severity_confidence=0,
                rule_id="expense-limit",
                rule_version="1",
                validation_run_id=validation.id,
                rule_kind=RuleKind.LIMIT,
                evidence_json=evidence.model_dump(mode="json"),
            )
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        detection = await run_detection(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            file_version_id=file_id,
            idempotency_key="f8-manifest-detection",
        )
        await session.commit()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        candidates = tuple(
            (
                await session.scalars(
                    select(CorrelationFinding)
                    .where(CorrelationFinding.detection_run_id == detection.id)
                    .order_by(CorrelationFinding.id)
                )
            ).all()
        )
        assert candidates
        investigated = candidates[0]
        investigation = InvestigationRun(
            tenant_id=tenant_id,
            correlation_finding_id=investigated.id,
            detection_run_id=detection.id,
            file_version_id=file_id,
            actor_id=actor_id,
            agent_version="react-v1",
            action_schema_version=1,
            prompt_template_version="prompt-v1",
            provider_kind=InvestigationProviderKind.DISABLED,
            provider_model="scripted-test",
            max_steps=6,
            timeout_seconds=30,
            redaction_version="v1",
            input_fingerprint="5" * 64,
            config_fingerprint="6" * 64,
        )
        session.add(investigation)
        await session.flush()
        result = InvestigationResult(
            tenant_id=tenant_id,
            investigation_run_id=investigation.id,
            correlation_finding_id=investigated.id,
            detection_run_id=detection.id,
            file_version_id=file_id,
            outcome=InvestigationOutcome.SUFFICIENT,
            evidence_sufficient=True,
            summary="证据充分",
            reason_code="EVIDENCE_SUFFICIENT",
            citations_json=[],
            result_fingerprint="7" * 64,
            completed_at=datetime.now(UTC),
        )
        session.add(result)
        await session.commit()
        requests: list[F7RunManifestRequest | F7NotRunManifestRequest] = [
            F7RunManifestRequest(
                correlation_finding_id=investigated.id,
                investigation_run_id=investigation.id,
                investigation_result_id=result.id,
                input_fingerprint=investigation.input_fingerprint,
                config_fingerprint=investigation.config_fingerprint,
                result_fingerprint=result.result_fingerprint,
                outcome=InvestigationGradingOutcome.SUFFICIENT,
                evidence_sufficient=True,
                citations=(),
            )
        ]
        requests.extend(
            F7NotRunManifestRequest(correlation_finding_id=item.id) for item in candidates[1:]
        )
        return (
            tenant_id,
            actor_id,
            file_id,
            validation.id,
            detection.id,
            tuple(requests),
        )


def test_source_row_fingerprint_uses_frozen_tenant_file_row_and_typed_projection() -> None:
    tenant_id = uuid.uuid4()
    file_version_id = uuid.uuid4()
    normalized = _normalized(uuid.uuid4())
    expected_payload = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "file_version_id": file_version_id,
        "row_no": 7,
        "normalized": normalized,
    }

    fingerprint = source_row_fingerprint(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        row_no=7,
        normalized=normalized,
    )

    assert (
        fingerprint
        == hashlib.sha256(SOURCE_ROW_DOMAIN + canonical_bytes(expected_payload)).hexdigest()
    )
    assert fingerprint != source_row_fingerprint(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        row_no=8,
        normalized=normalized,
    )


def test_f7_run_manifest_fails_closed_on_outcome_sufficiency_drift() -> None:
    common = {
        "correlation_finding_id": uuid.uuid4(),
        "investigation_run_id": uuid.uuid4(),
        "investigation_result_id": uuid.uuid4(),
        "input_fingerprint": "1" * 64,
        "config_fingerprint": "2" * 64,
        "result_fingerprint": "3" * 64,
        "outcome": InvestigationGradingOutcome.SUFFICIENT,
        "citations": (),
    }

    with pytest.raises(ValidationError, match="outcome/sufficiency mismatch"):
        F7RunManifestRequest.model_validate({**common, "evidence_sufficient": False})


async def test_builder_stops_after_bounded_file_lookup_when_identity_is_missing() -> None:
    db = AsyncMock()
    db.scalar.return_value = None

    with pytest.raises(GradingNotFoundError) as caught:
        await build_grading_manifests(
            db,
            tenant_id=uuid.uuid4(),
            file_version_id=uuid.uuid4(),
            validation_run_id=uuid.uuid4(),
            detection_run_id=uuid.uuid4(),
            f7_requests=(),
        )

    assert caught.value.code == "GRADING_FILE_NOT_FOUND"
    db.scalar.assert_awaited_once()
    db.execute.assert_not_awaited()


async def test_real_postgres_complete_path_is_stable_and_covers_all_sources(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> None:
    del clean_db
    tenant_id, _, file_id, validation_id, detection_id, requests = await _seed_complete_sources(
        session_factory
    )

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        first = await build_grading_manifests(
            session,
            tenant_id=tenant_id,
            file_version_id=file_id,
            validation_run_id=validation_id,
            detection_run_id=detection_id,
            f7_requests=tuple(reversed(requests)),
        )
        second = await build_grading_manifests(
            session,
            tenant_id=tenant_id,
            file_version_id=file_id,
            validation_run_id=validation_id,
            detection_run_id=detection_id,
            f7_requests=requests,
        )

    assert first == second
    assert len(first.f3_manifest.findings) == len(first.deterministic_sources) == 1
    assert len(first.f6_manifest.capabilities) == 4
    assert len(first.f6_manifest.candidates) == len(first.f7_manifest.entries)
    assert len(first.f6_manifest.candidates) == len(first.correlation_sources)
    assert tuple(item.detector for item in first.f6_manifest.capabilities) == (
        "split_invoice",
        "sequential_invoice",
        "frequency_anomaly",
        "spatiotemporal_tier0",
    )
    assert tuple(str(item.correlation_finding_id) for item in first.f7_manifest.entries) == tuple(
        sorted(str(item.correlation_finding_id) for item in first.f7_manifest.entries)
    )
    assert (
        first.f3_manifest_fingerprint
        == hashlib.sha256(canonical_bytes(first.f3_manifest)).hexdigest()
    )
    assert (
        first.f6_manifest_fingerprint
        == hashlib.sha256(canonical_bytes(first.f6_manifest)).hexdigest()
    )
    assert (
        first.f7_manifest_fingerprint
        == hashlib.sha256(canonical_bytes(first.f7_manifest)).hexdigest()
    )
    deterministic_row = first.deterministic_sources[0].row
    assert (
        deterministic_row.source_row_fingerprint
        == first.f6_manifest.candidates[0].participating_rows[0].source_row_fingerprint
    )


@pytest.mark.parametrize("request_drift", ["incomplete", "duplicate", "result"])
async def test_real_postgres_f7_manifest_drift_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
    request_drift: str,
) -> None:
    del clean_db
    tenant_id, _, file_id, validation_id, detection_id, requests = await _seed_complete_sources(
        session_factory
    )
    if request_drift == "incomplete":
        changed = requests[:-1]
        expected_code = "GRADING_F7_MANIFEST_INCOMPLETE"
    elif request_drift == "duplicate":
        changed = (*requests, requests[0])
        expected_code = "GRADING_F7_MANIFEST_INCOMPLETE"
    else:
        run_request = next(item for item in requests if isinstance(item, F7RunManifestRequest))
        changed = tuple(
            item.model_copy(update={"result_fingerprint": "8" * 64})
            if item is run_request
            else item
            for item in requests
        )
        expected_code = "GRADING_F7_RESULT_DRIFT"

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        with pytest.raises(GradingInputError) as caught:
            await build_grading_manifests(
                session,
                tenant_id=tenant_id,
                file_version_id=file_id,
                validation_run_id=validation_id,
                detection_run_id=detection_id,
                f7_requests=changed,
            )
    assert caught.value.code == expected_code


@pytest.mark.parametrize(
    "source_drift", ["physical_row", "capability_count", "capability_details", "source_row"]
)
async def test_real_postgres_source_fact_drift_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
    source_drift: str,
) -> None:
    del clean_db
    tenant_id, _, file_id, validation_id, detection_id, requests = await _seed_complete_sources(
        session_factory
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        if source_drift == "physical_row":
            row_id = await session.scalar(
                select(CorrelationFindingRow.id)
                .where(CorrelationFindingRow.detection_run_id == detection_id)
                .order_by(CorrelationFindingRow.id)
                .limit(1)
            )
            assert row_id is not None
            await session.execute(
                text("DELETE FROM correlation_finding_row WHERE id = :row_id"), {"row_id": row_id}
            )
            expected_code = "GRADING_F6_CANDIDATE_DRIFT"
        elif source_drift == "capability_count":
            declaration_id = await session.scalar(
                select(CapabilityDeclaration.id)
                .where(CapabilityDeclaration.detection_run_id == detection_id)
                .order_by(CapabilityDeclaration.id)
                .limit(1)
            )
            assert declaration_id is not None
            await session.execute(
                text(
                    "UPDATE capability_declaration "
                    "SET finding_count = finding_count + 1 WHERE id = :declaration_id"
                ),
                {"declaration_id": declaration_id},
            )
            expected_code = "GRADING_F6_CAPABILITY_DRIFT"
        elif source_drift == "capability_details":
            declaration_id = await session.scalar(
                select(CapabilityDeclaration.id)
                .where(CapabilityDeclaration.detection_run_id == detection_id)
                .order_by(CapabilityDeclaration.id)
                .limit(1)
            )
            assert declaration_id is not None
            await session.execute(
                text(
                    "UPDATE capability_declaration SET details_json = '{}'::jsonb "
                    "WHERE id = :declaration_id"
                ),
                {"declaration_id": declaration_id},
            )
            expected_code = "GRADING_F6_CAPABILITY_DRIFT"
        else:
            await session.execute(
                text(
                    "UPDATE expense_row SET normalized_json = "
                    "jsonb_set(normalized_json, '{mapping_version_id}', "
                    "to_jsonb(CAST(:wrong AS text))) "
                    "WHERE file_version_id = :file_id AND row_no = 1"
                ),
                {"wrong": str(uuid.uuid4()), "file_id": file_id},
            )
            expected_code = "GRADING_SOURCE_ROW_DRIFT"
        await session.execute(text("SET LOCAL session_replication_role = origin"))

        with pytest.raises(GradingInputError) as caught:
            await build_grading_manifests(
                session,
                tenant_id=tenant_id,
                file_version_id=file_id,
                validation_run_id=validation_id,
                detection_run_id=detection_id,
                f7_requests=requests,
            )
    assert caught.value.code == expected_code


async def test_real_postgres_detection_count_drift_fails_closed(
    session_factory: async_sessionmaker[AsyncSession], clean_db: None
) -> None:
    del clean_db
    tenant_id, _, file_id, validation_id, detection_id, requests = await _seed_complete_sources(
        session_factory
    )
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(
            text(
                "UPDATE detection_run SET finding_count = finding_count + 1 "
                "WHERE id = :detection_id"
            ),
            {"detection_id": detection_id},
        )
        await session.execute(text("SET LOCAL session_replication_role = origin"))
        with pytest.raises(GradingInputError) as caught:
            await build_grading_manifests(
                session,
                tenant_id=tenant_id,
                file_version_id=file_id,
                validation_run_id=validation_id,
                detection_run_id=detection_id,
                f7_requests=requests,
            )
    assert caught.value.code == "GRADING_F6_CANDIDATE_DRIFT"
