"""Configuration-only capability declaration for F7.

Readiness never probes a paid model endpoint.  A configured provider is only a
declaration that the investigation path may be attempted.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent.provider import OpenAiCompatibleProvider
from app.settings import Settings


class InvestigationCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["enabled", "unavailable"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    reason: str = Field(min_length=1, max_length=500)
    provider_kind: Literal["disabled", "openai_compatible"]
    provider_model: str | None = Field(default=None, max_length=256)
    max_steps: int = Field(ge=1, le=12)
    policy_retrieval_status: Literal["enabled", "unavailable"]


def declare_investigation_capability(settings: Settings) -> InvestigationCapability:
    if settings.llm_provider == "disabled":
        return _unavailable(settings, "PROVIDER_DISABLED", "异常取证模型尚未配置")
    if not settings.llm_base_url.strip():
        return _unavailable(settings, "BASE_URL_MISSING", "异常取证模型地址尚未配置")
    parsed = urlparse(settings.llm_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return _unavailable(settings, "BASE_URL_INVALID", "异常取证模型地址不符合安全约束")
    if not settings.llm_api_key.get_secret_value().strip():
        return _unavailable(settings, "API_KEY_MISSING", "异常取证模型凭据尚未配置")
    if not settings.llm_model.strip():
        return _unavailable(settings, "MODEL_MISSING", "异常取证模型名称尚未配置")
    if len(settings.pii_tokenization_key.get_secret_value().encode()) < 32:
        return _unavailable(settings, "REDACTION_KEY_MISSING", "PII 脱敏密钥尚未安全配置")
    return InvestigationCapability(
        status="enabled",
        reason_code="CONFIGURED_NOT_PROBED",
        reason="异常取证模型已配置；readiness 未发起真实模型调用",
        provider_kind=settings.llm_provider,
        provider_model=settings.llm_model,
        max_steps=settings.investigation_max_steps,
        policy_retrieval_status="enabled",
    )


def build_configured_provider(settings: Settings) -> OpenAiCompatibleProvider | None:
    capability = declare_investigation_capability(settings)
    if capability.status != "enabled":
        return None
    return OpenAiCompatibleProvider(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        validation_retries=settings.llm_validation_retries,
    )


def _unavailable(settings: Settings, reason_code: str, reason: str) -> InvestigationCapability:
    return InvestigationCapability(
        status="unavailable",
        reason_code=reason_code,
        reason=reason,
        provider_kind=settings.llm_provider,
        provider_model=settings.llm_model or None,
        max_steps=settings.investigation_max_steps,
        policy_retrieval_status="enabled",
    )
