"""Pure deterministic F6 cross-row detection domain."""

from app.core.detection.canonical import (
    canonical_bytes,
    canonical_json,
    finding_key,
    profile_canonical_json,
    profile_fingerprint,
)
from app.core.detection.engine import run_detection_core, stable_findings
from app.core.detection.errors import (
    DetectionError,
    DetectionInputError,
    DetectionInternalError,
    DetectionNotFoundError,
)
from app.core.detection.models import (
    DetectionBatch,
    DetectionCoreResult,
    DetectionProfileDefinition,
    DetectorKind,
)

__all__ = [
    "DetectionBatch",
    "DetectionCoreResult",
    "DetectionError",
    "DetectionInputError",
    "DetectionInternalError",
    "DetectionNotFoundError",
    "DetectionProfileDefinition",
    "DetectorKind",
    "canonical_bytes",
    "canonical_json",
    "finding_key",
    "profile_canonical_json",
    "profile_fingerprint",
    "run_detection_core",
    "stable_findings",
]
