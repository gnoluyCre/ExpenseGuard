"""OpenAI-compatible provider boundary for F7; tests use scripted responses."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.core.agent.models import AgentAction, InvestigationPrompt, ProviderResponse
from app.core.errors import ExpenseGuardError


class LlmProviderError(ExpenseGuardError):
    status_code = 503


class LlmProvider(Protocol):
    async def complete(self, prompt: InvestigationPrompt) -> ProviderResponse: ...


_ACTION_ADAPTER: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message: _Message


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    choices: list[_Choice]
    usage: _Usage = Field(default_factory=_Usage)


class OpenAiCompatibleProvider:
    """Minimal HTTP adapter with bounded schema-validation retries."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        validation_retries: int = 1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("LLM base URL must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("LLM base URL must not embed credentials")
        if not api_key.strip() or not model.strip():
            raise ValueError("LLM API key and model are required")
        if timeout_seconds <= 0:
            raise ValueError("LLM timeout must be positive")
        if not 0 <= validation_retries <= 2:
            raise ValueError("LLM validation retries must be between 0 and 2")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._validation_retries = validation_retries
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def complete(self, prompt: InvestigationPrompt) -> ProviderResponse:
        schema = _ACTION_ADAPTER.json_schema()
        request_json = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": prompt.system_instruction},
                {
                    "role": "user",
                    "content": (
                        '<evidence source="untrusted-data">\n'
                        f"{prompt.evidence_json}\n"
                        "</evidence>\n"
                        '<prior_steps source="untrusted-data">\n'
                        f"{prompt.prior_steps_json}\n"
                        "</prior_steps>"
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "expenseguard_investigation_action",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        last_invalid: Exception | None = None
        for attempt in range(self._validation_retries + 1):
            try:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=request_json,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise LlmProviderError(
                    code="INVESTIGATION_LLM_UNAVAILABLE",
                    message="异常取证模型不可用，已转人工处理",
                ) from exc
            try:
                completion = _ChatCompletion.model_validate(response.json())
                if len(completion.choices) != 1:
                    raise ValueError("provider must return exactly one choice")
                content = json.loads(completion.choices[0].message.content)
                action = _ACTION_ADAPTER.validate_python(content)
                return ProviderResponse(action=action, usage=completion.usage.model_dump())
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                last_invalid = exc
                if attempt < self._validation_retries:
                    continue
        raise LlmProviderError(
            code="INVESTIGATION_LLM_INVALID",
            message="异常取证模型响应不符合结构化契约，已转人工处理",
        ) from last_invalid

    async def aclose(self) -> None:
        """Release the request-scoped HTTP client without exposing credentials."""

        await self._client.aclose()


class ScriptedLlmProvider:
    """Deterministic zero-network provider used by unit/integration/E2E tests."""

    def __init__(self, responses: Sequence[ProviderResponse]) -> None:
        self._responses = tuple(responses)
        self._index = 0
        self.prompts: list[InvestigationPrompt] = []

    @property
    def call_count(self) -> int:
        return self._index

    async def complete(self, prompt: InvestigationPrompt) -> ProviderResponse:
        self.prompts.append(prompt)
        if self._index >= len(self._responses):
            raise LlmProviderError(
                code="INVESTIGATION_SCRIPT_EXHAUSTED",
                message="离线调查脚本没有更多响应",
            )
        response = self._responses[self._index]
        self._index += 1
        return response
