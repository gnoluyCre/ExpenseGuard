"""Build immutable F3/F6/F7 manifests from explicitly selected source runs."""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.detection.canonical import finding_key
from app.core.detection.models import (
    CORRELATION_EVIDENCE_ADAPTER,
    DETECTOR_ORDER,
    CapabilityDetails,
    CapabilityDraft,
    CapabilityStatus,
    DetectorKind,
)
from app.core.grading.canonical import canonical_bytes
from app.core.grading.errors import GradingInputError, GradingNotFoundError
from app.core.grading.models import (
    CorrelationGradingSource,
    DeterministicGradingSource,
    InvestigationGradingOutcome,
    InvestigationNotRunSource,
    InvestigationRunSource,
    InvestigationSource,
    ParticipantRow,
)
from app.core.grading.service_models import (
    CitationManifestItem,
    F3FindingManifestItem,
    F3Manifest,
    F6CandidateManifestItem,
    F6CapabilityManifestItem,
    F6Manifest,
    F6ParticipantManifestItem,
    F7Manifest,
    F7ManifestRequest,
    F7NotRunManifestRequest,
    F7RunManifestRequest,
    GradingManifestBundle,
)
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.rules.models import RuleEvidence, RuleKind, RuleOutcome
from app.db.models.batch import ExpenseRow, FileVersion
from app.db.models.detection import CorrelationFindingRow, DetectionRun
from app.db.models.findings import CapabilityDeclaration, CorrelationFinding, Finding
from app.db.models.investigation import InvestigationResult, InvestigationRun
from app.db.models.validation import ValidationRun, ValidationRunStatus

F3_MAX_BYTES = 8 * 1024 * 1024
F6_MAX_BYTES = 16 * 1024 * 1024
F7_MAX_BYTES = 32 * 1024 * 1024
SOURCE_ROW_DOMAIN = b"expenseguard-grading-source-row-v1\0"
_RULE_EVIDENCE_ADAPTER: TypeAdapter[RuleEvidence] = TypeAdapter(RuleEvidence)
_RULE_RANK = {kind: rank for rank, kind in enumerate(RuleKind)}
_DETECTOR_RANK = {kind: rank for rank, kind in enumerate(DETECTOR_ORDER)}


def _input_error(code: str) -> GradingInputError:
    return GradingInputError(code=code, message="二维分级输入无效或已漂移")


def _plain_fingerprint(value: object, maximum: int) -> str:
    encoded = canonical_bytes(value)
    if len(encoded) > maximum:
        raise _input_error("GRADING_MANIFEST_TOO_LARGE")
    return hashlib.sha256(encoded).hexdigest()


def source_row_fingerprint(
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    row_no: int,
    normalized: NormalizedExpenseRecord,
) -> str:
    payload = {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "file_version_id": file_version_id,
        "row_no": row_no,
        "normalized": normalized,
    }
    return hashlib.sha256(SOURCE_ROW_DOMAIN + canonical_bytes(payload)).hexdigest()


def _validated_row(
    rows: dict[int, dict[str, object] | None],
    *,
    row_no: int,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    mapping_version_id: uuid.UUID,
) -> ParticipantRow:
    raw = rows.get(row_no)
    if raw is None:
        raise _input_error("GRADING_SOURCE_ROW_DRIFT")
    try:
        normalized = NormalizedExpenseRecord.model_validate(raw)
    except ValidationError as exc:
        raise _input_error("GRADING_SOURCE_ROW_DRIFT") from exc
    if normalized.mapping_version_id != mapping_version_id:
        raise _input_error("GRADING_SOURCE_ROW_DRIFT")
    return ParticipantRow(
        row_no=row_no,
        source_row_fingerprint=source_row_fingerprint(
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            row_no=row_no,
            normalized=normalized,
        ),
    )


