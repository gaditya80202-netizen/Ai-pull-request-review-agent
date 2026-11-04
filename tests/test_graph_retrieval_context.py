import pytest

from backend.orchestrator import graph

SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,1 +1,2 @@
 old_function()
+new_feature()
"""


@pytest.mark.asyncio
async def test_repository_context_is_added(monkeypatch):
    captured = {}

    async def fake_retrieval(**kwargs):
        captured.update(kwargs)
        return "Relevant repository implementation."

    monkeypatch.setattr(
        graph, "retrieve_context_for_review", fake_retrieval
    )

    result = await graph.retrieve_repository_context({
        "repo": "owner/repository",
        "installation_id": 123,
        "diff": SAMPLE_DIFF,
        "context": "Previously supplied context.",
    })

    assert result["retrieval_status"] == "completed"
    assert "Previously supplied context." in result["context"]
    assert "Relevant repository implementation." in result["context"]
    assert captured["repo"] == "owner/repository"
    assert captured["installation_id"] == 123
    assert "src/app.py" in captured["query"]
    assert "new_feature()" in captured["query"]


@pytest.mark.asyncio
async def test_missing_identity_keeps_diff_only_mode():
    result = await graph.retrieve_repository_context({
        "diff": SAMPLE_DIFF,
        "context": "Existing context.",
    })

    assert result["context"] == "Existing context."
    assert result["retrieval_status"] == "skipped_missing_identity"


@pytest.mark.asyncio
async def test_partial_identity_is_rejected():
    with pytest.raises(ValueError, match="repo and installation_id"):
        await graph.retrieve_repository_context({
            "repo": "owner/repository",
            "diff": SAMPLE_DIFF,
        })


@pytest.mark.asyncio
async def test_retrieval_failure_is_not_silently_ignored(monkeypatch):
    async def fake_failure(**kwargs):
        raise RuntimeError("test retrieval failure")

    monkeypatch.setattr(
        graph, "retrieve_context_for_review", fake_failure
    )

    with pytest.raises(RuntimeError, match="test retrieval failure"):
        await graph.retrieve_repository_context({
            "repo": "owner/repository",
            "installation_id": 123,
            "diff": SAMPLE_DIFF,
        })
