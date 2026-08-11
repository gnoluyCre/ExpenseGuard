"""Frozen contracts for deterministic F8 two-dimensional grading."""

from __future__ import annotations

import uuid
from enum import IntEnum, StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.core.detection.models import CapabilityStatus, DetectorKind
from app.core.rules.models import RuleKind, RuleOutcome

MAX_CANONICAL_BYTES = 256 * 1024
MAX_SOURCE_CANONICAL_BYTES = 16 * 1024 * 1024
MAX_REASON_CODES = 16
MAX_SAFE_INTEGER = 1_000_000
MAX_BASIS_POINTS = 10_000


def _strict_integer(value: object) -> object:
    if type(value) is not int:
        raise ValueError("value must be a strict integer")
    return value


class SeverityLevel(IntEnum):
    LEVEL_0 = 0
    LEVEL_1 = 1
    LEVEL_2 = 2
    LEVEL_3 = 3


def _strict_level(value: object) -> object:
    if isinstance(value, SeverityLevel):
        return value
    if type(value) is not int:
        raise ValueError("severity level must be an integer from 0 through 3")
    return value


StrictLevel = Annotated[SeverityLevel, BeforeValidator(_strict_level)]
CostUnit = Annotated[int, BeforeValidator(_strict_integer), Field(ge=0, le=MAX_SAFE_INTEGER)]
PositiveCostUnit = Annotated[
    int, BeforeValidator(_strict_integer), Field(ge=1, le=MAX_SAFE_INTEGER)
]
BasisPoints = Annotated[int, BeforeValidator(_strict_integer), Field(ge=0, le=MAX_BASIS_POINTS)]


class StrictGradingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceKind(StrEnum):
    DETERMINISTIC = "deterministic"
    CORRELATION = "correlation"


SOURCE_KIND_ORDER: tuple[SourceKind, ...] = (
    SourceKind.DETERMINISTIC,
    SourceKind.CORRELATION,
)


class Disposition(StrEnum):
    HIGH_ATTENTION = "high_attention"
    MANUAL_ATTENTION = "manual_attention"
    CLEARED = "cleared"


DISPOSITION_ORDER: tuple[Disposition, ...] = (
    Disposition.HIGH_ATTENTION,
    Disposition.MANUAL_ATTENTION,
    Disposition.CLEARED,
)


