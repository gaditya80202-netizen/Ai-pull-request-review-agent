import pytest

from backend.memory import retrieval


class FakeDatabase:
    def __init__(self, mismatched_files=None, search_rows=None):
        self.mismatched_files = mismatched_files or []
        self.search_rows = search_rows or []
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))

        if "SELECT path, embedding_model" in query:
            return self.mismatched_files

        return self.search_rows


class FakeEmbedder:
    def __init__(self, vectors=None):
        self.vectors = vectors or [[0.1, 0.2, 0.3]]
        self.queries = []

    async def embed(self, texts):
        self.queries.append(texts)
        return self.vectors


@pytest.fixture
def configured_settings(monkeypatch):
    monkeypatch.setattr(retrieval.settings, "embed_dim", 3)
    monkeypatch.setattr(
        retrieval.settings,
        "cloudflare_embedding_model",
        "test-embedding-model",
    )


@pytest.mark.asyncio
async def test_hybrid_search_returns_ranked_results(
    configured_settings,
):
    expected = {
        "path": "README.md",
        "content": "Project overview",
        "rrf_score": 0.03,
    }
    db = FakeDatabase(search_rows=[expected])
    embedder = FakeEmbedder()

    results = await retrieval.hybrid_search(
        repo="owner/repository",
        installation_id=123,
        query="project overview",
        database=db,
        embedding_client=embedder,
    )

    assert results == [expected]
    assert embedder.queries == [["project overview"]]
    assert len(db.calls) == 2
    assert "FULL OUTER JOIN" in retrieval.HYBRID_SEARCH_SQL
    assert "c.installation_id = $4" in retrieval.HYBRID_SEARCH_SQL


@pytest.mark.asyncio
async def test_model_mismatch_stops_search(configured_settings):
    db = FakeDatabase(
        mismatched_files=[
            {
                "path": "README.md",
                "embedding_model": "old-embedding-model",
            }
        ]
    )
    embedder = FakeEmbedder()

    with pytest.raises(RuntimeError, match="different model"):
        await retrieval.hybrid_search(
            repo="owner/repository",
            installation_id=123,
            query="project overview",
            database=db,
            embedding_client=embedder,
        )

    assert embedder.queries == []
    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_wrong_embedding_dimensions_stop_search(
    configured_settings,
):
    db = FakeDatabase()
    embedder = FakeEmbedder(vectors=[[0.1, 0.2]])

    with pytest.raises(RuntimeError, match="dimensions"):
        await retrieval.hybrid_search(
            repo="owner/repository",
            installation_id=123,
            query="project overview",
            database=db,
            embedding_client=embedder,
        )

    assert len(db.calls) == 1


@pytest.mark.asyncio
async def test_empty_query_is_rejected_before_io():
    db = FakeDatabase()
    embedder = FakeEmbedder()

    with pytest.raises(ValueError, match="query must not be empty"):
        await retrieval.hybrid_search(
            repo="owner/repository",
            installation_id=123,
            query="   ",
            database=db,
            embedding_client=embedder,
        )

    assert db.calls == []
    assert embedder.queries == []
