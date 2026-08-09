"""Strongly typed service results for CP-F6.3."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.detection.models import CapabilityStatus, DetectorKind


@dataclass(frozen=True)
class DetectionConfigResult:
    id: uuid.UUID
    tenant_id: uuid.UUID
    version: int
    definition: dict[str, Any]
    config_fingerprint: str
    algorithm_bundle_version: str
    created_by: uuid.UUID
    created_at: datetime
    change_reason: str
    reused_existing: bool


@dataclass(frozen=True)
class DetectionConfigPage:
    items: tuple[DetectionConfigResult, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class DetectionRunResult:
    id: uuid.UUID
    tenant_id: uuid.UUID
    file_version_id: uuid.UUID
    detection_config_id: uuid.UUID
    config_version: int
    config_fingerprint: str
    algorithm_bundle_version: str
    input_fingerprint: str
    run_fingerprint: str
    source_row_count: int
    parsed_row_count: int
    error_row_count: int
    finding_count: int
    created_by: uuid.UUID
    created_at: datetime
    completed_at: datetime
    reused_existing: bool


@dataclass(frozen=True)
class CapabilityResult:
    detector: DetectorKind
    detector_version: str
    status: CapabilityStatus
    reason_code: str
    reason: str
    details: dict[str, Any]
    finding_count: int


@dataclass(frozen=True)
class BatchDetectionResult:
    run: DetectionRunResult | None
    capabilities: tuple[CapabilityResult, ...]
    current_config_fingerprint: str | None
    config_stale: bool


@dataclass(frozen=True)
class FindingSummary:
    id: uuid.UUID
    run_id: uuid.UUID
    detector: DetectorKind
    detector_version: str
    finding_key: str
    reasoning: str
    participating_row_count: int
    first_row_no: int


@dataclass(frozen=True)
class FindingPage:
    items: tuple[FindingSummary, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ParticipatingRowResult:
    ordinal: int
    row_no: int
    raw: dict[str, Any]
    normalized: dict[str, Any] | None
    parse_error_code: str | None


@dataclass(frozen=True)
class FindingDetail:
    id: uuid.UUID
    run_id: uuid.UUID
    file_version_id: uuid.UUID
    detector: DetectorKind
    detector_version: str
    finding_key: str
    evidence: dict[str, Any]
    reasoning: str
    rows: tuple[ParticipatingRowResult, ...]
    completed: int
    total: int
