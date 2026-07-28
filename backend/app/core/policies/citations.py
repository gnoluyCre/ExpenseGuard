"""Pure, fail-closed verification for exact policy citations."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class CitationVerificationError(ValueError):
    """An exact citation could not be verified without exposing candidate text."""

    code = "CITATION_EXACT_VERIFICATION_FAILED"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VerifiedCitation:
    """A citation proven to be an exact code-point slice of one clause."""

    clause_id: UUID
    quote_start: int
    quote_end: int
    exact_quote: str


def verify_exact_quote(
    *,
    clause_id: UUID,
    clause_text: str,
    quote_start: int,
    quote_end: int,
    exact_quote: str,
) -> VerifiedCitation:
    """Verify the sole exact-match rule from F4 specification section 7.

    Python string indexes are Unicode code-point offsets and slicing uses an
    end-exclusive boundary.  No normalization, trimming, folding, searching,
    or fuzzy comparison is performed.
    """
    if not exact_quote:
        raise CitationVerificationError
    if not any(not character.isspace() for character in exact_quote):
        raise CitationVerificationError
    if quote_start < 0 or quote_start >= quote_end or quote_end > len(clause_text):
        raise CitationVerificationError
    if exact_quote != clause_text[quote_start:quote_end]:
        raise CitationVerificationError
    return VerifiedCitation(
        clause_id=clause_id,
        quote_start=quote_start,
        quote_end=quote_end,
        exact_quote=exact_quote,
    )
