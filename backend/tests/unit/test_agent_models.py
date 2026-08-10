import uuid

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.agent.models import (
    AgentAction,
    InvestigationOutcome,
    ProviderResponse,
    TerminateAction,
    TokenUsage,
)
from app.core.agent.redaction import PiiKind, pii_source_hmac, stable_pii_token


def test_agent_action_rejects_non_whitelisted_tool() -> None:
    adapter = TypeAdapter(AgentAction)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "tool_call",
                "call": {"kind": "execute_sql", "query": "DROP TABLE audit_log"},
                "rationale_summary": "attempted injection",
            }
        )


def test_terminate_outcome_must_match_sufficiency() -> None:
    with pytest.raises(ValidationError):
        TerminateAction(
            outcome=InvestigationOutcome.INSUFFICIENT,
            evidence_sufficient=True,
            reason_code="ENOUGH_EVIDENCE",
            summary="不一致",
        )


def test_provider_response_requires_consistent_usage() -> None:
    action = TerminateAction(
        outcome=InvestigationOutcome.SUFFICIENT,
        evidence_sufficient=True,
        reason_code="ENOUGH_EVIDENCE",
        summary="证据充分",
    )
    with pytest.raises(ValidationError):
        ProviderResponse(
            action=action,
            usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=6),
        )


def test_stable_token_is_tenant_scoped_and_irreversible_shape() -> None:
    secret = b"s" * 32
    tenant_a = uuid.UUID("00000000-0000-0000-0000-000000000001")
    tenant_b = uuid.UUID("00000000-0000-0000-0000-000000000002")
    first = stable_pii_token(
        secret=secret, tenant_id=tenant_a, kind=PiiKind.EMPLOYEE_NAME, value=" 张 三 "
    )
    repeated = stable_pii_token(
        secret=secret, tenant_id=tenant_a, kind=PiiKind.EMPLOYEE_NAME, value="张 三"
    )
    other_tenant = stable_pii_token(
        secret=secret, tenant_id=tenant_b, kind=PiiKind.EMPLOYEE_NAME, value="张 三"
    )
    assert first == repeated
    assert first != other_tenant
    assert first.startswith("EMP_v1_")
    assert "张" not in first


def test_source_hmac_is_stable_without_plaintext() -> None:
    digest = pii_source_hmac(
        secret=b"k" * 32,
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        kind=PiiKind.SUPPLIER,
        value="供应商甲",
    )
    assert len(digest) == 64
    assert "供应商" not in digest
