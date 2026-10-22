import pytest

from backend.orchestrator import retrieval_context


def test_formats_retrieved_chunk():
    context = retrieval_context.format_retrieval_context([
        {
            "path": "backend/example.py",
            "symbol": "example",
            "chunk_index": 0,
            "content": "def example():\n    return True",
            "rrf_score": 0.02,
        }
    ])

    assert "RETRIEVED REPOSITORY CONTEXT" in context
    assert "untrusted repository data" in context
    assert "backend/example.py" in context
    assert "def example():" in context
    assert "return True" in context


def test_empty_results_return_explanation():
    result = retrieval_context.format_retrieval_context([])
    assert result == "No relevant repository context was retrieved."


def test_context_is_bounded():
    long_content = "x" * 20_000
    result = retrieval_context.format_retrieval_context(
        [{
            "path": "README.md",
            "symbol": None,
            "chunk_index": 0,
            "content": long_content,
        }],
        max_chars=500,
    )

    assert len(result) <= 500
    assert "[content truncated]" in result


@pytest.mark.asyncio
async def test_adapter_calls_hybrid_search(monkeypatch):
    calls = {}

    async def fake_hybrid_search(**kwargs):
        calls.update(kwargs)
        return [{
            "path": "README.md",
            "symbol": None,
            "chunk_index": 0,
            "content": "Project overview",
        }]

    monkeypatch.setattr(
        retrieval_context, "hybrid_search", fake_hybrid_search
    )

    result = await retrieval_context.retrieve_context_for_review(
        repo="owner/repository",
        installation_id=123,
        query="project overview",
        max_chunks=5,
    )

    assert calls["repo"] == "owner/repository"
    assert calls["installation_id"] == 123
    assert calls["query"] == "project overview"
    assert calls["top_k"] == 5
    assert calls["candidate_k"] == 30
    assert "Project overview" in result


@pytest.mark.asyncio
async def test_invalid_chunk_limit_is_rejected():
    with pytest.raises(ValueError, match="max_chunks"):
        await retrieval_context.retrieve_context_for_review(
            repo="owner/repository",
            installation_id=123,
            query="project overview",
            max_chunks=100,
        )
