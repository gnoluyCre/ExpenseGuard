"""F4 制度与索引配置的 canonical SHA-256 指纹。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from uuid import UUID

_REPORT_PROVENANCE_KEYS = frozenset(
    {
        "chunker_version",
        "embedding_model_family",
        "embedding_model_fingerprint",
        "embedding_model_id",
        "embedding_model_revision",
        "index_generation",
        "index_generation_id",
        "rerank_model_fingerprint",
        "rerank_model_family",
        "rerank_model_id",
        "rerank_model_revision",
    }
)


def canonical_sha256(value: object) -> str:
    """对 JSON 可表达值计算稳定指纹。"""
    canonical = _canonicalize(value)
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_binding_payload(
    *,
    tenant_id: UUID | str,
    rule_config_id: UUID | str,
    policy_family_id: UUID | str,
    policy_document_id: UUID | str,
    policy_clause_id: UUID | str,
    quote_start: int,
    quote_end: int,
    quote_sha256: str,
    clause_text_sha256: str,
    citation_order: int,
) -> dict[str, object]:
    """Build the complete canonical identity of one confirmed binding."""
    return {
        "schema_version": 1,
        "citation_order": citation_order,
        "clause_text_sha256": clause_text_sha256,
        "policy_clause_id": str(policy_clause_id),
        "policy_document_id": str(policy_document_id),
        "policy_family_id": str(policy_family_id),
        "quote_end": quote_end,
        "quote_sha256": quote_sha256,
        "quote_start": quote_start,
        "rule_config_id": str(rule_config_id),
        "tenant_id": str(tenant_id),
    }


def canonical_binding_fingerprint(
    *,
    tenant_id: UUID | str,
    rule_config_id: UUID | str,
    policy_family_id: UUID | str,
    policy_document_id: UUID | str,
    policy_clause_id: UUID | str,
    quote_start: int,
    quote_end: int,
    quote_sha256: str,
    clause_text_sha256: str,
    citation_order: int,
) -> str:
    """Fingerprint one confirmed binding without storing policy text in the identity."""
    return canonical_sha256(
        canonical_binding_payload(
            tenant_id=tenant_id,
            rule_config_id=rule_config_id,
            policy_family_id=policy_family_id,
            policy_document_id=policy_document_id,
            policy_clause_id=policy_clause_id,
            quote_start=quote_start,
            quote_end=quote_end,
            quote_sha256=quote_sha256,
            clause_text_sha256=clause_text_sha256,
            citation_order=citation_order,
        )
    )


def canonical_report_payload(
    *,
    tenant_id: UUID | str,
    file_version_id: UUID | str,
    validation_run_id: UUID | str,
    source_content_sha256: str,
    mapping_version: str,
    ruleset_fingerprint: str,
    binding_policy_manifest: Mapping[str, Sequence[Mapping[str, object]]],
    report_schema_version: str,
    template_version: str,
    attention_mapping_version: str,
) -> dict[str, object]:
    """Build report identity while retaining citation order within each binding group.

    Mapping key order is canonicalized by :func:`canonical_sha256`.  Each
    group's citation sequence is deliberately retained because citation order
    is report semantics.  Retrieval index, model, and chunker provenance are
    recursively excluded: they explain how a binding candidate was found but
    cannot create a second report for the same frozen revision.
    """
    identity_manifest = {
        group_key: [_without_report_provenance(citation) for citation in citations]
        for group_key, citations in binding_policy_manifest.items()
    }
    return {
        "attention_mapping_version": attention_mapping_version,
        "binding_policy_manifest": identity_manifest,
        "file_version_id": str(file_version_id),
        "mapping_version": mapping_version,
        "report_schema_version": report_schema_version,
        "ruleset_fingerprint": ruleset_fingerprint,
        "source_content_sha256": source_content_sha256,
        "template_version": template_version,
        "tenant_id": str(tenant_id),
        "validation_run_id": str(validation_run_id),
    }


def canonical_report_fingerprint(
    *,
    tenant_id: UUID | str,
    file_version_id: UUID | str,
    validation_run_id: UUID | str,
    source_content_sha256: str,
    mapping_version: str,
    ruleset_fingerprint: str,
    binding_policy_manifest: Mapping[str, Sequence[Mapping[str, object]]],
    report_schema_version: str,
    template_version: str,
    attention_mapping_version: str,
) -> str:
    """Fingerprint the frozen inputs that can affect a report snapshot."""
    return canonical_sha256(
        canonical_report_payload(
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            validation_run_id=validation_run_id,
            source_content_sha256=source_content_sha256,
            mapping_version=mapping_version,
            ruleset_fingerprint=ruleset_fingerprint,
            binding_policy_manifest=binding_policy_manifest,
            report_schema_version=report_schema_version,
            template_version=template_version,
            attention_mapping_version=attention_mapping_version,
        )
    )


def _canonicalize(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonicalize(item) for item in value]
    return value


def _without_report_provenance(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _without_report_provenance(item)
            for key, item in value.items()
            if str(key) not in _REPORT_PROVENANCE_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_without_report_provenance(item) for item in value]
    return value
