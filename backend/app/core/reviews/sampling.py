"""Pure, reproducible F5 clearance-sampling primitives."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Iterable, Sequence

from app.core.policies.canonical import canonical_sha256
from app.core.reviews.models import SamplingConfigParameters, SamplingSelection

_SCORE_DOMAIN = b"expenseguard:f5:sampling:sha256-rank-v1\0"
_SEED_BYTES = 32


def canonical_sampling_config_payload(
    parameters: SamplingConfigParameters,
) -> dict[str, int | str]:
    """Return the complete version-independent identity of sampling parameters."""
    return {
        "algorithm_version": parameters.algorithm_version,
        "max_sample_size": parameters.max_sample_size,
        "min_sample_size": parameters.min_sample_size,
        "rate_bps": parameters.rate_bps,
        "schema_version": 1,
    }


def canonical_sampling_config_fingerprint(parameters: SamplingConfigParameters) -> str:
    """Hash the canonical parameter identity without tenant or config-version drift."""
    return canonical_sha256(canonical_sampling_config_payload(parameters))


def calculate_sample_size(eligible_count: int, parameters: SamplingConfigParameters) -> int:
    """Apply the frozen integer-only F5 sample-size formula."""
    if eligible_count < 0:
        raise ValueError("eligible_count must be non-negative")
    if eligible_count == 0:
        return 0
    rate_size = (eligible_count * parameters.rate_bps + 9_999) // 10_000
    return min(
        eligible_count,
        parameters.max_sample_size,
        max(parameters.min_sample_size, rate_size),
    )


def generate_seed_hex() -> str:
    """Create the one-time persisted 32-byte CSPRNG seed for a new plan."""
    return secrets.token_hex(_SEED_BYTES)


def score_sampling_row(
    *,
    seed_hex: str,
    tenant_id: uuid.UUID,
    report_run_id: uuid.UUID,
    row_no: int,
) -> bytes:
    """Calculate the exact sha256-rank-v1 score bytes for one eligible row."""
    seed = _decode_seed(seed_hex)
    if row_no < 1:
        raise ValueError("row_no must be positive")
    return hashlib.sha256(
        _SCORE_DOMAIN
        + seed
        + tenant_id.bytes
        + report_run_id.bytes
        + row_no.to_bytes(8, "big", signed=False)
    ).digest()


def select_sampling_rows(
    *,
    eligible_row_nos: Iterable[int],
    parameters: SamplingConfigParameters,
    seed_hex: str,
    tenant_id: uuid.UUID,
    report_run_id: uuid.UUID,
) -> tuple[SamplingSelection, ...]:
    """Select a deterministic prefix independent of input and database row order."""
    rows = tuple(eligible_row_nos)
    if any(row_no < 1 for row_no in rows):
        raise ValueError("eligible row numbers must be positive")
    if len(set(rows)) != len(rows):
        raise ValueError("eligible row numbers must be unique")

    scored = sorted(
        (
            (
                score_sampling_row(
                    seed_hex=seed_hex,
                    tenant_id=tenant_id,
                    report_run_id=report_run_id,
                    row_no=row_no,
                ),
                row_no,
            )
            for row_no in rows
        ),
        key=lambda item: (item[0], item[1]),
    )
    sample_size = calculate_sample_size(len(rows), parameters)
    return tuple(
        SamplingSelection(
            row_no=row_no,
            selection_rank=rank,
            selection_score_sha256=score.hex(),
        )
        for rank, (score, row_no) in enumerate(scored[:sample_size], start=1)
    )


def verify_sampling_selection(
    *,
    eligible_row_nos: Sequence[int],
    parameters: SamplingConfigParameters,
    seed_hex: str,
    tenant_id: uuid.UUID,
    report_run_id: uuid.UUID,
    selections: Sequence[SamplingSelection],
) -> None:
    """Mechanically reject any persisted selection that cannot be reproduced."""
    expected = select_sampling_rows(
        eligible_row_nos=eligible_row_nos,
        parameters=parameters,
        seed_hex=seed_hex,
        tenant_id=tenant_id,
        report_run_id=report_run_id,
    )
    if tuple(selections) != expected:
        raise ValueError("sampling selection does not match the frozen plan inputs")


def _decode_seed(seed_hex: str) -> bytes:
    if len(seed_hex) != _SEED_BYTES * 2 or seed_hex.lower() != seed_hex:
        raise ValueError("seed_hex must be 64 lowercase hexadecimal characters")
    try:
        seed = bytes.fromhex(seed_hex)
    except ValueError as exc:
        raise ValueError("seed_hex must be 64 lowercase hexadecimal characters") from exc
    if len(seed) != _SEED_BYTES:
        raise ValueError("seed_hex must encode exactly 32 bytes")
    return seed
