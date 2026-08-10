"""Tenant-stable, irreversible PII tokens for outbound LLM payloads."""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
import uuid
from enum import StrEnum


class PiiKind(StrEnum):
    EMPLOYEE_NAME = "employee_name"
    SUPPLIER = "supplier"
    PHONE = "phone"
    NATIONAL_ID = "national_id"


_PREFIX: dict[PiiKind, str] = {
    PiiKind.EMPLOYEE_NAME: "EMP",
    PiiKind.SUPPLIER: "SUP",
    PiiKind.PHONE: "TEL",
    PiiKind.NATIONAL_ID: "NID",
}


def normalize_pii(value: str) -> str:
    """Normalize without guessing identity equivalence beyond case/spacing."""

    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    return " ".join(normalized.split())


def stable_pii_token(
    *,
    secret: bytes,
    tenant_id: uuid.UUID,
    kind: PiiKind,
    value: str,
    version: int = 1,
) -> str:
    """Return a tenant-scoped token; plaintext is never persisted by this helper."""

    if len(secret) < 32:
        raise ValueError("PII tokenization secret must contain at least 32 bytes")
    if version <= 0:
        raise ValueError("token version must be positive")
    normalized = normalize_pii(value)
    if not normalized:
        raise ValueError("PII value must not be blank")
    payload = f"v{version}\0{tenant_id}\0{kind.value}\0{normalized}".encode()
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{_PREFIX[kind]}_v{version}_{digest[:16]}"


def pii_source_hmac(
    *,
    secret: bytes,
    tenant_id: uuid.UUID,
    kind: PiiKind,
    value: str,
    version: int = 1,
) -> str:
    """Return the full keyed fingerprint used by the token ledger."""

    if len(secret) < 32:
        raise ValueError("PII tokenization secret must contain at least 32 bytes")
    normalized = normalize_pii(value)
    if not normalized:
        raise ValueError("PII value must not be blank")
    payload = f"source\0v{version}\0{tenant_id}\0{kind.value}\0{normalized}".encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()