class InvestigationGradingOutcome(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    MAX_STEPS = "max_steps"
    FAILED = "failed"
    NOT_RUN = "not_run"


class GradingReasonCode(StrEnum):
    IMPACT_RULE_MAPPING = "IMPACT_RULE_MAPPING"
    IMPACT_DETECTOR_MAPPING = "IMPACT_DETECTOR_MAPPING"
    CONFIDENCE_F3_FLAGGED = "CONFIDENCE_F3_FLAGGED"
    CONFIDENCE_F3_UNAVAILABLE = "CONFIDENCE_F3_UNAVAILABLE"
    CONFIDENCE_F7_SUFFICIENT = "CONFIDENCE_F7_SUFFICIENT"
    CONFIDENCE_F7_INSUFFICIENT = "CONFIDENCE_F7_INSUFFICIENT"
    CONFIDENCE_F7_UNAVAILABLE = "CONFIDENCE_F7_UNAVAILABLE"
    CONFIDENCE_F7_MAX_STEPS = "CONFIDENCE_F7_MAX_STEPS"
    CONFIDENCE_F7_FAILED = "CONFIDENCE_F7_FAILED"
    CONFIDENCE_F7_NOT_RUN = "CONFIDENCE_F7_NOT_RUN"
    CAPABILITY_ENABLED = "CAPABILITY_ENABLED"
    CAPABILITY_DEGRADED = "CAPABILITY_DEGRADED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    CONFIDENCE_CAP_APPLIED = "CONFIDENCE_CAP_APPLIED"
    COST_MATRIX_SELECTED = "COST_MATRIX_SELECTED"
    OVERRIDE_IMPACT_3 = "OVERRIDE_IMPACT_3"
    OVERRIDE_CONFIDENCE_0 = "OVERRIDE_CONFIDENCE_0"
    OVERRIDE_F3_UNAVAILABLE = "OVERRIDE_F3_UNAVAILABLE"
    OVERRIDE_F7_NON_SUFFICIENT = "OVERRIDE_F7_NON_SUFFICIENT"
    OVERRIDE_CAPABILITY_UNAVAILABLE = "OVERRIDE_CAPABILITY_UNAVAILABLE"
    FINAL_HIGH_ATTENTION = "FINAL_HIGH_ATTENTION"
    FINAL_MANUAL_ATTENTION = "FINAL_MANUAL_ATTENTION"
    FINAL_CLEARED = "FINAL_CLEARED"


REASON_CODE_ORDER: tuple[GradingReasonCode, ...] = tuple(GradingReasonCode)
_REASON_RANK = {reason: rank for rank, reason in enumerate(REASON_CODE_ORDER)}


class RuleImpactMap(StrictGradingModel):
    limit: StrictLevel
    invoice_type: StrictLevel
    timeliness: StrictLevel
    invoice_title: StrictLevel
    invoice_duplicate: StrictLevel

    def for_kind(self, kind: RuleKind) -> SeverityLevel:
        return SeverityLevel(getattr(self, kind.value))


class DetectorImpactMap(StrictGradingModel):
    split_invoice: StrictLevel
    sequential_invoice: StrictLevel
    frequency_anomaly: StrictLevel
    spatiotemporal_tier0: StrictLevel

    def for_kind(self, kind: DetectorKind) -> SeverityLevel:
        return SeverityLevel(getattr(self, kind.value))


class InvestigationConfidenceMap(StrictGradingModel):
    sufficient: StrictLevel
    insufficient: StrictLevel
    unavailable: StrictLevel
    max_steps: StrictLevel
    failed: StrictLevel
    not_run: StrictLevel

    @model_validator(mode="after")
    def not_run_is_zero(self) -> InvestigationConfidenceMap:
        if self.not_run is not SeverityLevel.LEVEL_0:
            raise ValueError("not_run confidence must be 0")
        return self

    def for_outcome(self, outcome: InvestigationGradingOutcome) -> SeverityLevel:
        return SeverityLevel(getattr(self, outcome.value))


class CapabilityConfidenceCap(StrictGradingModel):
    enabled: StrictLevel
    degraded: StrictLevel
    unavailable: StrictLevel

    @model_validator(mode="after")
    def caps_are_safe(self) -> CapabilityConfidenceCap:
        maximums = {
            CapabilityStatus.ENABLED: SeverityLevel.LEVEL_3,
            CapabilityStatus.DEGRADED: SeverityLevel.LEVEL_2,
            CapabilityStatus.UNAVAILABLE: SeverityLevel.LEVEL_0,
        }
        for status, maximum in maximums.items():
            if self.for_status(status) > maximum:
                raise ValueError(f"{status.value} confidence cap exceeds {maximum.value}")
        return self

    def for_status(self, status: CapabilityStatus) -> SeverityLevel:
        return SeverityLevel(getattr(self, status.value))


type LevelVector = tuple[CostUnit, CostUnit, CostUnit, CostUnit]
type ProbabilityVector = tuple[BasisPoints, BasisPoints, BasisPoints, BasisPoints]
type MatrixRow = tuple[Disposition, Disposition, Disposition, Disposition]
type DispositionMatrix = tuple[MatrixRow, MatrixRow, MatrixRow, MatrixRow]


class GradingConfigV1(StrictGradingModel):
    schema_version: Literal[1] = 1
    algorithm_version: Literal["cost-matrix-v1"] = "cost-matrix-v1"
    impact_by_rule_kind: RuleImpactMap
    impact_by_detector: DetectorImpactMap
    confidence_by_investigation_outcome: InvestigationConfidenceMap
    capability_confidence_cap: CapabilityConfidenceCap
    impact_cost_units: LevelVector
    confidence_issue_probability_bps: ProbabilityVector
    false_negative_multiplier_bps: PositiveCostUnit
    false_positive_cost_units: PositiveCostUnit
    manual_review_cost_units: PositiveCostUnit
    disposition_matrix: DispositionMatrix

    @model_validator(mode="after")
    def vectors_and_matrix_are_valid(self) -> GradingConfigV1:
        if self.impact_cost_units[0] != 0 or any(
            left >= right for left, right in pairwise(self.impact_cost_units)
        ):
            raise ValueError("impact cost units must start at 0 and strictly increase")
        probabilities = self.confidence_issue_probability_bps
        if probabilities[0] != 0 or any(left >= right for left, right in pairwise(probabilities)):
            raise ValueError("confidence probabilities must start at 0 and strictly increase")

        from app.core.grading.matrix import generate_disposition_matrix

        expected = generate_disposition_matrix(self)
        if self.disposition_matrix != expected:
            raise ValueError("disposition matrix does not match mechanically generated matrix")
        return self


class ParticipantRow(StrictGradingModel):
    row_no: Annotated[int, BeforeValidator(_strict_integer), Field(ge=1)]
    source_row_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class InvestigationRunSource(StrictGradingModel):
    kind: Literal["run"] = "run"
    outcome: Literal[
        InvestigationGradingOutcome.SUFFICIENT,
        InvestigationGradingOutcome.INSUFFICIENT,
        InvestigationGradingOutcome.UNAVAILABLE,
        InvestigationGradingOutcome.MAX_STEPS,
        InvestigationGradingOutcome.FAILED,
    ]
    evidence_sufficient: bool | None

    @field_validator("evidence_sufficient", mode="before")
    @classmethod
    def evidence_boolean_is_strict(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("evidence_sufficient must be a strict boolean or null")
        return value

    @model_validator(mode="after")
    def evidence_matches_outcome(self) -> InvestigationRunSource:
        expected: bool | None
        if self.outcome is InvestigationGradingOutcome.SUFFICIENT:
            expected = True
        elif self.outcome is InvestigationGradingOutcome.INSUFFICIENT:
            expected = False
        else:
            expected = None
        if self.evidence_sufficient is not expected:
            raise ValueError("evidence_sufficient conflicts with investigation outcome")
        return self


class InvestigationNotRunSource(StrictGradingModel):
    kind: Literal["not_run"] = "not_run"


type InvestigationSource = Annotated[
    InvestigationRunSource | InvestigationNotRunSource, Field(discriminator="kind")
]
INVESTIGATION_SOURCE_ADAPTER: TypeAdapter[InvestigationSource] = TypeAdapter(InvestigationSource)


class DeterministicGradingSource(StrictGradingModel):
    kind: Literal[SourceKind.DETERMINISTIC] = SourceKind.DETERMINISTIC
    finding_id: uuid.UUID
    row: ParticipantRow
    rule_kind: RuleKind
    outcome: Literal[RuleOutcome.FLAGGED, RuleOutcome.UNAVAILABLE]
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def source_id(self) -> uuid.UUID:
        return self.finding_id

    @property
    def first_row_no(self) -> int:
        return self.row.row_no


class CorrelationGradingSource(StrictGradingModel):
    kind: Literal[SourceKind.CORRELATION] = SourceKind.CORRELATION
    correlation_finding_id: uuid.UUID
    detector: DetectorKind
    finding_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    participating_rows: tuple[ParticipantRow, ...] = Field(min_length=2, max_length=5000)
    capability_status: CapabilityStatus
    investigation: InvestigationSource

    @field_validator("participating_rows")
    @classmethod
    def rows_are_unique_and_sorted(
        cls, values: tuple[ParticipantRow, ...]
    ) -> tuple[ParticipantRow, ...]:
        row_numbers = tuple(row.row_no for row in values)
        if len(row_numbers) != len(set(row_numbers)) or row_numbers != tuple(sorted(row_numbers)):
            raise ValueError("participating rows must be unique and sorted by row_no")
        return values

    @property
    def source_id(self) -> uuid.UUID:
        return self.correlation_finding_id

    @property
    def first_row_no(self) -> int:
        return self.participating_rows[0].row_no


type GradingSource = Annotated[
    DeterministicGradingSource | CorrelationGradingSource, Field(discriminator="kind")
]
GRADING_SOURCE_ADAPTER: TypeAdapter[GradingSource] = TypeAdapter(GradingSource)


class GradingInput(StrictGradingModel):
    config: GradingConfigV1
    f3_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    f6_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    f7_manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_sources: tuple[DeterministicGradingSource, ...] = Field(max_length=5000)
    correlation_sources: tuple[CorrelationGradingSource, ...] = Field(max_length=20000)

    @field_validator("deterministic_sources")
    @classmethod
    def deterministic_sources_are_stable(
        cls, values: tuple[DeterministicGradingSource, ...]
    ) -> tuple[DeterministicGradingSource, ...]:
        return tuple(sorted(values, key=lambda item: str(item.source_id)))

    @field_validator("correlation_sources")
    @classmethod
    def correlation_sources_are_stable(
        cls, values: tuple[CorrelationGradingSource, ...]
    ) -> tuple[CorrelationGradingSource, ...]:
        return tuple(sorted(values, key=lambda item: str(item.source_id)))

    @model_validator(mode="after")
    def sources_are_unique_and_stable(self) -> GradingInput:
        deterministic_ids = tuple(item.source_id for item in self.deterministic_sources)
        correlation_ids = tuple(item.source_id for item in self.correlation_sources)
        if len(deterministic_ids) != len(set(deterministic_ids)) or len(correlation_ids) != len(
            set(correlation_ids)
        ):
            raise ValueError("grading source identities must be unique")
        return self


class CostLosses(StrictGradingModel):
    clear_loss: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    flag_loss: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    review_loss: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]


class MatrixCellResult(StrictGradingModel):
    impact: StrictLevel
    confidence: StrictLevel
    losses: CostLosses
    selected_disposition: Disposition
    disposition: Disposition
    override_reason_codes: tuple[GradingReasonCode, ...] = Field(max_length=2)


class EvidenceSnapshotBase(StrictGradingModel):
    mapped_impact: StrictLevel
    confidence_before_cap: StrictLevel
    confidence_after_cap: StrictLevel
    losses: CostLosses
    matrix_disposition: Disposition


class DeterministicEvidenceSnapshot(EvidenceSnapshotBase):
    source_kind: Literal[SourceKind.DETERMINISTIC] = SourceKind.DETERMINISTIC
    rule_kind: RuleKind
    detector: None = None
    source_outcome: Literal[RuleOutcome.FLAGGED, RuleOutcome.UNAVAILABLE]
    capability_status: None = None


class CorrelationEvidenceSnapshot(EvidenceSnapshotBase):
    source_kind: Literal[SourceKind.CORRELATION] = SourceKind.CORRELATION
    rule_kind: None = None
    detector: DetectorKind
    source_outcome: InvestigationGradingOutcome
    capability_status: CapabilityStatus


type GradingEvidenceSnapshot = Annotated[
    DeterministicEvidenceSnapshot | CorrelationEvidenceSnapshot,
    Field(discriminator="source_kind"),
]


class GradingItemResult(StrictGradingModel):
    source_kind: SourceKind
    source_id: uuid.UUID
    first_row_no: Annotated[int, BeforeValidator(_strict_integer), Field(ge=1)]
    severity_impact: StrictLevel
    severity_confidence: StrictLevel
    matrix_disposition: Disposition
    disposition: Disposition
    reason_codes: tuple[GradingReasonCode, ...] = Field(min_length=1, max_length=MAX_REASON_CODES)
    evidence_snapshot: GradingEvidenceSnapshot
    item_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("reason_codes")
    @classmethod
    def reasons_are_unique_and_ordered(
        cls, values: tuple[GradingReasonCode, ...]
    ) -> tuple[GradingReasonCode, ...]:
        if len(values) != len(set(values)):
            raise ValueError("reason codes must be unique")
        if values != tuple(sorted(values, key=_REASON_RANK.__getitem__)):
            raise ValueError("reason codes must use the fixed specification order")
        return values


class GradingCoreResult(StrictGradingModel):
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[GradingItemResult, ...]
    deterministic_item_count: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    correlation_item_count: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    high_attention_count: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    manual_attention_count: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]
    cleared_count: Annotated[int, BeforeValidator(_strict_integer), Field(ge=0)]

    @model_validator(mode="after")
    def counts_match_items(self) -> GradingCoreResult:
        if self.deterministic_item_count != sum(
            item.source_kind is SourceKind.DETERMINISTIC for item in self.items
        ) or self.correlation_item_count != sum(
            item.source_kind is SourceKind.CORRELATION for item in self.items
        ):
            raise ValueError("source counts do not match grading items")
        disposition_counts = {
            disposition: sum(item.disposition is disposition for item in self.items)
            for disposition in Disposition
        }
        if (
            self.high_attention_count != disposition_counts[Disposition.HIGH_ATTENTION]
            or self.manual_attention_count != disposition_counts[Disposition.MANUAL_ATTENTION]
            or self.cleared_count != disposition_counts[Disposition.CLEARED]
        ):
            raise ValueError("disposition counts do not match grading items")
        return self
