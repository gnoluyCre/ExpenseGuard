"""Strict, persistence-agnostic models for deterministic F6 detection."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from app.core.parsing.models import UNIFIED_FIELDS, NormalizedExpenseRecord, UnifiedField

MAX_CANONICAL_BYTES = 256 * 1024
ALGORITHM_BUNDLE_VERSION = "correlation-v1"


class StrictDetectionModel(BaseModel):
    """Every CP-F6.2 boundary is immutable and rejects unknown input."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DetectorKind(StrEnum):
    SPLIT_INVOICE = "split_invoice"
    SEQUENTIAL_INVOICE = "sequential_invoice"
    FREQUENCY_ANOMALY = "frequency_anomaly"
    SPATIOTEMPORAL_TIER0 = "spatiotemporal_tier0"


DETECTOR_ORDER: tuple[DetectorKind, ...] = tuple(DetectorKind)
DETECTOR_VERSIONS: dict[DetectorKind, str] = {
    DetectorKind.SPLIT_INVOICE: "split-window-v1",
    DetectorKind.SEQUENTIAL_INVOICE: "invoice-sequence-v1",
    DetectorKind.FREQUENCY_ANOMALY: "frequency-mad-v1",
    DetectorKind.SPATIOTEMPORAL_TIER0: "spatiotemporal-pair-v1",
}


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    INFERRED = "inferred"
    MISSING = "missing"


class CapabilityStatus(StrEnum):
    ENABLED = "enabled"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityReason(StrEnum):
    CONFIG_DISABLED = "CONFIG_DISABLED"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    INSUFFICIENT_ELIGIBLE_ROWS = "INSUFFICIENT_ELIGIBLE_ROWS"
    INSUFFICIENT_POPULATION = "INSUFFICIENT_POPULATION"
    ZONE_MAPPING_BELOW_MINIMUM = "ZONE_MAPPING_BELOW_MINIMUM"
    INFERRED_FIELD_USED = "INFERRED_FIELD_USED"
    CURRENCY_CONFLICT = "CURRENCY_CONFLICT"
    THRESHOLD_CURRENCY_UNCONFIGURED = "THRESHOLD_CURRENCY_UNCONFIGURED"
    PARTIAL_PERIOD_SKIPPED = "PARTIAL_PERIOD_SKIPPED"
    SERIAL_UNPARSEABLE = "SERIAL_UNPARSEABLE"
    LOCATION_UNMAPPED = "LOCATION_UNMAPPED"
    READY = "READY"


CAPABILITY_REASON_ORDER: tuple[CapabilityReason, ...] = tuple(CapabilityReason)


class ExclusionReason(StrEnum):
    AMOUNT_BELOW_INDIVIDUAL_FLOOR = "AMOUNT_BELOW_INDIVIDUAL_FLOOR"
    AMOUNT_NOT_BELOW_THRESHOLD = "AMOUNT_NOT_BELOW_THRESHOLD"
    AMOUNT_NOT_POSITIVE = "AMOUNT_NOT_POSITIVE"
    CURRENCY_CONFLICT = "CURRENCY_CONFLICT"
    LOCATION_UNMAPPED = "LOCATION_UNMAPPED"
    PARSE_ERROR = "PARSE_ERROR"
    REQUIRED_VALUE_MISSING = "REQUIRED_VALUE_MISSING"
    SERIAL_UNPARSEABLE = "SERIAL_UNPARSEABLE"
    THRESHOLD_CURRENCY_UNCONFIGURED = "THRESHOLD_CURRENCY_UNCONFIGURED"


def normalize_positive_decimal(value: object, *, maximum: Decimal | None = None) -> str:
    """Validate an ASCII, finite, positive, non-exponent decimal string."""
    if not isinstance(value, str) or not value:
        raise ValueError("值必须是正的非指数十进制字符串")
    if value.count(".") > 1 or any(character not in "0123456789." for character in value):
        raise ValueError("值必须是正的非指数十进制字符串")
    integer, separator, fraction = value.partition(".")
    if not integer or (separator and not fraction):
        raise ValueError("值必须是正的非指数十进制字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("值必须是正的非指数十进制字符串") from exc
    if not parsed.is_finite() or parsed <= 0 or (maximum is not None and parsed > maximum):
        raise ValueError("十进制值超出允许范围")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _validate_exact_text(value: str, *, maximum: int = 512) -> str:
    if not value or value != value.strip() or len(value) > maximum:
        raise ValueError("exact 配置值必须非空、无首尾空白且长度合法")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("exact 配置值不得包含控制字符")
    return value


def _validate_currency(value: str) -> str:
    if len(value) != 3 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for character in value):
        raise ValueError("币种必须是三位 ASCII 大写代码")
    return value


