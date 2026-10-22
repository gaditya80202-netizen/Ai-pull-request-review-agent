from types import SimpleNamespace

import pytest

from backend.core.settings import settings
from backend.memory.embedder import CloudflareEmbeddingClient


def fake_response(count, dimensions=1024):
    # Return responses in reverse order to test index-based ordering.
    return SimpleNamespace(
        data=[
            SimpleNamespace(
                index=index,
                embedding=[float(index + 0.25)] * dimensions,
            )
            for index in reversed(range(count))
        ]
    )


class FakeEmbeddings:
    def __init__(self, dimensions=1024):
        self.dimensions = dimensions
        self.calls = []

    async def create(self, *, model, input):
        self.calls.append({
            "model": model,
            "input": list(input),
        })
        return fake_response(len(input), self.dimensions)


class FakeAPIClient:
    def __init__(self, dimensions=1024):
        self.embeddings = FakeEmbeddings(dimensions)
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def embedding_settings(monkeypatch):
    monkeypatch.setattr(settings, "embed_dim", 1024)
    monkeypatch.setattr(settings, "embed_batch_size", 2)
    monkeypatch.setattr(
        settings,
        "cloudflare_embedding_model",
        "@cf/qwen/qwen3-embedding-0.6b",
    )


@pytest.mark.asyncio
async def test_empty_input_returns_empty_list(embedding_settings):
    api = FakeAPIClient()
    client = CloudflareEmbeddingClient(api_client=api)

    assert await client.embed([]) == []
    assert api.embeddings.calls == []


@pytest.mark.asyncio
async def test_valid_embeddings_are_ordered_and_1024_dimensional(
    embedding_settings,
):
    api = FakeAPIClient()
    client = CloudflareEmbeddingClient(api_client=api)

    vectors = await client.embed(["first text", "second text"])

    assert len(vectors) == 2
    assert all(len(vector) == 1024 for vector in vectors)
    assert vectors[0][0] == 0.25
    assert vectors[1][0] == 1.25

    call = api.embeddings.calls[0]
    assert call["model"] == "@cf/qwen/qwen3-embedding-0.6b"


@pytest.mark.asyncio
async def test_inputs_are_batched(embedding_settings):
    api = FakeAPIClient()
    client = CloudflareEmbeddingClient(api_client=api)

    vectors = await client.embed([
        "text one",
        "text two",
        "text three",
    ])

    assert len(vectors) == 3
    assert [
        len(call["input"]) for call in api.embeddings.calls
    ] == [2, 1]


@pytest.mark.asyncio
async def test_incorrect_dimension_is_rejected(embedding_settings):
    api = FakeAPIClient(dimensions=768)
    client = CloudflareEmbeddingClient(api_client=api)

    with pytest.raises(
        ValueError,
        match="Unexpected embedding dimension",
    ):
        await client.embed(["test text"])


@pytest.mark.asyncio
async def test_blank_input_is_rejected(embedding_settings):
    api = FakeAPIClient()
    client = CloudflareEmbeddingClient(api_client=api)

    with pytest.raises(ValueError, match="non-empty"):
        await client.embed(["valid text", "   "])

    assert api.embeddings.calls == []
