"""Pure deterministic capability evaluation and four F6 detectors."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from app.core.detection.canonical import (
    finding_identity_payload,
    finding_key,
    group_key_fingerprint,
    profile_fingerprint,
)
from app.core.detection.models import (
    CAPABILITY_REASON_ORDER,
    DETECTOR_ORDER,
    DETECTOR_VERSIONS,
    AvailabilityStatus,
    CapabilityDetails,
    CapabilityDraft,
    CapabilityReason,
    CapabilityStatus,
    CorrelationEvidence,
    DependencyAvailability,
    DetectionBatch,
    DetectionCoreResult,
    DetectionProfileDefinition,
    DetectorDefinition,
    DetectorKind,
    DetectorOutput,
    DetectorRuntimeFacts,
    ExclusionCount,
    ExclusionReason,
    FindingDraft,
    FrequencyAnomalyDefinition,
    FrequencyAnomalyEvidence,
    FrequencyAnomalyFacts,
    FrequencyRuntimeFacts,
    ParsedSourceRow,
    ParseErrorSourceRow,
    RationalValue,
    SequentialInvoiceDefinition,
    SequentialInvoiceEvidence,
    SequentialInvoiceFacts,
    SequentialRuntimeFacts,
    SpatiotemporalRuntimeFacts,
    SpatiotemporalTier0Definition,
    SpatiotemporalTier0Evidence,
    SpatiotemporalTier0Facts,
    SplitInvoiceDefinition,
    SplitInvoiceEvidence,
    SplitInvoiceFacts,
    SplitRuntimeFacts,
    ZoneRowCount,
    capability_status_for_reason,
)
from app.core.parsing.models import NormalizedExpenseRecord, UnifiedField

_REASON_SNAPSHOTS: dict[CapabilityReason, str] = {
    CapabilityReason.CONFIG_DISABLED: "检测器已被配置关闭。",
    CapabilityReason.REQUIRED_FIELD_MISSING: "必需字段不可用，无法执行该检测器。",
    CapabilityReason.INSUFFICIENT_ELIGIBLE_ROWS: "满足条件的行数或覆盖率不足。",
    CapabilityReason.THRESHOLD_CURRENCY_UNCONFIGURED: "部分或全部币种缺少审批阈值。",
    CapabilityReason.INSUFFICIENT_POPULATION: "没有日历周期达到最小总体规模。",
    CapabilityReason.ZONE_MAPPING_BELOW_MINIMUM: "地点分区映射覆盖率低于配置要求。",
    CapabilityReason.INFERRED_FIELD_USED: "检测使用了确定性推断字段。",
    CapabilityReason.CURRENCY_CONFLICT: "部分行币种与固定币种声明冲突。",
    CapabilityReason.PARTIAL_PERIOD_SKIPPED: "部分日历周期因总体不足被跳过。",
    CapabilityReason.SERIAL_UNPARSEABLE: "部分发票号无法按 ASCII 数字后缀解析。",
    CapabilityReason.LOCATION_UNMAPPED: "部分地点没有 exact 分区映射。",
    CapabilityReason.READY: "字段和运行时覆盖满足检测要求。",
}


@dataclass(frozen=True, slots=True)
class _Parsed:
    row_no: int
    record: NormalizedExpenseRecord
    amount: Decimal
    expense_date: date


@dataclass(frozen=True, slots=True)
class _SplitRow:
    parsed: _Parsed
    employee: str
    merchant: str
    currency: str
    threshold: Decimal


@dataclass(frozen=True, slots=True)
class _SequentialRow:
    parsed: _Parsed
    partition: tuple[str, ...]
    prefix: str
    width: int
    serial: int


@dataclass(frozen=True, slots=True)
class _FrequencyRow:
    parsed: _Parsed
    employee: str
    period_key: str


@dataclass(frozen=True, slots=True)
class _SpatiotemporalRow:
    parsed: _Parsed
    employee: str
    zone: str


def run_detection_core(
    profile: DetectionProfileDefinition,
    batch: DetectionBatch,
) -> DetectionCoreResult:
    """Run the complete CP-F6.2 core without side effects."""
    fingerprint = profile_fingerprint(profile)
    by_kind = {definition.type: definition for definition in profile.detectors}
    outputs = (
        _detect_split(_expect_split(by_kind[DetectorKind.SPLIT_INVOICE]), batch, fingerprint),
        _detect_sequential(
            _expect_sequential(by_kind[DetectorKind.SEQUENTIAL_INVOICE]), batch, fingerprint
        ),
        _detect_frequency(
            _expect_frequency(by_kind[DetectorKind.FREQUENCY_ANOMALY]), batch, fingerprint
        ),
        _detect_spatiotemporal(
            _expect_spatiotemporal(by_kind[DetectorKind.SPATIOTEMPORAL_TIER0]),
            batch,
            fingerprint,
        ),
    )
    return DetectionCoreResult(profile_fingerprint=fingerprint, outputs=outputs)


def stable_findings(findings: Iterable[FindingDraft]) -> tuple[FindingDraft, ...]:
    """Deduplicate by key, fail closed on collision, then apply the frozen ordering."""
    identities: dict[str, bytes] = {}
    retained: dict[str, FindingDraft] = {}
    for finding in findings:
        identity = finding_identity_payload(
            detector=finding.detector,
            detector_version=finding.detector_version,
            profile_fingerprint_value=finding.evidence.profile_fingerprint,
            participating_row_nos=finding.participating_row_nos,
            evidence=finding.evidence,
        )
        expected_key = finding_key(
            detector=finding.detector,
            detector_version=finding.detector_version,
            profile_fingerprint_value=finding.evidence.profile_fingerprint,
            participating_row_nos=finding.participating_row_nos,
            evidence=finding.evidence,
        )
        if finding.finding_key != expected_key:
            raise ValueError("finding key does not match canonical payload")
        previous = identities.get(finding.finding_key)
        if previous is not None and previous != identity:
            raise ValueError("finding key collision with different canonical payload")
        identities[finding.finding_key] = identity
        retained.setdefault(finding.finding_key, finding)
    detector_rank = {detector: rank for rank, detector in enumerate(DETECTOR_ORDER)}
    return tuple(
        sorted(
            retained.values(),
            key=lambda item: (
                detector_rank[item.detector],
                item.participating_row_nos[0],
                item.participating_row_nos,
                item.finding_key,
            ),
        )
    )


def _parsed_rows(batch: DetectionBatch) -> tuple[_Parsed, ...]:
    parsed: list[_Parsed] = []
    for source in batch.rows:
        if isinstance(source, ParsedSourceRow):
            parsed.append(
                _Parsed(
                    row_no=source.row_no,
                    record=source.normalized,
                    amount=Decimal(source.normalized.amount),
                    expense_date=date.fromisoformat(source.normalized.expense_date),
                )
            )
    return tuple(parsed)


def _base_exclusions(batch: DetectionBatch) -> Counter[ExclusionReason]:
    return Counter(
        {
            ExclusionReason.PARSE_ERROR: sum(
                isinstance(row, ParseErrorSourceRow) for row in batch.rows
            )
        }
    )


def _dependencies(
    batch: DetectionBatch, fields: Sequence[UnifiedField]
) -> tuple[DependencyAvailability, ...]:
    status = {item.field_name: item.status for item in batch.field_availability}
    return tuple(DependencyAvailability(field_name=field, status=status[field]) for field in fields)


def _initial_causes(
    definition: DetectorDefinition,
    dependencies: Sequence[DependencyAvailability],
) -> set[CapabilityReason]:
    causes: set[CapabilityReason] = set()
    if not definition.enabled:
        causes.add(CapabilityReason.CONFIG_DISABLED)
    if any(item.status is AvailabilityStatus.MISSING for item in dependencies):
        causes.add(CapabilityReason.REQUIRED_FIELD_MISSING)
    if any(item.status is AvailabilityStatus.INFERRED for item in dependencies):
        causes.add(CapabilityReason.INFERRED_FIELD_USED)
    return causes


def _eligible_rate_bps(eligible_count: int, source_count: int) -> int:
    return eligible_count * 10_000 // source_count


def _add_coverage_cause(
    definition: DetectorDefinition,
    *,
    eligible_count: int,
    source_count: int,
    causes: set[CapabilityReason],
) -> None:
    if eligible_count < definition.min_eligible_rows or (
        eligible_count * 10_000 < source_count * definition.min_eligible_rate_bps
    ):
        causes.add(CapabilityReason.INSUFFICIENT_ELIGIBLE_ROWS)


def _sorted_causes(causes: set[CapabilityReason]) -> tuple[CapabilityReason, ...]:
    if not causes:
        return (CapabilityReason.READY,)
    return tuple(reason for reason in CAPABILITY_REASON_ORDER if reason in causes)


def _capability(
    *,
    detector: DetectorKind,
    source_count: int,
    parsed_count: int,
    eligible_count: int,
    dependencies: tuple[DependencyAvailability, ...],
    causes: set[CapabilityReason],
    exclusions: Counter[ExclusionReason],
    runtime: DetectorRuntimeFacts,
    finding_count: int,
) -> CapabilityDraft:
    ordered_causes = _sorted_causes(causes)
    primary = ordered_causes[0]
    exclusion_counts = tuple(
        ExclusionCount(reason_code=reason, count=count)
        for reason, count in sorted(exclusions.items(), key=lambda item: item[0].value)
        if count > 0
    )
    details = CapabilityDetails(
        source_row_count=source_count,
        parsed_row_count=parsed_count,
        eligible_row_count=eligible_count,
        excluded_row_count=source_count - eligible_count,
        eligible_rate_bps=_eligible_rate_bps(eligible_count, source_count),
        dependencies=dependencies,
        causes=ordered_causes,
        exclusion_counts=exclusion_counts,
        runtime=runtime,
    )
    return CapabilityDraft(
        detector=detector,
        detector_version=DETECTOR_VERSIONS[detector],
        status=capability_status_for_reason(primary),
        reason_code=primary,
        reason_snapshot=_REASON_SNAPSHOTS[primary],
        details=details,
        finding_count=finding_count,
    )


def _can_run(causes: set[CapabilityReason]) -> bool:
    ordered = _sorted_causes(causes)
    return capability_status_for_reason(ordered[0]) is not CapabilityStatus.UNAVAILABLE


def _build_finding(
    *,
    detector: DetectorKind,
    profile_fingerprint_value: str,
    row_nos: Iterable[int],
    evidence: CorrelationEvidence,
    reasoning: str,
) -> FindingDraft:
    participating = tuple(sorted(set(row_nos)))
    key = finding_key(
        detector=detector,
        detector_version=DETECTOR_VERSIONS[detector],
        profile_fingerprint_value=profile_fingerprint_value,
        participating_row_nos=participating,
        evidence=evidence,
    )
    return FindingDraft(
        detector=detector,
        detector_version=DETECTOR_VERSIONS[detector],
        participating_row_nos=participating,
        evidence=evidence,
        finding_key=key,
        reasoning_snapshot=reasoning,
    )


def _detect_split(
    definition: SplitInvoiceDefinition,
    batch: DetectionBatch,
    fingerprint: str,
) -> DetectorOutput:
    detector = DetectorKind.SPLIT_INVOICE
    dependency_fields = [
        UnifiedField.AMOUNT,
        UnifiedField.EXPENSE_DATE,
        UnifiedField.EMPLOYEE,
        UnifiedField.MERCHANT,
    ]
    if definition.currency_mode == "field":
        dependency_fields.append(UnifiedField.CURRENCY)
    dependencies = _dependencies(batch, dependency_fields)
    causes = _initial_causes(definition, dependencies)
    exclusions = _base_exclusions(batch)
    eligible: list[_SplitRow] = []
    unconfigured_count = 0
    conflict_count = 0
    thresholds = {
        currency: Decimal(value) for currency, value in definition.approval_thresholds.items()
    }
    for parsed in _parsed_rows(batch):
        record = parsed.record
        if record.employee is None or record.merchant is None:
            exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
            continue
        if parsed.amount <= 0:
            exclusions[ExclusionReason.AMOUNT_NOT_POSITIVE] += 1
            continue
        if definition.currency_mode == "fixed":
            currency = definition.fixed_currency
            if currency is None:
                raise ValueError("fixed currency profile lost its validated currency")
            if record.currency is not None and record.currency != currency:
                conflict_count += 1
                exclusions[ExclusionReason.CURRENCY_CONFLICT] += 1
                continue
        else:
            currency = record.currency
            if currency is None:
                exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
                continue
        threshold = thresholds.get(currency)
        if threshold is None:
            unconfigured_count += 1
            exclusions[ExclusionReason.THRESHOLD_CURRENCY_UNCONFIGURED] += 1
            continue
        if parsed.amount >= threshold:
            exclusions[ExclusionReason.AMOUNT_NOT_BELOW_THRESHOLD] += 1
            continue
        if parsed.amount * 10_000 < threshold * definition.individual_floor_bps:
            exclusions[ExclusionReason.AMOUNT_BELOW_INDIVIDUAL_FLOOR] += 1
            continue
        merchant = definition.merchant_aliases.get(record.merchant, record.merchant)
        eligible.append(
            _SplitRow(
                parsed=parsed,
                employee=record.employee,
                merchant=merchant,
                currency=currency,
                threshold=threshold,
            )
        )
    if conflict_count:
        causes.add(CapabilityReason.CURRENCY_CONFLICT)
    if unconfigured_count:
        causes.add(CapabilityReason.THRESHOLD_CURRENCY_UNCONFIGURED)
    _add_coverage_cause(
        definition,
        eligible_count=len(eligible),
        source_count=len(batch.rows),
        causes=causes,
    )
    findings: list[FindingDraft] = []
    if _can_run(causes):
        groups: dict[tuple[str, str, str], list[_SplitRow]] = defaultdict(list)
        for row in eligible:
            groups[(row.employee, row.merchant, row.currency)].append(row)
        for group, group_rows in sorted(groups.items()):
            ordered = sorted(
                group_rows, key=lambda item: (item.parsed.expense_date, item.parsed.row_no)
            )
            start = 0
            while start < len(ordered):
                anchor = ordered[start].parsed.expense_date
                end = start + 1
                while (
                    end < len(ordered)
                    and (ordered[end].parsed.expense_date - anchor).days
                    <= definition.date_window_days
                ):
                    end += 1
                window = ordered[start:end]
                total = sum((item.parsed.amount for item in window), Decimal("0"))
                threshold = window[0].threshold
                matches = (
                    total > threshold
                    if definition.aggregate_operator == "gt"
                    else total >= threshold
                )
                if len(window) >= definition.min_rows and matches:
                    facts = SplitInvoiceFacts(
                        currency=window[0].currency,
                        approval_threshold=_decimal_text(threshold),
                        individual_floor_bps=definition.individual_floor_bps,
                        aggregate_operator=definition.aggregate_operator,
                        date_start=window[0].parsed.expense_date.isoformat(),
                        date_end=window[-1].parsed.expense_date.isoformat(),
                        row_count=len(window),
                        amounts=tuple(_decimal_text(item.parsed.amount) for item in window),
                        total=_decimal_text(total),
                    )
                    evidence = SplitInvoiceEvidence(
                        detector=detector,
                        detector_version=DETECTOR_VERSIONS[detector],
                        profile_fingerprint=fingerprint,
                        group_key_fingerprint=group_key_fingerprint(group),
                        facts=facts,
                    )
                    findings.append(
                        _build_finding(
                            detector=detector,
                            profile_fingerprint_value=fingerprint,
                            row_nos=(item.parsed.row_no for item in window),
                            evidence=evidence,
                            reasoning="同一精确主体、商户和币种的非重叠日期窗口合计达到审批阈值。",
                        )
                    )
                start = end
    ordered_findings = stable_findings(findings)
    runtime = SplitRuntimeFacts(
        detector=detector,
        threshold_currency_count=len(thresholds),
        unconfigured_currency_row_count=unconfigured_count,
        currency_conflict_row_count=conflict_count,
    )
    declaration = _capability(
        detector=detector,
        source_count=len(batch.rows),
        parsed_count=len(_parsed_rows(batch)),
        eligible_count=len(eligible),
        dependencies=dependencies,
        causes=causes,
        exclusions=exclusions,
        runtime=runtime,
        finding_count=len(ordered_findings),
    )
    return DetectorOutput(declaration=declaration, findings=ordered_findings)


def _parse_ascii_suffix(value: str, minimum: int, maximum: int) -> tuple[str, int, int] | None:
    index = len(value)
    while index > 0 and "0" <= value[index - 1] <= "9":
        index -= 1
    suffix = value[index:]
    if not minimum <= len(suffix) <= maximum:
        return None
    return value[:index], len(suffix), int(suffix)


def _detect_sequential(
    definition: SequentialInvoiceDefinition,
    batch: DetectionBatch,
    fingerprint: str,
) -> DetectorOutput:
    detector = DetectorKind.SEQUENTIAL_INVOICE
    field_map = {
        "employee": UnifiedField.EMPLOYEE,
        "merchant": UnifiedField.MERCHANT,
        "invoice_type": UnifiedField.INVOICE_TYPE,
    }
    dependencies = _dependencies(
        batch,
        [
            UnifiedField.INVOICE_NO,
            *(field_map[field.value] for field in definition.partition_fields),
        ],
    )
    causes = _initial_causes(definition, dependencies)
    exclusions = _base_exclusions(batch)
    eligible: list[_SequentialRow] = []
    unparseable_count = 0
    for parsed in _parsed_rows(batch):
        record = parsed.record
        if record.invoice_no is None:
            exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
            continue
        partition_values: list[str] = []
        missing_partition = False
        for field in definition.partition_fields:
            value = getattr(record, field.value)
            if value is None:
                missing_partition = True
                break
            partition_values.append(value)
        if missing_partition:
            exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
            continue
        parsed_suffix = _parse_ascii_suffix(
            record.invoice_no,
            definition.numeric_suffix_min_digits,
            definition.numeric_suffix_max_digits,
        )
        if parsed_suffix is None:
            unparseable_count += 1
            exclusions[ExclusionReason.SERIAL_UNPARSEABLE] += 1
            continue
        prefix, width, serial = parsed_suffix
        eligible.append(
            _SequentialRow(
                parsed=parsed,
                partition=tuple(partition_values),
                prefix=prefix,
                width=width,
                serial=serial,
            )
        )
    if unparseable_count:
        causes.add(CapabilityReason.SERIAL_UNPARSEABLE)
    _add_coverage_cause(
        definition,
        eligible_count=len(eligible),
        source_count=len(batch.rows),
        causes=causes,
    )
    findings: list[FindingDraft] = []
    duplicate_count = 0
    if _can_run(causes):
        groups: dict[tuple[tuple[str, ...], str, int], dict[int, list[_SequentialRow]]] = {}
        for row in eligible:
            group = (row.partition, row.prefix, row.width)
            groups.setdefault(group, {}).setdefault(row.serial, []).append(row)
        for group, serial_rows in sorted(groups.items()):
            for rows in serial_rows.values():
                rows.sort(key=lambda item: item.parsed.row_no)
                duplicate_count += len(rows) - 1
            serials = sorted(serial_rows)
            chain_start = 0
            while chain_start < len(serials):
                chain_end = chain_start + 1
                while chain_end < len(serials) and serials[chain_end] == serials[chain_end - 1] + 1:
                    chain_end += 1
                chain = serials[chain_start:chain_end]
                if len(chain) >= definition.min_sequence_length:
                    ordered_rows = tuple(
                        row.parsed.row_no for serial in chain for row in serial_rows[serial]
                    )
                    prefix = group[1]
                    width = group[2]
                    facts = SequentialInvoiceFacts(
                        prefix_fingerprint=group_key_fingerprint(prefix),
                        suffix_width=width,
                        start_serial=str(chain[0]).zfill(width),
                        end_serial=str(chain[-1]).zfill(width),
                        sequence_length=len(chain),
                        ordered_row_nos=ordered_rows,
                    )
                    evidence = SequentialInvoiceEvidence(
                        detector=detector,
                        detector_version=DETECTOR_VERSIONS[detector],
                        profile_fingerprint=fingerprint,
                        group_key_fingerprint=group_key_fingerprint(group),
                        facts=facts,
                    )
                    findings.append(
                        _build_finding(
                            detector=detector,
                            profile_fingerprint_value=fingerprint,
                            row_nos=ordered_rows,
                            evidence=evidence,
                            reasoning="同一精确分区内出现达到配置长度的最大连续发票号后缀序列。",
                        )
                    )
                chain_start = chain_end
    ordered_findings = stable_findings(findings)
    runtime = SequentialRuntimeFacts(
        detector=detector,
        serial_parse_count=len(eligible),
        serial_unparseable_count=unparseable_count,
        duplicate_serial_row_count=duplicate_count,
    )
    declaration = _capability(
        detector=detector,
        source_count=len(batch.rows),
        parsed_count=len(_parsed_rows(batch)),
        eligible_count=len(eligible),
        dependencies=dependencies,
        causes=causes,
        exclusions=exclusions,
        runtime=runtime,
        finding_count=len(ordered_findings),
    )
    return DetectorOutput(declaration=declaration, findings=ordered_findings)


def _period_key(expense_date: date, period: str) -> str:
    if period == "calendar_month":
        return expense_date.strftime("%Y-%m")
    monday = expense_date - timedelta(days=expense_date.weekday())
    return monday.isoformat()


def _median(values: Sequence[Fraction]) -> Fraction:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _detect_frequency(
    definition: FrequencyAnomalyDefinition,
    batch: DetectionBatch,
    fingerprint: str,
) -> DetectorOutput:
    detector = DetectorKind.FREQUENCY_ANOMALY
    dependencies = _dependencies(batch, [UnifiedField.EMPLOYEE, UnifiedField.EXPENSE_DATE])
    causes = _initial_causes(definition, dependencies)
    exclusions = _base_exclusions(batch)
    eligible: list[_FrequencyRow] = []
    for parsed in _parsed_rows(batch):
        if parsed.record.employee is None:
            exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
            continue
        eligible.append(
            _FrequencyRow(
                parsed=parsed,
                employee=parsed.record.employee,
                period_key=_period_key(parsed.expense_date, definition.period),
            )
        )
    _add_coverage_cause(
        definition,
        eligible_count=len(eligible),
        source_count=len(batch.rows),
        causes=causes,
    )
    periods: dict[str, dict[str, list[_FrequencyRow]]] = {}
    for row in eligible:
        periods.setdefault(row.period_key, {}).setdefault(row.employee, []).append(row)
    eligible_periods = {
        key: employees
        for key, employees in periods.items()
        if len(employees) >= definition.min_population
    }
    skipped_count = len(periods) - len(eligible_periods)
    if not eligible_periods:
        causes.add(CapabilityReason.INSUFFICIENT_POPULATION)
    elif skipped_count:
        causes.add(CapabilityReason.PARTIAL_PERIOD_SKIPPED)
    findings: list[FindingDraft] = []
    if _can_run(causes):
        multiplier = Fraction(Decimal(definition.mad_multiplier))
        floor = Fraction(Decimal(definition.mad_floor))
        for period_key, employees in sorted(eligible_periods.items()):
            counts = [Fraction(len(rows)) for rows in employees.values()]
            median = _median(counts)
            mad = _median([abs(value - median) for value in counts])
            denominator = max(mad, floor)
            for employee, rows in sorted(employees.items()):
                count = Fraction(len(rows))
                delta = count - median
                ratio = delta / denominator
                if len(rows) >= definition.absolute_min_count and delta > 0 and ratio >= multiplier:
                    facts = FrequencyAnomalyFacts(
                        period_key=period_key,
                        population=len(employees),
                        count=len(rows),
                        median=RationalValue.from_fraction(median),
                        mad=RationalValue.from_fraction(mad),
                        effective_denominator=RationalValue.from_fraction(denominator),
                        multiplier=RationalValue.from_fraction(multiplier),
                        positive_delta=RationalValue.from_fraction(delta),
                        observed_ratio=RationalValue.from_fraction(ratio),
                    )
                    evidence = FrequencyAnomalyEvidence(
                        detector=detector,
                        detector_version=DETECTOR_VERSIONS[detector],
                        profile_fingerprint=fingerprint,
                        group_key_fingerprint=group_key_fingerprint((period_key, employee)),
                        facts=facts,
                    )
                    findings.append(
                        _build_finding(
                            detector=detector,
                            profile_fingerprint_value=fingerprint,
                            row_nos=(row.parsed.row_no for row in rows),
                            evidence=evidence,
                            reasoning="该员工在固定日历周期内的报销频次达到精确 median/MAD 阈值。",
                        )
                    )
    ordered_findings = stable_findings(findings)
    runtime = FrequencyRuntimeFacts(
        detector=detector,
        period_count=len(periods),
        eligible_period_count=len(eligible_periods),
        skipped_period_count=skipped_count,
    )
    declaration = _capability(
        detector=detector,
        source_count=len(batch.rows),
        parsed_count=len(_parsed_rows(batch)),
        eligible_count=len(eligible),
        dependencies=dependencies,
        causes=causes,
        exclusions=exclusions,
        runtime=runtime,
        finding_count=len(ordered_findings),
    )
    return DetectorOutput(declaration=declaration, findings=ordered_findings)


def _detect_spatiotemporal(
    definition: SpatiotemporalTier0Definition,
    batch: DetectionBatch,
    fingerprint: str,
) -> DetectorOutput:
    detector = DetectorKind.SPATIOTEMPORAL_TIER0
    dependencies = _dependencies(
        batch, [UnifiedField.EMPLOYEE, UnifiedField.EXPENSE_DATE, UnifiedField.LOCATION]
    )
    causes = _initial_causes(definition, dependencies)
    exclusions = _base_exclusions(batch)
    eligible: list[_SpatiotemporalRow] = []
    location_value_count = 0
    unmapped_count = 0
    for parsed in _parsed_rows(batch):
        record = parsed.record
        if record.employee is None or record.location is None:
            exclusions[ExclusionReason.REQUIRED_VALUE_MISSING] += 1
            continue
        location_value_count += 1
        zone = definition.location_aliases.get(record.location)
        if zone is None:
            unmapped_count += 1
            exclusions[ExclusionReason.LOCATION_UNMAPPED] += 1
            continue
        eligible.append(_SpatiotemporalRow(parsed=parsed, employee=record.employee, zone=zone))
    _add_coverage_cause(
        definition,
        eligible_count=len(eligible),
        source_count=len(batch.rows),
        causes=causes,
    )
    mapping_rate = len(eligible) * 10_000 // location_value_count if location_value_count else 0
    if mapping_rate < definition.min_zone_mapping_rate_bps:
        causes.add(CapabilityReason.ZONE_MAPPING_BELOW_MINIMUM)
    elif unmapped_count:
        causes.add(CapabilityReason.LOCATION_UNMAPPED)
    findings: list[FindingDraft] = []
    if _can_run(causes):
        groups: dict[tuple[str, date], dict[str, list[_SpatiotemporalRow]]] = {}
        for row in eligible:
            groups.setdefault((row.employee, row.parsed.expense_date), {}).setdefault(
                row.zone, []
            ).append(row)
        configured_pairs = set(definition.incompatible_zone_pairs)
        for group, zones in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1])):
            present = sorted(zones)
            matched: list[tuple[str, str]] = []
            for left_index, left in enumerate(present):
                for right in present[left_index + 1 :]:
                    pair = (left, right)
                    if pair in configured_pairs:
                        matched.append(pair)
            if not matched:
                continue
            involved = {zone for pair in matched for zone in pair}
            row_nos = [
                row.parsed.row_no
                for zone in sorted(involved)
                for row in sorted(zones[zone], key=lambda item: item.parsed.row_no)
            ]
            facts = SpatiotemporalTier0Facts(
                expense_date=group[1].isoformat(),
                incompatible_zone_pairs=tuple(matched),
                zone_row_counts=tuple(
                    ZoneRowCount(zone_id=zone, row_count=len(zones[zone]))
                    for zone in sorted(involved)
                ),
            )
            evidence = SpatiotemporalTier0Evidence(
                detector=detector,
                detector_version=DETECTOR_VERSIONS[detector],
                profile_fingerprint=fingerprint,
                group_key_fingerprint=group_key_fingerprint(group),
                facts=facts,
            )
            findings.append(
                _build_finding(
                    detector=detector,
                    profile_fingerprint_value=fingerprint,
                    row_nos=row_nos,
                    evidence=evidence,
                    reasoning="同一员工同日出现于配置明确声明为不相容的地点分区。",
                )
            )
    ordered_findings = stable_findings(findings)
    runtime = SpatiotemporalRuntimeFacts(
        detector=detector,
        location_value_count=location_value_count,
        zone_mapped_row_count=len(eligible),
        zone_unmapped_row_count=unmapped_count,
        zone_mapping_rate_bps=mapping_rate,
    )
    declaration = _capability(
        detector=detector,
        source_count=len(batch.rows),
        parsed_count=len(_parsed_rows(batch)),
        eligible_count=len(eligible),
        dependencies=dependencies,
        causes=causes,
        exclusions=exclusions,
        runtime=runtime,
        finding_count=len(ordered_findings),
    )
    return DetectorOutput(declaration=declaration, findings=ordered_findings)


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _expect_split(definition: DetectorDefinition) -> SplitInvoiceDefinition:
    if not isinstance(definition, SplitInvoiceDefinition):
        raise TypeError("split detector definition mismatch")
    return definition


def _expect_sequential(definition: DetectorDefinition) -> SequentialInvoiceDefinition:
    if not isinstance(definition, SequentialInvoiceDefinition):
        raise TypeError("sequential detector definition mismatch")
    return definition


def _expect_frequency(definition: DetectorDefinition) -> FrequencyAnomalyDefinition:
    if not isinstance(definition, FrequencyAnomalyDefinition):
        raise TypeError("frequency detector definition mismatch")
    return definition


def _expect_spatiotemporal(definition: DetectorDefinition) -> SpatiotemporalTier0Definition:
    if not isinstance(definition, SpatiotemporalTier0Definition):
        raise TypeError("spatiotemporal detector definition mismatch")
    return definition
