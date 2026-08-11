"""Strong service contracts for F8 manifest, run, and query orchestration."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from app.core.detection.models import CapabilityStatus, DetectorKind
from app.core.grading.errors import (
    GradingError,
    GradingInputError,
    GradingInternalError,
    GradingNotFoundError,
    GradingUnavailableError,
)
from app.core.grading.models import (
    CorrelationGradingSource,
    DeterministicGradingSource,
    Disposition,
    GradingConfigV1,
    GradingEvidenceSnapshot,
    GradingReasonCode,
    InvestigationGradingOutcome,
    StrictGradingModel,
)
from app.core.parsing.models import NormalizedExpenseRecord
from app.core.rules.models import RuleKind, RuleOutcome

GradingServiceError = GradingError

__all__ = [
    "GradingError",
    "GradingInputError",
    "GradingInternalError",
    "GradingNotFoundError",
    "GradingServiceError",
    "GradingUnavailableError",
]


class CitationManifestItem(StrictGradingModel):
    schema_version: Literal[1] = 1
    clause_id: uuid.UUID
    quote_start: int = Field(ge=0)
    quote_end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=4096)

    @field_validator("quote")
    @classmethod
    def quote_has_no_controls(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("citation quote contains a control character")
        return value

    @model_validator(mode="after")
    def quote_range_is_valid(self) -> CitationManifestItem:
        if self.quote_end <= self.quote_start:
            raise ValueError("citation quote range must be non-empty")
        return self


class F7RunManifestRequest(StrictGradingModel):
    kind: Literal["run"] = "run"
    correlation_finding_id: uuid.UUID
    investigation_run_id: uuid.UUID
    investigation_result_id: uuid.UUID
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: InvestigationGradingOutcome
    evidence_sufficient: bool | None
    citations: tuple[CitationManifestItem, ...] = Field(max_length=100)

    @field_validator("citations")
    @classmethod
    def citations_are_unique_and_sorted(
        cls, values: tuple[CitationManifestItem, ...]
    ) -> tuple[CitationManifestItem, ...]:
        keys = tuple(
            (str(item.clause_id), item.quote_start, item.quote_end, item.quote) for item in values
        )
        if len(keys) != len(set(keys)) or keys != tuple(sorted(keys)):
            raise ValueError("citations must be unique and use stable order")
        return values

    @model_validator(mode="after")
    def outcome_matches_sufficiency(self) -> F7RunManifestRequest:
        expected = {
            InvestigationGradingOutcome.SUFFICIENT: True,
            InvestigationGradingOutcome.INSUFFICIENT: False,
            InvestigationGradingOutcome.UNAVAILABLE: None,
            InvestigationGradingOutcome.MAX_STEPS: None,
            InvestigationGradingOutcome.FAILED: None,
        }[self.outcome]
        if self.evidence_sufficient is not expected:
            raise ValueError("investigation outcome/sufficiency mismatch")
        return self


class F7NotRunManifestRequest(StrictGradingModel):
    kind: Literal["not_run"] = "not_run"
    correlation_finding_id: uuid.UUID
    reason_code: Literal["INVESTIGATION_NOT_RUN"] = "INVESTIGATION_NOT_RUN"


type F7ManifestRequest = Annotated[
    F7RunManifestRequest | F7NotRunManifestRequest, Field(discriminator="kind")
]


class F3FindingManifestItem(StrictGradingModel):
    finding_id: uuid.UUID
    row_no: int = Field(ge=1)
    rule_id: str = Field(min_length=1, max_length=128)
    rule_version: str | None = Field(default=None, max_length=64)
    rule_kind: RuleKind
    outcome: Literal[RuleOutcome.FLAGGED, RuleOutcome.UNAVAILABLE]
    reason_code: str = Field(min_length=1, max_length=64)
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class F3Manifest(StrictGradingModel):
    schema_version: Literal[1] = 1
    tenant_id: uuid.UUID
    file_version_id: uuid.UUID
    file_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_revision_no: int = Field(ge=1)
    validation_run_id: uuid.UUID
    mapping_version_id: uuid.UUID
    ruleset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    ruleset_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    findings: tuple[F3FindingManifestItem, ...] = Field(max_length=5000)


class F6CapabilityManifestItem(StrictGradingModel):
    declaration_id: uuid.UUID
    detector: DetectorKind
    detector_version: str = Field(min_length=1, max_length=32)
    status: CapabilityStatus
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=1, max_length=64)
    details_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_count: int = Field(ge=0)


class F6ParticipantManifestItem(StrictGradingModel):
    ordinal: int = Field(ge=1)
    row_no: int = Field(ge=1)
    source_row_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class F6CandidateManifestItem(StrictGradingModel):
    correlation_finding_id: uuid.UUID
    detector: DetectorKind
    detector_version: str = Field(min_length=1, max_length=32)
    finding_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    participating_rows: tuple[F6ParticipantManifestItem, ...] = Field(min_length=2, max_length=5000)
    first_row_no: int = Field(ge=1)


class F6Manifest(StrictGradingModel):
    schema_version: Literal[1] = 1
    tenant_id: uuid.UUID
    file_version_id: uuid.UUID
    detection_run_id: uuid.UUID
    detection_config_id: uuid.UUID
    config_version: int = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    algorithm_bundle_version: str = Field(min_length=1, max_length=32)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    capabilities: tuple[F6CapabilityManifestItem, ...] = Field(min_length=4, max_length=4)
    candidates: tuple[F6CandidateManifestItem, ...] = Field(max_length=20000)


class F7Manifest(StrictGradingModel):
    schema_version: Literal[1] = 1
    tenant_id: uuid.UUID
    file_version_id: uuid.UUID
    detection_run_id: uuid.UUID
    entries: tuple[F7ManifestRequest, ...] = Field(max_length=20000)


class GradingManifestBundle(StrictGradingModel):
    f3_manifest: F3Manifest
    f3_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    f6_manifest: F6Manifest
    f6_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    f7_manifest: F7Manifest
    f7_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_sources: tuple[DeterministicGradingSource, ...]
    correlation_sources: tuple[CorrelationGradingSource, ...]


class GradingConfigView(StrictGradingModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    schema_version: int
    definition: GradingConfigV1
    config_fingerprint: str
    algorithm_version: str
    created_by: uuid.UUID
    change_reason: str
    created_at: datetime
    reused_existing: bool


class GradingConfigPage(StrictGradingModel):
    items: tuple[GradingConfigView, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class GradingRunView(StrictGradingModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    file_version_id: uuid.UUID
    validation_run_id: uuid.UUID
    detection_run_id: uuid.UUID
    grading_config_id: uuid.UUID
    created_by: uuid.UUID
    config_version: int
    config_fingerprint: str
    algorithm_version: str
    f3_manifest_fingerprint: str
    f6_manifest_fingerprint: str
    f7_manifest_fingerprint: str
    input_fingerprint: str
    deterministic_item_count: int
    correlation_item_count: int
    high_attention_count: int
    manual_attention_count: int
    cleared_count: int
    created_at: datetime
    completed_at: datetime
    reused_existing: bool


class BatchGradingView(StrictGradingModel):
    file_version_id: uuid.UUID
    run: GradingRunView | None
    current_config_id: uuid.UUID | None
    current_validation_run_id: uuid.UUID | None
    current_detection_run_id: uuid.UUID | None
    config_stale: bool
    validation_run_stale: bool
    detection_run_stale: bool


class GradingItemView(StrictGradingModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    grading_run_id: uuid.UUID
    file_version_id: uuid.UUID
    source_kind: Literal["deterministic", "correlation"]
    finding_id: uuid.UUID | None
    correlation_finding_id: uuid.UUID | None
    investigation_run_id: uuid.UUID | None
    rule_kind: RuleKind | None
    detector: DetectorKind | None
    f3_outcome: RuleOutcome | None
    f6_capability_status: CapabilityStatus | None
    f7_outcome: InvestigationGradingOutcome | None
    severity_impact: int = Field(ge=0, le=3)
    severity_confidence: int = Field(ge=0, le=3)
    disposition: Disposition
    reason_codes: tuple[GradingReasonCode, ...]
    evidence_snapshot: GradingEvidenceSnapshot
    item_fingerprint: str
    first_row_no: int
    created_at: datetime


class GradingItemPage(StrictGradingModel):
    items: tuple[GradingItemView, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class GradingRowView(StrictGradingModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    grading_run_id: uuid.UUID
    grading_item_id: uuid.UUID
    file_version_id: uuid.UUID
    row_no: int
    ordinal: int
    source_row_fingerprint: str
    raw: dict[str, Any]
    normalized: NormalizedExpenseRecord
    created_at: datetime


class GradingRowPage(StrictGradingModel):
    items: tuple[GradingRowView, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
