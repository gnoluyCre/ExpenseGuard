"""Build the minimal allowlisted row payload that may be sent to an LLM."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.redaction import PiiKind, pii_source_hmac, stable_pii_token
from app.core.parsing.models import NormalizedExpenseRecord


class SanitizedRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_no: int = Field(ge=1)
    amount: str
    expense_date: str
    employee_token: str | None = None
    supplier_token: str | None = None
    expense_type: str | None = None
    invoice_type: str | None = None
    submission_date: str | None = None
    currency: str | None = None


class PiiTokenDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_version: str = Field(pattern=r"^v[1-9][0-9]?$", max_length=16)
    pii_kind: PiiKind
    source_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")
    token: str = Field(pattern=r"^[A-Z]{3}_v[1-9][0-9]?_[0-9a-f]{16}$")


class SanitizedRowResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row: SanitizedRow
    token_drafts: tuple[PiiTokenDraft, ...]


def sanitize_expense_row(
    *,
    tenant_id: uuid.UUID,
    row_no: int,
    record: NormalizedExpenseRecord,
    secret: bytes,
    token_version: int,
) -> SanitizedRowResult:
    employee_token, employee_draft = _tokenize_optional(
        tenant_id=tenant_id,
        kind=PiiKind.EMPLOYEE_NAME,
        value=record.employee,
        secret=secret,
        version=token_version,
    )
    supplier_token, supplier_draft = _tokenize_optional(
        tenant_id=tenant_id,
        kind=PiiKind.SUPPLIER,
        value=record.merchant,
        secret=secret,
        version=token_version,
    )
    drafts = tuple(draft for draft in (employee_draft, supplier_draft) if draft is not None)
    return SanitizedRowResult(
        row=SanitizedRow(
            row_no=row_no,
            amount=record.amount,
            expense_date=record.expense_date,
            employee_token=employee_token,
            supplier_token=supplier_token,
            expense_type=record.expense_type,
            invoice_type=record.invoice_type,
            submission_date=record.submission_date,
            currency=record.currency,
        ),
        token_drafts=drafts,
    )


def _tokenize_optional(
    *,
    tenant_id: uuid.UUID,
    kind: PiiKind,
    value: str | None,
    secret: bytes,
    version: int,
) -> tuple[str | None, PiiTokenDraft | None]:
    if value is None:
        return None, None
    token = stable_pii_token(
        secret=secret,
        tenant_id=tenant_id,
        kind=kind,
        value=value,
        version=version,
    )
    return token, PiiTokenDraft(
        token_version=f"v{version}",
        pii_kind=kind,
        source_hmac=pii_source_hmac(
            secret=secret,
            tenant_id=tenant_id,
            kind=kind,
            value=value,
            version=version,
        ),
        token=token,
    )