class DetectorDefinitionBase(StrictDetectionModel):
    enabled: bool
    min_eligible_rows: int = Field(ge=2, le=5000)
    min_eligible_rate_bps: int = Field(ge=1, le=10_000)


class SplitInvoiceDefinition(DetectorDefinitionBase):
    type: Literal[DetectorKind.SPLIT_INVOICE]
    approval_thresholds: dict[str, str] = Field(min_length=1, max_length=500)
    currency_mode: Literal["field", "fixed"]
    fixed_currency: str | None
    aggregate_operator: Literal["gt", "gte"]
    individual_floor_bps: int = Field(ge=1, le=9999)
    date_window_days: int = Field(ge=0, le=31)
    min_rows: int = Field(ge=2, le=50)
    merchant_aliases: dict[str, str] = Field(max_length=5000)

    @field_validator("approval_thresholds", mode="before")
    @classmethod
    def validate_thresholds(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("approval_thresholds 必须是 object")
        normalized: dict[str, str] = {}
        for raw_currency, raw_threshold in value.items():
            if not isinstance(raw_currency, str):
                raise ValueError("币种键必须是字符串")
            currency = _validate_currency(raw_currency)
            normalized[currency] = normalize_positive_decimal(raw_threshold)
        return dict(sorted(normalized.items()))

    @field_validator("fixed_currency")
    @classmethod
    def validate_fixed_currency(cls, value: str | None) -> str | None:
        return None if value is None else _validate_currency(value)

    @field_validator("merchant_aliases", mode="before")
    @classmethod
    def validate_merchant_aliases(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("merchant_aliases 必须是 object")
        normalized: dict[str, str] = {}
        for raw_alias, raw_key in value.items():
            if not isinstance(raw_alias, str) or not isinstance(raw_key, str):
                raise ValueError("merchant alias 的键和值必须是字符串")
            alias = _validate_exact_text(raw_alias)
            normalized[alias] = _validate_exact_text(raw_key)
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def fixed_currency_matches_mode(self) -> SplitInvoiceDefinition:
        if (self.currency_mode == "fixed") != (self.fixed_currency is not None):
            raise ValueError("fixed_currency 必须且只能用于 fixed currency mode")
        return self


class SequentialPartitionField(StrEnum):
    EMPLOYEE = "employee"
    MERCHANT = "merchant"
    INVOICE_TYPE = "invoice_type"


class SequentialInvoiceDefinition(DetectorDefinitionBase):
    type: Literal[DetectorKind.SEQUENTIAL_INVOICE]
    min_sequence_length: int = Field(ge=2, le=50)
    numeric_suffix_min_digits: int = Field(ge=1, le=32)
    numeric_suffix_max_digits: int = Field(ge=1, le=32)
    partition_fields: tuple[SequentialPartitionField, ...]

    @model_validator(mode="after")
    def validate_suffix_and_partition(self) -> SequentialInvoiceDefinition:
        if self.numeric_suffix_max_digits < self.numeric_suffix_min_digits:
            raise ValueError("numeric suffix 最大位数不得小于最小位数")
        if len(self.partition_fields) != len(set(self.partition_fields)):
            raise ValueError("partition_fields 不得重复")
        return self


class FrequencyAnomalyDefinition(DetectorDefinitionBase):
    type: Literal[DetectorKind.FREQUENCY_ANOMALY]
    period: Literal["calendar_week_monday", "calendar_month"]
    min_population: int = Field(ge=3, le=5000)
    absolute_min_count: int = Field(ge=2, le=5000)
    mad_multiplier: str
    mad_floor: str

    @field_validator("mad_multiplier", mode="before")
    @classmethod
    def validate_multiplier(cls, value: object) -> str:
        return normalize_positive_decimal(value, maximum=Decimal("100"))

    @field_validator("mad_floor", mode="before")
    @classmethod
    def validate_floor(cls, value: object) -> str:
        return normalize_positive_decimal(value, maximum=Decimal("5000"))


class SpatiotemporalTier0Definition(DetectorDefinitionBase):
    type: Literal[DetectorKind.SPATIOTEMPORAL_TIER0]
    location_aliases: dict[str, str] = Field(max_length=5000)
    incompatible_zone_pairs: tuple[tuple[str, str], ...] = Field(max_length=10_000)
    min_zone_mapping_rate_bps: int = Field(ge=1, le=10_000)

    @field_validator("location_aliases", mode="before")
    @classmethod
    def validate_location_aliases(cls, value: object) -> object:
        if not isinstance(value, dict):
            raise ValueError("location_aliases 必须是 object")
        normalized: dict[str, str] = {}
        for raw_alias, raw_zone in value.items():
            if not isinstance(raw_alias, str) or not isinstance(raw_zone, str):
                raise ValueError("location alias 的键和值必须是字符串")
            alias = _validate_exact_text(raw_alias)
            normalized[alias] = _validate_exact_text(raw_zone, maximum=64)
        return dict(sorted(normalized.items()))

    @field_validator("incompatible_zone_pairs", mode="before")
    @classmethod
    def canonicalize_pairs(cls, value: object) -> object:
        if not isinstance(value, list | tuple):
            raise ValueError("incompatible_zone_pairs 必须是数组")
        pairs: list[tuple[str, str]] = []
        for raw_pair in value:
            if not isinstance(raw_pair, list | tuple) or len(raw_pair) != 2:
                raise ValueError("zone pair 必须恰有两项")
            left_raw, right_raw = raw_pair
            if not isinstance(left_raw, str) or not isinstance(right_raw, str):
                raise ValueError("zone id 必须是字符串")
            left = _validate_exact_text(left_raw, maximum=64)
            right = _validate_exact_text(right_raw, maximum=64)
            if left == right:
                raise ValueError("zone pair 不得自冲突")
            pairs.append((min(left, right), max(left, right)))
        if len(pairs) != len(set(pairs)):
            raise ValueError("incompatible zone pair 不得重复")
        return tuple(sorted(pairs))

    @model_validator(mode="after")
    def pairs_reference_known_zones(self) -> SpatiotemporalTier0Definition:
        zones = set(self.location_aliases.values())
        if any(
            left not in zones or right not in zones for left, right in self.incompatible_zone_pairs
        ):
            raise ValueError("每个 incompatible zone 必须至少被一个 alias 引用")
        return self


type DetectorDefinition = Annotated[
    SplitInvoiceDefinition
    | SequentialInvoiceDefinition
    | FrequencyAnomalyDefinition
    | SpatiotemporalTier0Definition,
    Field(discriminator="type"),
]
DETECTOR_DEFINITION_ADAPTER: TypeAdapter[DetectorDefinition] = TypeAdapter(DetectorDefinition)


class DetectionProfileDefinition(StrictDetectionModel):
    schema_version: Literal[1] = 1
    algorithm_bundle_version: Literal["correlation-v1"] = "correlation-v1"
    detectors: tuple[DetectorDefinition, ...] = Field(min_length=4, max_length=4)

    @field_validator("detectors")
    @classmethod
    def detectors_are_complete_and_ordered(
        cls, values: tuple[DetectorDefinition, ...]
    ) -> tuple[DetectorDefinition, ...]:
        by_kind = {item.type: item for item in values}
        if len(by_kind) != len(values) or set(by_kind) != set(DETECTOR_ORDER):
            raise ValueError("detectors 必须恰好包含四种类型各一次")
        return tuple(by_kind[kind] for kind in DETECTOR_ORDER)


class ParsedSourceRow(StrictDetectionModel):
    kind: Literal["parsed"] = "parsed"
    row_no: int = Field(ge=1)
    normalized: NormalizedExpenseRecord


class ParseErrorSourceRow(StrictDetectionModel):
    kind: Literal["parse_error"] = "parse_error"
    row_no: int = Field(ge=1)
    error_code: str = Field(min_length=1, max_length=64)

    @field_validator("error_code")
    @classmethod
    def error_code_is_safe(cls, value: str) -> str:
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for character in value):
            raise ValueError("parse error code 必须是大写下划线标识")
        return value


type SourceRow = Annotated[ParsedSourceRow | ParseErrorSourceRow, Field(discriminator="kind")]


class FieldAvailabilitySnapshot(StrictDetectionModel):
    field_name: UnifiedField
    status: AvailabilityStatus


class DetectionBatch(StrictDetectionModel):
    rows: tuple[SourceRow, ...] = Field(min_length=1, max_length=5000)
    field_availability: tuple[FieldAvailabilitySnapshot, ...] = Field(min_length=12, max_length=12)

    @field_validator("rows")
    @classmethod
    def canonicalize_rows(cls, rows: tuple[SourceRow, ...]) -> tuple[SourceRow, ...]:
        row_nos = [row.row_no for row in rows]
        if len(row_nos) != len(set(row_nos)):
            raise ValueError("row_no 不得重复")
        return tuple(sorted(rows, key=lambda item: item.row_no))

    @field_validator("field_availability")
    @classmethod
    def canonicalize_availability(
        cls, values: tuple[FieldAvailabilitySnapshot, ...]
    ) -> tuple[FieldAvailabilitySnapshot, ...]:
        availability = {item.field_name: item for item in values}
        if len(availability) != len(values) or set(availability) != set(UNIFIED_FIELDS):
            raise ValueError("field availability 必须恰好覆盖 12 个统一字段")
        return tuple(availability[field] for field in UNIFIED_FIELDS)


class DependencyAvailability(StrictDetectionModel):
    field_name: UnifiedField
    status: AvailabilityStatus


class ExclusionCount(StrictDetectionModel):
    reason_code: ExclusionReason
    count: int = Field(gt=0)


class RuntimeFactsBase(StrictDetectionModel):
    detector: DetectorKind


class SplitRuntimeFacts(RuntimeFactsBase):
    detector: Literal[DetectorKind.SPLIT_INVOICE]
    threshold_currency_count: int = Field(ge=0)
    unconfigured_currency_row_count: int = Field(ge=0)
    currency_conflict_row_count: int = Field(ge=0)


class SequentialRuntimeFacts(RuntimeFactsBase):
    detector: Literal[DetectorKind.SEQUENTIAL_INVOICE]
    serial_parse_count: int = Field(ge=0)
    serial_unparseable_count: int = Field(ge=0)
    duplicate_serial_row_count: int = Field(ge=0)


class FrequencyRuntimeFacts(RuntimeFactsBase):
    detector: Literal[DetectorKind.FREQUENCY_ANOMALY]
    period_count: int = Field(ge=0)
    eligible_period_count: int = Field(ge=0)
    skipped_period_count: int = Field(ge=0)


class SpatiotemporalRuntimeFacts(RuntimeFactsBase):
    detector: Literal[DetectorKind.SPATIOTEMPORAL_TIER0]
    location_value_count: int = Field(ge=0)
    zone_mapped_row_count: int = Field(ge=0)
    zone_unmapped_row_count: int = Field(ge=0)
    zone_mapping_rate_bps: int = Field(ge=0, le=10_000)


type DetectorRuntimeFacts = Annotated[
    SplitRuntimeFacts | SequentialRuntimeFacts | FrequencyRuntimeFacts | SpatiotemporalRuntimeFacts,
    Field(discriminator="detector"),
]


class CapabilityDetails(StrictDetectionModel):
    source_row_count: int = Field(ge=0)
    parsed_row_count: int = Field(ge=0)
    eligible_row_count: int = Field(ge=0)
    excluded_row_count: int = Field(ge=0)
    eligible_rate_bps: int = Field(ge=0, le=10_000)
    dependencies: tuple[DependencyAvailability, ...]
    causes: tuple[CapabilityReason, ...]
    exclusion_counts: tuple[ExclusionCount, ...]
    runtime: DetectorRuntimeFacts

    @model_validator(mode="after")
    def counts_and_order_are_consistent(self) -> CapabilityDetails:
        if self.parsed_row_count > self.source_row_count:
            raise ValueError("parsed count 不得超过 source count")
        if self.eligible_row_count + self.excluded_row_count != self.source_row_count:
            raise ValueError("eligible/excluded 必须覆盖全部 source rows")
        if tuple(sorted(self.exclusion_counts, key=lambda item: item.reason_code.value)) != (
            self.exclusion_counts
        ):
            raise ValueError("exclusion_counts 必须按 reason code 排序")
        expected_causes = tuple(
            reason for reason in CAPABILITY_REASON_ORDER if reason in set(self.causes)
        )
        if expected_causes != self.causes:
            raise ValueError("causes 必须按固定优先级去重排序")
        return self


class CapabilityDraft(StrictDetectionModel):
    detector: DetectorKind
    detector_version: str
    status: CapabilityStatus
    reason_code: CapabilityReason
    reason_snapshot: str = Field(min_length=1, max_length=500)
    details: CapabilityDetails
    finding_count: int = Field(ge=0)

    @model_validator(mode="after")
    def identities_are_consistent(self) -> CapabilityDraft:
        if self.detector_version != DETECTOR_VERSIONS[self.detector]:
            raise ValueError("detector/version 不匹配")
        if self.details.runtime.detector is not self.detector:
            raise ValueError("runtime facts detector 不匹配")
        if not self.details.causes or self.details.causes[0] is not self.reason_code:
            raise ValueError("primary reason 必须是排序后的第一项")
        expected_status = capability_status_for_reason(self.reason_code)
        if self.status is not expected_status:
            raise ValueError("capability status 与 primary reason 不匹配")
        return self


def capability_status_for_reason(reason: CapabilityReason) -> CapabilityStatus:
    if reason in {
        CapabilityReason.CONFIG_DISABLED,
        CapabilityReason.REQUIRED_FIELD_MISSING,
        CapabilityReason.INSUFFICIENT_ELIGIBLE_ROWS,
        CapabilityReason.INSUFFICIENT_POPULATION,
        CapabilityReason.ZONE_MAPPING_BELOW_MINIMUM,
    }:
        return CapabilityStatus.UNAVAILABLE
    if reason is CapabilityReason.READY:
        return CapabilityStatus.ENABLED
    return CapabilityStatus.DEGRADED


class RationalValue(StrictDetectionModel):
    numerator: int
    denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def must_be_reduced(self) -> RationalValue:
        reduced = Fraction(self.numerator, self.denominator)
        if (reduced.numerator, reduced.denominator) != (self.numerator, self.denominator):
            raise ValueError("rational value 必须是最简分数")
        return self

    @classmethod
    def from_fraction(cls, value: Fraction) -> RationalValue:
        return cls(numerator=value.numerator, denominator=value.denominator)


class SplitInvoiceFacts(StrictDetectionModel):
    currency: str
    approval_threshold: str
    individual_floor_bps: int
    aggregate_operator: Literal["gt", "gte"]
    date_start: str
    date_end: str
    row_count: int = Field(ge=2)
    amounts: tuple[str, ...] = Field(min_length=2)
    total: str


class SequentialInvoiceFacts(StrictDetectionModel):
    prefix_fingerprint: str
    suffix_width: int = Field(ge=1, le=32)
    start_serial: str
    end_serial: str
    sequence_length: int = Field(ge=2)
    ordered_row_nos: tuple[int, ...] = Field(min_length=2)


class FrequencyAnomalyFacts(StrictDetectionModel):
    period_key: str
    population: int = Field(ge=3)
    count: int = Field(ge=2)
    median: RationalValue
    mad: RationalValue
    effective_denominator: RationalValue
    multiplier: RationalValue
    positive_delta: RationalValue
    observed_ratio: RationalValue


class ZoneRowCount(StrictDetectionModel):
    zone_id: str
    row_count: int = Field(gt=0)


class SpatiotemporalTier0Facts(StrictDetectionModel):
    expense_date: str
    incompatible_zone_pairs: tuple[tuple[str, str], ...] = Field(min_length=1)
    zone_row_counts: tuple[ZoneRowCount, ...] = Field(min_length=2)


class EvidenceBase(StrictDetectionModel):
    schema_version: Literal[1] = 1
    detector: DetectorKind
    detector_version: str
    profile_fingerprint: str
    group_key_fingerprint: str
    reason_code: Literal["STATISTICAL_CANDIDATE"] = "STATISTICAL_CANDIDATE"

    @model_validator(mode="after")
    def validate_fingerprints_and_version(self) -> EvidenceBase:
        for value in (self.profile_fingerprint, self.group_key_fingerprint):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("fingerprint 必须是 64 位小写十六进制")
        if self.detector_version != DETECTOR_VERSIONS[self.detector]:
            raise ValueError("evidence detector/version 不匹配")
        return self


class SplitInvoiceEvidence(EvidenceBase):
    detector: Literal[DetectorKind.SPLIT_INVOICE]
    facts: SplitInvoiceFacts


class SequentialInvoiceEvidence(EvidenceBase):
    detector: Literal[DetectorKind.SEQUENTIAL_INVOICE]
    facts: SequentialInvoiceFacts


class FrequencyAnomalyEvidence(EvidenceBase):
    detector: Literal[DetectorKind.FREQUENCY_ANOMALY]
    facts: FrequencyAnomalyFacts


class SpatiotemporalTier0Evidence(EvidenceBase):
    detector: Literal[DetectorKind.SPATIOTEMPORAL_TIER0]
    facts: SpatiotemporalTier0Facts


type CorrelationEvidence = Annotated[
    SplitInvoiceEvidence
    | SequentialInvoiceEvidence
    | FrequencyAnomalyEvidence
    | SpatiotemporalTier0Evidence,
    Field(discriminator="detector"),
]
CORRELATION_EVIDENCE_ADAPTER: TypeAdapter[CorrelationEvidence] = TypeAdapter(CorrelationEvidence)


class FindingDraft(StrictDetectionModel):
    detector: DetectorKind
    detector_version: str
    participating_row_nos: tuple[int, ...] = Field(min_length=2)
    evidence: CorrelationEvidence
    finding_key: str
    reasoning_snapshot: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_identity(self) -> FindingDraft:
        if self.detector_version != DETECTOR_VERSIONS[self.detector]:
            raise ValueError("finding detector/version 不匹配")
        if self.evidence.detector is not self.detector or (
            self.evidence.detector_version != self.detector_version
        ):
            raise ValueError("finding/evidence identity 不匹配")
        if (
            any(row_no < 1 for row_no in self.participating_row_nos)
            or len(set(self.participating_row_nos)) != len(self.participating_row_nos)
            or tuple(sorted(self.participating_row_nos)) != self.participating_row_nos
        ):
            raise ValueError("participating rows 必须是递增、唯一、正整数")
        if len(self.finding_key) != 64 or any(
            character not in "0123456789abcdef" for character in self.finding_key
        ):
            raise ValueError("finding key 必须是 64 位小写十六进制")
        return self


class DetectorOutput(StrictDetectionModel):
    declaration: CapabilityDraft
    findings: tuple[FindingDraft, ...]

    @model_validator(mode="after")
    def output_is_consistent(self) -> DetectorOutput:
        if self.declaration.finding_count != len(self.findings):
            raise ValueError("capability finding count 与 findings 不一致")
        if any(item.detector is not self.declaration.detector for item in self.findings):
            raise ValueError("detector output 混入其他 detector finding")
        return self


class DetectionCoreResult(StrictDetectionModel):
    profile_fingerprint: str
    outputs: tuple[DetectorOutput, ...] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def outputs_are_complete_and_ordered(self) -> DetectionCoreResult:
        detectors = tuple(output.declaration.detector for output in self.outputs)
        if detectors != DETECTOR_ORDER:
            raise ValueError("outputs 必须按固定顺序恰好覆盖四个 detector")
        if any(
            finding.evidence.profile_fingerprint != self.profile_fingerprint
            for output in self.outputs
            for finding in output.findings
        ):
            raise ValueError("result profile fingerprint 不一致")
        return self
