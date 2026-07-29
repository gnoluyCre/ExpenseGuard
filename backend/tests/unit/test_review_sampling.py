"""Golden and boundary tests for the pure F5 sampling core."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.reviews.models import SamplingConfigParameters, SamplingSelection
from app.core.reviews.sampling import (
    calculate_sample_size,
    canonical_sampling_config_fingerprint,
    generate_seed_hex,
    score_sampling_row,
    select_sampling_rows,
    verify_sampling_selection,
)

SEED_HEX = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
TENANT_ID = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
REPORT_RUN_ID = uuid.UUID("fedcba98-7654-3210-fedc-ba9876543210")


def _parameters(
    *, rate_bps: int = 2_500, min_sample_size: int = 2, max_sample_size: int = 4
) -> SamplingConfigParameters:
    return SamplingConfigParameters(
        rate_bps=rate_bps,
        min_sample_size=min_sample_size,
        max_sample_size=max_sample_size,
    )


@pytest.mark.parametrize(
    ("eligible_count", "parameters", "expected"),
    [
        (0, _parameters(), 0),
        (1, _parameters(min_sample_size=3, max_sample_size=10), 1),
        (5, _parameters(rate_bps=1, min_sample_size=2, max_sample_size=5), 2),
        (5, _parameters(rate_bps=10_000, min_sample_size=1, max_sample_size=3), 3),
        (10_001, _parameters(rate_bps=1, min_sample_size=1, max_sample_size=20), 2),
    ],
)
def test_calculate_sample_size_uses_integer_ceil(
    eligible_count: int, parameters: SamplingConfigParameters, expected: int
) -> None:
    assert calculate_sample_size(eligible_count, parameters) == expected


def test_sampling_config_rejects_invalid_boundaries() -> None:
    with pytest.raises(ValidationError):
        _parameters(rate_bps=0)
    with pytest.raises(ValidationError):
        _parameters(rate_bps=10_001)
    with pytest.raises(ValidationError):
        _parameters(min_sample_size=0)
    with pytest.raises(ValidationError):
        _parameters(min_sample_size=3, max_sample_size=2)
    with pytest.raises(ValueError, match="non-negative"):
        calculate_sample_size(-1, _parameters())


def test_canonical_config_has_stable_golden_fingerprint() -> None:
    parameters = _parameters()
    assert (
        canonical_sampling_config_fingerprint(parameters)
        == "095b63c0bcf72110af43b42a6b9c45bb5ee63c415c2ccfa860b4f8ff51e0ecd5"
    )
    assert canonical_sampling_config_fingerprint(
        parameters.model_copy(update={"rate_bps": 2_501})
    ) != canonical_sampling_config_fingerprint(parameters)


@pytest.mark.parametrize(
    ("row_no", "expected"),
    [
        (1, "6b5e4e008a22be9d873a330eb30d5f17e00f21d3379e0738240eff22a2b1677f"),
        (2, "fe37a7ccbedd04d3a1b62a425ab5cf3a410b788fb2d2bd8c7b3b3c55202ca936"),
        (255, "c7b417819126d034ec4a224cc6483b0ac2930cd0fd028fca7e35ca7513dad01b"),
        (256, "db75ecf142eb6f606cbc105e854a98d0a2d48a6f3df6501bef9b5bcc8f7bd9fa"),
        (
            4_294_967_297,
            "6d6851443f5c7e45ae5a80d59d927c48c6d2aaa022f35c926f8d4470205c77d4",
        ),
    ],
)
def test_score_sampling_row_golden_vectors(row_no: int, expected: str) -> None:
    assert (
        score_sampling_row(
            seed_hex=SEED_HEX,
            tenant_id=TENANT_ID,
            report_run_id=REPORT_RUN_ID,
            row_no=row_no,
        ).hex()
        == expected
    )


def test_selection_is_independent_of_candidate_order() -> None:
    expected = (
        SamplingSelection(
            row_no=1,
            selection_rank=1,
            selection_score_sha256=(
                "6b5e4e008a22be9d873a330eb30d5f17e00f21d3379e0738240eff22a2b1677f"
            ),
        ),
        SamplingSelection(
            row_no=255,
            selection_rank=2,
            selection_score_sha256=(
                "c7b417819126d034ec4a224cc6483b0ac2930cd0fd028fca7e35ca7513dad01b"
            ),
        ),
    )
    first = select_sampling_rows(
        eligible_row_nos=[256, 1, 255, 2],
        parameters=_parameters(),
        seed_hex=SEED_HEX,
        tenant_id=TENANT_ID,
        report_run_id=REPORT_RUN_ID,
    )
    second = select_sampling_rows(
        eligible_row_nos=[2, 255, 1, 256],
        parameters=_parameters(),
        seed_hex=SEED_HEX,
        tenant_id=TENANT_ID,
        report_run_id=REPORT_RUN_ID,
    )
    assert first == second == expected
    verify_sampling_selection(
        eligible_row_nos=[1, 2, 255, 256],
        parameters=_parameters(),
        seed_hex=SEED_HEX,
        tenant_id=TENANT_ID,
        report_run_id=REPORT_RUN_ID,
        selections=first,
    )


def test_selection_rejects_invalid_inputs_and_tampering() -> None:
    arguments = {
        "parameters": _parameters(),
        "seed_hex": SEED_HEX,
        "tenant_id": TENANT_ID,
        "report_run_id": REPORT_RUN_ID,
    }
    with pytest.raises(ValueError, match="unique"):
        select_sampling_rows(eligible_row_nos=[1, 1], **arguments)
    with pytest.raises(ValueError, match="positive"):
        select_sampling_rows(eligible_row_nos=[0], **arguments)
    with pytest.raises(ValueError, match="lowercase hexadecimal"):
        select_sampling_rows(eligible_row_nos=[1], **(arguments | {"seed_hex": "A" * 64}))

    selected = select_sampling_rows(eligible_row_nos=[1, 2], **arguments)
    with pytest.raises(ValueError, match="does not match"):
        verify_sampling_selection(
            eligible_row_nos=[1, 2],
            selections=selected[::-1],
            **arguments,
        )


def test_generated_seed_is_lowercase_32_byte_hex() -> None:
    first = generate_seed_hex()
    second = generate_seed_hex()
    assert len(first) == len(second) == 64
    assert first == first.lower()
    assert bytes.fromhex(first)
    assert first != second
