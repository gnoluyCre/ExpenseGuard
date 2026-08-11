from decimal import Decimal

from app.core.agent.models import TokenUsage
from app.core.observability.model_usage import ModelPricing


def test_model_pricing_is_explicit_and_never_guesses() -> None:
    usage = TokenUsage(prompt_tokens=1_000, completion_tokens=500, total_tokens=1_500)
    assert ModelPricing().estimate(usage) is None
    assert ModelPricing(input_per_million_usd=Decimal("2")).estimate(usage) is None
    assert ModelPricing(
        input_per_million_usd=Decimal("2"),
        output_per_million_usd=Decimal("6"),
    ).estimate(usage) == Decimal("0.005")
    assert ModelPricing(
        input_per_million_usd=Decimal("0"),
        output_per_million_usd=Decimal("0"),
    ).estimate(usage) == Decimal("0")
