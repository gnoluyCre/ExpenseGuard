import httpx
import pytest

from app.core.retrieval.providers import (
    DeterministicLocalModels,
    HttpLocalModels,
    LocalModelError,
)
from app.settings import Settings


async def test_fake_models_are_deterministic() -> None:
    provider = DeterministicLocalModels(vector_size=8)
    assert await provider.embed(["制度"]) == await provider.embed(["制度"])
    scores = await provider.rerank("交通费用", ["交通费用标准", "住宿规定"])
    assert scores[0] > scores[1]


def test_http_provider_rejects_public_or_unlisted_endpoint() -> None:
    with pytest.raises(LocalModelError) as caught:
        HttpLocalModels(
            base_url="https://api.example.com",
            allowed_hosts=["127.0.0.1"],
            embedding_model="embed",
            rerank_model="rerank",
            vector_size=2,
        )
    assert caught.value.code == "POLICY_MODEL_ENDPOINT_FORBIDDEN"


def test_production_rejects_fake_models() -> None:
    with pytest.raises(ValueError, match="prod 环境禁止"):
        Settings(app_env="prod", policy_embedding_provider="fake")


async def test_http_provider_validates_response_shape() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/embeddings":
            return httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
            )
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 0.75}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpLocalModels(
        base_url="http://127.0.0.1:7997",
        allowed_hosts=["127.0.0.1"],
        embedding_model="embed",
        rerank_model="rerank",
        vector_size=2,
        client=client,
    )
    assert await provider.embed(["a"]) == ((0.1, 0.2),)
    assert await provider.rerank("q", ["a"]) == (0.75,)
    await client.aclose()
