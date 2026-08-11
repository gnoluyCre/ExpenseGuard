"""Safe task-attributed model usage events for logs/OTLP collectors."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from opentelemetry import trace

from app.core.agent.models import TokenUsage

logger = logging.getLogger("expenseguard.model_usage")
_MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPricing:
    input_per_million_usd: Decimal | None = None
    output_per_million_usd: Decimal | None = None

    def estimate(self, usage: TokenUsage) -> Decimal | None:
        if self.input_per_million_usd is None or self.output_per_million_usd is None:
            return None
        return (
            Decimal(usage.prompt_tokens) * self.input_per_million_usd
            + Decimal(usage.completion_tokens) * self.output_per_million_usd
        ) / _MILLION


def record_model_usage(
    *,
    investigation_run_id: uuid.UUID,
    file_version_id: uuid.UUID,
    step_no: int,
    provider: str,
    model: str,
    duration_ms: float,
    usage: TokenUsage,
    pricing: ModelPricing,
) -> None:
    """Emit metadata only; prompt, tool content, PII and credentials are forbidden."""
    cost = pricing.estimate(usage)
    with trace.get_tracer("expenseguard.investigation").start_as_current_span(
        "investigation.llm.step"
    ) as span:
        span.set_attribute("expenseguard.task_id", str(file_version_id))
        span.set_attribute("expenseguard.investigation_run_id", str(investigation_run_id))
        span.set_attribute("expenseguard.phase", "investigation")
        span.set_attribute("expenseguard.step_no", step_no)
        span.set_attribute("gen_ai.provider.name", provider)
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
        span.set_attribute("expenseguard.duration_ms", duration_ms)
        if cost is not None:
            span.set_attribute("expenseguard.estimated_cost_usd", float(cost))
    logger.info(
        "llm.step_completed",
        extra={
            "task_id": str(file_version_id),
            "investigation_run_id": str(investigation_run_id),
            "file_version_id": str(file_version_id),
            "phase": "investigation",
            "step_no": step_no,
            "provider": provider,
            "model": model,
            "duration_ms": round(duration_ms, 3),
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "estimated_cost_usd": (
                str(cost.quantize(Decimal("0.00000001"))) if cost is not None else None
            ),
        },
    )
