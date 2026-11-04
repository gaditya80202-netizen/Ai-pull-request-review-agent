from types import SimpleNamespace

import pytest

from backend.job_queue import arq_worker as worker

DELIVERY_ID = "11111111-1111-4111-8111-111111111111"
SHA = "a" * 40

JOB = {
    "delivery_id": DELIVERY_ID,
    "installation_id": 123,
    "repo": "owner/repository",
    "ref": "refs/heads/main",
    "head_sha": SHA,
    "inbox_managed": True,
}


class FakeGitHub:
    def __init__(self, branch="main"):
        self.branch = branch

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_repository(self, repo):
        return {"default_branch": self.branch}


@pytest.fixture
def setup_reindex(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)

    events = []

    async def claim(db, delivery_id):
        events.append(("claim", delivery_id))
        return {"attempts": 1}

    async def mark(db, delivery_id, *, status, last_error=None):
        events.append((status, delivery_id, last_error))

    async def fake_index(repo, *, installation_id):
        events.append(("index", repo, installation_id))
        return SimpleNamespace(
            branch="main",
            commit_sha=SHA,
            files_seen=2,
            files_eligible=2,
            files_indexed=1,
            files_skipped_unchanged=1,
            files_filtered=0,
            chunks_written=1,
        )

    monkeypatch.setattr(worker, "claim_delivery", claim)
    monkeypatch.setattr(worker, "mark_delivery", mark)
    monkeypatch.setattr(worker, "index_repository", fake_index)
    return events


@pytest.mark.asyncio
async def test_default_branch_is_reindexed(setup_reindex):
    result = await worker.reindex_repo(
        {"tiger_client": object()},
        JOB,
    )

    assert result["status"] == "reindexed"
    assert result["branch"] == "main"
    assert result["files_indexed"] == 1
    assert result["auto_posted"] is False
    assert setup_reindex[0] == ("claim", DELIVERY_ID)
    assert setup_reindex[1] == ("index", "owner/repository", 123)
    assert setup_reindex[2][0] == "completed"


@pytest.mark.asyncio
async def test_push_no_longer_targets_default_branch(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(
        worker, "GitHubClient", lambda: FakeGitHub(branch="stable")
    )

    events = []

    async def claim(db, delivery_id):
        return {"attempts": 1}

    async def mark(db, delivery_id, *, status, last_error=None):
        events.append((status, last_error))

    async def forbidden_index(*args, **kwargs):
        raise AssertionError("Must not index a non-default branch")

    monkeypatch.setattr(worker, "claim_delivery", claim)
    monkeypatch.setattr(worker, "mark_delivery", mark)
    monkeypatch.setattr(worker, "index_repository", forbidden_index)

    result = await worker.reindex_repo(
        {"tiger_client": object()},
        JOB,
    )

    assert result["status"] == "ignored_not_default_branch"
    assert events[0][0] == "ignored"


@pytest.mark.asyncio
async def test_unclaimable_reindex_delivery_does_not_index(
    monkeypatch,
):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)

    async def claim(db, delivery_id):
        return None

    async def forbidden_index(*args, **kwargs):
        raise AssertionError("Unclaimed deliveries must not run")

    monkeypatch.setattr(worker, "claim_delivery", claim)
    monkeypatch.setattr(worker, "index_repository", forbidden_index)

    result = await worker.reindex_repo(
        {"tiger_client": object()},
        JOB,
    )

    assert result["status"] == "inbox_not_claimed"


@pytest.mark.asyncio
async def test_installation_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 999)

    with pytest.raises(ValueError, match="does not match"):
        await worker.reindex_repo({"tiger_client": object()}, JOB)


@pytest.mark.asyncio
async def test_invalid_commit_sha_is_rejected(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)

    with pytest.raises(ValueError, match="commit SHA"):
        await worker.reindex_repo(
            {"tiger_client": object()},
            {**JOB, "head_sha": "not-a-sha"},
        )
