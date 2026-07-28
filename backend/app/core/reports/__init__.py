"""F4 immutable report assembly and snapshot reads."""

from app.core.reports.models import (
    CitationSnapshot,
    ParseErrorSnapshot,
    ReportItemSnapshot,
    ReportSnapshot,
    ReportSummary,
)
from app.core.reports.service import (
    ATTENTION_MAPPING_VERSION,
    REPORT_TEMPLATE_VERSION,
    InvalidReportIdempotencyKeyError,
    ReportError,
    ReportInternalError,
    attention_group_for_verdict,
    generate_report,
    idempotency_key_hash,
    load_report_snapshot,
    report_item_order_key,
    report_request_fingerprint,
)

__all__ = [
    "ATTENTION_MAPPING_VERSION",
    "REPORT_TEMPLATE_VERSION",
    "CitationSnapshot",
    "InvalidReportIdempotencyKeyError",
    "ParseErrorSnapshot",
    "ReportError",
    "ReportInternalError",
    "ReportItemSnapshot",
    "ReportSnapshot",
    "ReportSummary",
    "attention_group_for_verdict",
    "generate_report",
    "idempotency_key_hash",
    "load_report_snapshot",
    "report_item_order_key",
    "report_request_fingerprint",
]
