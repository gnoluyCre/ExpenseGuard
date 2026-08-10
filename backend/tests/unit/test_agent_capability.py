from pydantic import SecretStr

from app.core.agent.capability import (
    build_configured_provider,
    declare_investigation_capability,
)
from app.settings import Settings


def test_missing_configuration_is_explicitly_unavailable() -> None:
    settings = Settings(_env_file=None)
    capability = declare_investigation_capability(settings)
    assert capability.status == "unavailable"
    assert capability.reason_code == "PROVIDER_DISABLED"
    assert build_configured_provider(settings) is None


def test_configured_capability_does_not_probe_network() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key=SecretStr("test-only-key"),
        llm_model="model-family",
        pii_tokenization_key=SecretStr("x" * 32),
    )
    capability = declare_investigation_capability(settings)
    assert capability.status == "enabled"
    assert capability.reason_code == "CONFIGURED_NOT_PROBED"
    assert capability.provider_model == "model-family"
    assert build_configured_provider(settings) is not None


def test_short_redaction_key_keeps_provider_unavailable() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.test/v1",
        llm_api_key=SecretStr("test-only-key"),
        llm_model="model-family",
        pii_tokenization_key=SecretStr("too-short"),
    )
    capability = declare_investigation_capability(settings)
    assert capability.status == "unavailable"
    assert capability.reason_code == "REDACTION_KEY_MISSING"
