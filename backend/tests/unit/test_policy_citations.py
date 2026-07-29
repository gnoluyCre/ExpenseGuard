from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest

from app.core.policies.canonical import (
    canonical_binding_fingerprint,
    canonical_report_fingerprint,
)
from app.core.policies.citations import (
    CitationVerificationError,
    VerifiedCitation,
    verify_exact_quote,
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _verify(
    clause_text: str,
    exact_quote: str,
    quote_start: int,
    quote_end: int,
    *,
    clause_id: UUID | None = None,
) -> VerifiedCitation:
    return verify_exact_quote(
        clause_id=clause_id or uuid4(),
        clause_text=clause_text,
        quote_start=quote_start,
        quote_end=quote_end,
        exact_quote=exact_quote,
    )


def test_verify_exact_quote_accepts_nonempty_end_exclusive_code_point_slice() -> None:
    clause_id = uuid4()
    clause_text = "A😀制度原文\n第二行"

    verified = _verify(clause_text, "😀制度原文\n", 1, 7, clause_id=clause_id)

    assert verified == VerifiedCitation(
        clause_id=clause_id,
        quote_start=1,
        quote_end=7,
        exact_quote="😀制度原文\n",
    )


def test_verify_exact_quote_uses_claimed_offsets_for_repeated_text() -> None:
    verified = _verify("制度；制度", "制度", 3, 5)

    assert (verified.quote_start, verified.quote_end) == (3, 5)


@pytest.mark.parametrize(
    ("clause_text", "exact_quote", "quote_start", "quote_end"),
    [
        ("制度原文", "", 0, 0),
        (" \t\n制度", " \t\n", 0, 3),
        ("制度原文", "制度", -1, 1),
        ("制度原文", "制", 1, 1),
        ("制度原文", "", 2, 2),
        ("制度原文", "制度", 2, 1),
        ("制度原文", "原文", 2, 5),
        (" 制度 ", "制度", 0, 4),  # would match only after trimming
        ("é", "e\u0301", 0, 1),  # NFC difference
        ("①", "1", 0, 1),  # NFKC difference
        ("Policy", "policy", 0, 6),  # case folding
        ("ＡＢ", "AB", 0, 2),  # full-width conversion
        ("第一行\n第二行", "第一行 第二行", 0, 7),  # newline collapse
        ("制度，原文", "制度,原文", 0, 5),  # punctuation replacement
        ("报销制度", "报稍制度", 0, 4),  # edit-distance/fuzzy match
        ("第一条", "第一条第二条", 0, 6),  # cross-clause concatenation
    ],
)
def test_verify_exact_quote_rejects_every_nonexact_case(
    clause_text: str,
    exact_quote: str,
    quote_start: int,
    quote_end: int,
) -> None:
    with pytest.raises(CitationVerificationError):
        _verify(clause_text, exact_quote, quote_start, quote_end)


def test_verification_failure_exposes_only_stable_safe_code() -> None:
    clause_text = "高度敏感制度原文"
    candidate = "高度敏感候选引用"

    with pytest.raises(CitationVerificationError) as caught:
        _verify(clause_text, candidate, 0, len(clause_text))

    error = caught.value
    assert str(error) == CitationVerificationError.code
    assert error.args == (CitationVerificationError.code,)
    assert clause_text not in repr(error)
    assert candidate not in repr(error)


def test_binding_fingerprint_is_stable_and_binds_quote_offsets_and_hashes() -> None:
    common: dict[str, object] = {
        "tenant_id": "6dcf2b52-9705-426d-a15c-19330ab62117",
        "rule_config_id": "257b94d8-3796-4e91-a76f-50a045971635",
        "policy_family_id": "8e35292b-542d-4937-aaf9-c401a20257ca",
        "policy_document_id": "401ad80f-cd39-41bb-814b-8bd49f4502d0",
        "policy_clause_id": "61a5f8c6-99a4-428d-b41b-82241e5107bb",
        "quote_start": 1,
        "quote_end": 3,
        "quote_sha256": _sha256("制度"),
        "clause_text_sha256": _sha256("A制度原文"),
        "citation_order": 1,
    }
    first = canonical_binding_fingerprint(**common)  # type: ignore[arg-type]
    second = canonical_binding_fingerprint(**dict(reversed(common.items())))  # type: ignore[arg-type]

    assert first == second
    for changed in (
        {"quote_start": 0},
        {"quote_end": 4},
        {"quote_sha256": _sha256("原文")},
        {"clause_text_sha256": _sha256("B制度原文")},
        {"citation_order": 2},
    ):
        assert canonical_binding_fingerprint(**(common | changed)) != first  # type: ignore[arg-type]


def _report_fingerprint(
    manifest: Mapping[str, Sequence[Mapping[str, object]]],
) -> str:
    return canonical_report_fingerprint(
        tenant_id="6dcf2b52-9705-426d-a15c-19330ab62117",
        file_version_id="7c0e57ab-e9f3-4f8d-9c10-f153ecee1227",
        validation_run_id="e27cb708-9c14-4e9e-bc40-e4e390636ac2",
        source_content_sha256=_sha256("source"),
        mapping_version="mapping-v1",
        ruleset_fingerprint=_sha256("ruleset"),
        binding_policy_manifest=manifest,
        report_schema_version="report-v1",
        template_version="template-v1",
        attention_mapping_version="attention-v1",
    )


def test_report_fingerprint_canonicalizes_groups_but_preserves_citation_order() -> None:
    first_citation = {"binding_fingerprint": "binding-a", "citation_order": 1}
    second_citation = {"binding_fingerprint": "binding-b", "citation_order": 2}
    manifest = {"rule-b": [second_citation], "rule-a": [first_citation, second_citation]}
    reordered_groups = {
        "rule-a": [first_citation, second_citation],
        "rule-b": [second_citation],
    }

    assert _report_fingerprint(manifest) == _report_fingerprint(reordered_groups)
    assert _report_fingerprint(manifest) != _report_fingerprint(
        {"rule-b": [second_citation], "rule-a": [second_citation, first_citation]}
    )


def test_report_fingerprint_excludes_index_model_and_chunker_provenance() -> None:
    base_citation: dict[str, object] = {
        "binding_fingerprint": "binding-a",
        "document_content_sha256": _sha256("document"),
        "clause_text_sha256": _sha256("clause"),
        "quote_sha256": _sha256("quote"),
        "citation_order": 1,
    }
    with_provenance = base_citation | {
        "index_generation": 9,
        "index_generation_id": str(uuid4()),
        "embedding_model_family": "embedding-family",
        "embedding_model_fingerprint": _sha256("embedding"),
        "rerank_model_family": "rerank-family",
        "rerank_model_revision": "rerank-r9",
        "chunker_version": "chunker-v9",
    }

    assert _report_fingerprint({"rule-a": [base_citation]}) == _report_fingerprint(
        {"rule-a": [with_provenance]}
    )


def test_report_fingerprint_changes_when_effective_snapshot_content_changes() -> None:
    base = {
        "binding_fingerprint": "binding-a",
        "document_content_sha256": _sha256("document"),
        "citation_order": 1,
    }
    changed = base | {"document_content_sha256": _sha256("changed-document")}

    assert _report_fingerprint({"rule-a": [base]}) != _report_fingerprint({"rule-a": [changed]})
