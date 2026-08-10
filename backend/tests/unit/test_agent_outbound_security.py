import pytest

from app.core.agent.outbound_security import OutboundSafetyError, assert_safe_outbound_text


@pytest.mark.parametrize("value", ["张三", "13800138000", "11010519491231002X"])
def test_outbound_scanner_blocks_plaintext_and_identifier_patterns(value: str) -> None:
    with pytest.raises(OutboundSafetyError):
        assert_safe_outbound_text(
            f'{{"subject":"{value}"}}',
            forbidden_values=("张三",),
        )


def test_outbound_scanner_accepts_stable_tokens_and_financial_fields() -> None:
    assert_safe_outbound_text(
        '{"employee":"EMP_v1_0123456789abcdef","amount":"138.00","date":"2026-08-10"}'
    )