async def build_grading_manifests(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    f7_requests: Sequence[F7ManifestRequest],
) -> GradingManifestBundle:
    """Load every source collection in bounded bulk queries and fail closed on drift."""

    file = await db.scalar(
        select(FileVersion).where(
            FileVersion.id == file_version_id, FileVersion.tenant_id == tenant_id
        )
    )
    if file is None:
        raise GradingNotFoundError(code="GRADING_FILE_NOT_FOUND", message="批次不存在")
    validation = await db.scalar(
        select(ValidationRun).where(
            ValidationRun.id == validation_run_id,
            ValidationRun.tenant_id == tenant_id,
            ValidationRun.file_version_id == file_version_id,
        )
    )
    if validation is None:
        raise GradingNotFoundError(code="GRADING_SOURCE_RUN_NOT_FOUND", message="校验运行不存在")
    if validation.status is not ValidationRunStatus.COMPLETED or validation.completed_at is None:
        raise _input_error("GRADING_SOURCE_RUN_NOT_COMPLETED")
    if file.mapping_version_id != validation.mapping_version_id:
        raise _input_error("GRADING_SOURCE_RUN_DRIFT")
    detection = await db.scalar(
        select(DetectionRun).where(
            DetectionRun.id == detection_run_id,
            DetectionRun.tenant_id == tenant_id,
            DetectionRun.file_version_id == file_version_id,
        )
    )
    if detection is None:
        raise GradingNotFoundError(
            code="GRADING_SOURCE_RUN_NOT_FOUND", message="关联检测运行不存在"
        )

    findings = tuple(
        (
            await db.scalars(
                select(Finding).where(
                    Finding.tenant_id == tenant_id,
                    Finding.file_version_id == file_version_id,
                    Finding.validation_run_id == validation_run_id,
                )
            )
        ).all()
    )
    capabilities = tuple(
        (
            await db.scalars(
                select(CapabilityDeclaration).where(
                    CapabilityDeclaration.tenant_id == tenant_id,
                    CapabilityDeclaration.file_version_id == file_version_id,
                    CapabilityDeclaration.detection_run_id == detection_run_id,
                )
            )
        ).all()
    )
    candidates = tuple(
        (
            await db.scalars(
                select(CorrelationFinding).where(
                    CorrelationFinding.tenant_id == tenant_id,
                    CorrelationFinding.file_version_id == file_version_id,
                    CorrelationFinding.detection_run_id == detection_run_id,
                )
            )
        ).all()
    )
    if detection.finding_count != len(candidates):
        raise _input_error("GRADING_F6_CANDIDATE_DRIFT")
    physical_rows = tuple(
        (
            await db.scalars(
                select(CorrelationFindingRow).where(
                    CorrelationFindingRow.tenant_id == tenant_id,
                    CorrelationFindingRow.file_version_id == file_version_id,
                    CorrelationFindingRow.detection_run_id == detection_run_id,
                )
            )
        ).all()
    )
    rows = {
        row_no: normalized
        for row_no, normalized in (
            await db.execute(
                select(ExpenseRow.row_no, ExpenseRow.normalized_json).where(
                    ExpenseRow.tenant_id == tenant_id,
                    ExpenseRow.file_version_id == file_version_id,
                )
            )
        ).all()
    }

    f3_items: list[F3FindingManifestItem] = []
    deterministic_sources: list[DeterministicGradingSource] = []
    for finding in findings:
        if finding.evidence_json is None or finding.row_no is None or finding.rule_kind is None:
            raise _input_error("GRADING_F3_SOURCE_DRIFT")
        try:
            f3_evidence = _RULE_EVIDENCE_ADAPTER.validate_python(finding.evidence_json)
        except ValidationError as exc:
            raise _input_error("GRADING_F3_SOURCE_DRIFT") from exc
        persisted_rule_kind = RuleKind(finding.rule_kind)
        if f3_evidence.rule_kind is not persisted_rule_kind:
            raise _input_error("GRADING_F3_SOURCE_DRIFT")
        if f3_evidence.outcome is RuleOutcome.EXEMPTED:
            continue
        if finding.rule_id is None:
            raise _input_error("GRADING_F3_SOURCE_DRIFT")
        evidence_fp = _plain_fingerprint(f3_evidence, 256 * 1024)
        participant = _validated_row(
            rows,
            row_no=finding.row_no,
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            mapping_version_id=validation.mapping_version_id,
        )
        item = F3FindingManifestItem(
            finding_id=finding.id,
            row_no=finding.row_no,
            rule_id=finding.rule_id,
            rule_version=finding.rule_version,
            rule_kind=f3_evidence.rule_kind,
            outcome=f3_evidence.outcome,
            reason_code=f3_evidence.reason_code,
            evidence_fingerprint=evidence_fp,
        )
        f3_items.append(item)
        deterministic_sources.append(
            DeterministicGradingSource(
                finding_id=finding.id,
                row=participant,
                rule_kind=f3_evidence.rule_kind,
                outcome=f3_evidence.outcome,
                evidence_fingerprint=evidence_fp,
            )
        )
    f3_items.sort(
        key=lambda item: (
            item.row_no,
            _RULE_RANK[item.rule_kind],
            item.rule_id,
            item.rule_version or "",
            str(item.finding_id),
        )
    )
    deterministic_sources.sort(key=lambda item: (item.first_row_no, str(item.finding_id)))
    f3_manifest = F3Manifest(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        file_content_hash=file.content_hash,
        file_revision_no=file.revision_no,
        validation_run_id=validation.id,
        mapping_version_id=validation.mapping_version_id,
        ruleset_fingerprint=validation.ruleset_fingerprint,
        ruleset_manifest_fingerprint=_plain_fingerprint(
            validation.ruleset_manifest, 8 * 1024 * 1024
        ),
        findings=tuple(f3_items),
    )

    cap_by_detector = {DetectorKind(item.detector): item for item in capabilities}
    if len(cap_by_detector) != 4 or set(cap_by_detector) != set(DETECTOR_ORDER):
        raise _input_error("GRADING_F6_CAPABILITY_DRIFT")
    row_groups: dict[uuid.UUID, list[CorrelationFindingRow]] = defaultdict(list)
    for physical in physical_rows:
        row_groups[physical.finding_id].append(physical)
    actual_counts = Counter(DetectorKind(item.detector) for item in candidates)
    cap_manifest_items: list[F6CapabilityManifestItem] = []
    for detector in DETECTOR_ORDER:
        declaration = cap_by_detector[detector]
        if declaration.config_fingerprint != detection.config_fingerprint:
            raise _input_error("GRADING_F6_CAPABILITY_DRIFT")
        try:
            details = CapabilityDetails.model_validate(declaration.details_json)
            draft = CapabilityDraft(
                detector=detector,
                detector_version=declaration.detector_version,
                status=CapabilityStatus(declaration.status),
                reason_code=declaration.reason_code,
                reason_snapshot=declaration.reason,
                details=details,
                finding_count=declaration.finding_count,
            )
        except ValidationError as exc:
            raise _input_error("GRADING_F6_CAPABILITY_DRIFT") from exc
        if draft.finding_count != actual_counts[detector]:
            raise _input_error("GRADING_F6_CAPABILITY_DRIFT")
        cap_manifest_items.append(
            F6CapabilityManifestItem(
                declaration_id=declaration.id,
                detector=detector,
                detector_version=draft.detector_version,
                status=draft.status,
                config_fingerprint=declaration.config_fingerprint,
                reason_code=draft.reason_code,
                details_fingerprint=_plain_fingerprint(details, 256 * 1024),
                finding_count=draft.finding_count,
            )
        )

    candidate_manifest_items: list[F6CandidateManifestItem] = []
    candidate_parts: dict[uuid.UUID, tuple[ParticipantRow, ...]] = {}
    candidate_evidence: dict[uuid.UUID, str] = {}
    for candidate in candidates:
        try:
            f6_evidence = CORRELATION_EVIDENCE_ADAPTER.validate_python(candidate.evidence_json)
        except ValidationError as exc:
            raise _input_error("GRADING_F6_CANDIDATE_DRIFT") from exc
        if (
            f6_evidence.detector is not DetectorKind(candidate.detector)
            or f6_evidence.detector_version != candidate.detector_version
            or f6_evidence.profile_fingerprint != detection.config_fingerprint
        ):
            raise _input_error("GRADING_F6_CANDIDATE_DRIFT")
        candidate_rows = sorted(row_groups.get(candidate.id, []), key=lambda item: item.ordinal)
        if [item.ordinal for item in candidate_rows] != list(
            range(1, len(candidate_rows) + 1)
        ) or len(candidate_rows) < 2:
            raise _input_error("GRADING_F6_CANDIDATE_DRIFT")
        participants = tuple(
            _validated_row(
                rows,
                row_no=item.row_no,
                tenant_id=tenant_id,
                file_version_id=file_version_id,
                mapping_version_id=validation.mapping_version_id,
            )
            for item in candidate_rows
        )
        if tuple(sorted(participants, key=lambda item: item.row_no)) != participants:
            raise _input_error("GRADING_F6_CANDIDATE_DRIFT")
        recomputed_key = finding_key(
            detector=DetectorKind(candidate.detector),
            detector_version=candidate.detector_version,
            profile_fingerprint_value=detection.config_fingerprint,
            participating_row_nos=[item.row_no for item in participants],
            evidence=f6_evidence,
        )
        if recomputed_key != candidate.finding_key:
            raise _input_error("GRADING_F6_CANDIDATE_DRIFT")
        evidence_fp = _plain_fingerprint(f6_evidence, 256 * 1024)
        candidate_parts[candidate.id] = participants
        candidate_evidence[candidate.id] = evidence_fp
        candidate_manifest_items.append(
            F6CandidateManifestItem(
                correlation_finding_id=candidate.id,
                detector=DetectorKind(candidate.detector),
                detector_version=candidate.detector_version,
                finding_key=candidate.finding_key,
                evidence_fingerprint=evidence_fp,
                participating_rows=tuple(
                    F6ParticipantManifestItem(
                        ordinal=index,
                        row_no=participant.row_no,
                        source_row_fingerprint=participant.source_row_fingerprint,
                    )
                    for index, participant in enumerate(participants, start=1)
                ),
                first_row_no=participants[0].row_no,
            )
        )
    candidate_manifest_items.sort(
        key=lambda item: (
            _DETECTOR_RANK[item.detector],
            item.first_row_no,
            item.finding_key,
            str(item.correlation_finding_id),
        )
    )
    f6_manifest = F6Manifest(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        detection_run_id=detection.id,
        detection_config_id=detection.detection_config_id,
        config_version=detection.config_version,
        config_fingerprint=detection.config_fingerprint,
        algorithm_bundle_version=detection.algorithm_bundle_version,
        input_fingerprint=detection.input_fingerprint,
        run_fingerprint=detection.run_fingerprint,
        capabilities=tuple(cap_manifest_items),
        candidates=tuple(candidate_manifest_items),
    )

    request_by_candidate = {item.correlation_finding_id: item for item in f7_requests}
    expected_ids = set(candidate_parts)
    if len(request_by_candidate) != len(f7_requests) or set(request_by_candidate) != expected_ids:
        raise _input_error("GRADING_F7_MANIFEST_INCOMPLETE")
    run_requests = tuple(item for item in f7_requests if isinstance(item, F7RunManifestRequest))
    result_pairs: dict[uuid.UUID, tuple[InvestigationRun, InvestigationResult]] = {}
    if run_requests:
        result_ids = [item.investigation_result_id for item in run_requests]
        pairs = (
            await db.execute(
                select(InvestigationRun, InvestigationResult)
                .join(
                    InvestigationResult,
                    InvestigationResult.investigation_run_id == InvestigationRun.id,
                )
                .where(
                    InvestigationRun.tenant_id == tenant_id,
                    InvestigationResult.tenant_id == tenant_id,
                    InvestigationResult.id.in_(result_ids),
                )
            )
        ).all()
        result_pairs = {result.id: (run, result) for run, result in pairs}

    correlation_sources: list[CorrelationGradingSource] = []
    ordered_requests = tuple(sorted(f7_requests, key=lambda item: str(item.correlation_finding_id)))
    candidate_by_id = {item.id: item for item in candidates}
    for request in ordered_requests:
        candidate = candidate_by_id[request.correlation_finding_id]
        if isinstance(request, F7NotRunManifestRequest):
            investigation: InvestigationSource = InvestigationNotRunSource()
        else:
            pair = result_pairs.get(request.investigation_result_id)
            if pair is None:
                raise _input_error("GRADING_F7_RESULT_DRIFT")
            run, result = pair
            try:
                stored_citations = tuple(
                    sorted(
                        (
                            CitationManifestItem.model_validate(item)
                            for item in result.citations_json
                        ),
                        key=lambda item: (
                            str(item.clause_id),
                            item.quote_start,
                            item.quote_end,
                            item.quote,
                        ),
                    )
                )
            except ValidationError as exc:
                raise _input_error("GRADING_F7_RESULT_DRIFT") from exc
            identity_matches = (
                run.id == request.investigation_run_id
                and run.correlation_finding_id == candidate.id
                and run.detection_run_id == detection_run_id
                and run.file_version_id == file_version_id
                and run.input_fingerprint == request.input_fingerprint
                and run.config_fingerprint == request.config_fingerprint
                and result.investigation_run_id == run.id
                and result.correlation_finding_id == candidate.id
                and result.detection_run_id == detection_run_id
                and result.file_version_id == file_version_id
                and result.result_fingerprint == request.result_fingerprint
                and InvestigationGradingOutcome(result.outcome) is request.outcome
                and result.evidence_sufficient is request.evidence_sufficient
                and stored_citations == request.citations
            )
            if not identity_matches:
                raise _input_error("GRADING_F7_RESULT_DRIFT")
            investigation = InvestigationRunSource(
                outcome=request.outcome, evidence_sufficient=request.evidence_sufficient
            )
        candidate_detector = DetectorKind(candidate.detector)
        capability = cap_by_detector[candidate_detector]
        correlation_sources.append(
            CorrelationGradingSource(
                correlation_finding_id=candidate.id,
                detector=candidate_detector,
                finding_key=candidate.finding_key,
                evidence_fingerprint=candidate_evidence[candidate.id],
                participating_rows=candidate_parts[candidate.id],
                capability_status=CapabilityStatus(capability.status),
                investigation=investigation,
            )
        )
    f7_manifest = F7Manifest(
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        detection_run_id=detection_run_id,
        entries=ordered_requests,
    )
    return GradingManifestBundle(
        f3_manifest=f3_manifest,
        f3_manifest_fingerprint=_plain_fingerprint(f3_manifest, F3_MAX_BYTES),
        f6_manifest=f6_manifest,
        f6_manifest_fingerprint=_plain_fingerprint(f6_manifest, F6_MAX_BYTES),
        f7_manifest=f7_manifest,
        f7_manifest_fingerprint=_plain_fingerprint(f7_manifest, F7_MAX_BYTES),
        deterministic_sources=tuple(deterministic_sources),
        correlation_sources=tuple(correlation_sources),
    )
