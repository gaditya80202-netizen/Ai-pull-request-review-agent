from contextlib import asynccontextmanager

import pytest


@pytest.fixture(autouse=True)
def mock_worker_budget(monkeypatch):
    from decimal import Decimal
    from types import SimpleNamespace

    async def fake_reserve(database, review_id):
        return SimpleNamespace(
            allowed=True,
            reason="reserved",
            reserved_usd=Decimal("0.100000"),
        )

    async def fake_settle(database, review_id, actual_cost_usd):
        return True

    monkeypatch.setattr(worker, "reserve_review_budget", fake_reserve)
    monkeypatch.setattr(worker, "settle_review_budget", fake_settle)


from backend.job_queue import arq_worker as worker

REVIEW_ID = "11111111-1111-4111-8111-111111111111"
HEAD_SHA = "a" * 40

JOB = {
    "installation_id": 123,
    "repo": "acme/project",
    "pr_number": 4,
    "head_sha": HEAD_SHA,
    "delivery_id": "delivery-test",
}

DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 Old text
+New text
"""


class FakeGitHub:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, repo, pr_number):
        return {"head": {"sha": HEAD_SHA}}

    async def get_pull_request_diff(self, repo, pr_number):
        return DIFF

    async def get_pull_request_files(self, repo, pr_number):
        return [{"filename": "README.md"}]


class FakeDatabase:
    def __init__(self, status, claim_succeeds):
        self.status = status
        self.claim_succeeds = claim_succeeds
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))

        if "SELECT id::text AS id, status, updated_at" in query:
            return [{
                "id": REVIEW_ID,
                "status": self.status,
                "updated_at": None,
            }]

        if "updated_at < now() - INTERVAL '15 minutes'" in query:
            return [{"id": REVIEW_ID}] if self.claim_succeeds else []

        if "status = 'failed'" in query:
            return [{"id": REVIEW_ID}] if self.claim_succeeds else []

        raise AssertionError("Unexpected query: " + query)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


class FakeGraph:
    async def ainvoke(self, state):
        return {
            "result": {
                "status": "human_review_required",
                "findings": [],
                "requires_human_review": True,
                "auto_post_allowed": False,
                "gate_reasons": ["Approval required in test."],
            },
            "retrieval_status": "completed",
        }


@pytest.fixture
def worker_mocks(monkeypatch):
    monkeypatch.setattr(
        worker.settings, "github_installation_id", 123
    )
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(worker, "build_review_graph", FakeGraph)

    async def fake_persist(
    database, *, review_id, installation_id, review_result
):
        assert review_id == REVIEW_ID
        return "human_review_required"

    monkeypatch.setattr(worker, "_persist_review", fake_persist)


@pytest.mark.asyncio
async def test_fresh_running_review_is_not_restarted(worker_mocks):
    db = FakeDatabase(status="running", claim_succeeds=False)

    result = await worker.run_review({"tiger_client": db}, JOB)

    assert result["status"] == "already_running"
    assert any(
        "updated_at < now() - INTERVAL '15 minutes'" in query
        for query, _ in db.fetch_calls
    )


@pytest.mark.asyncio
async def test_stale_running_review_can_be_reclaimed(worker_mocks):
    db = FakeDatabase(status="running", claim_succeeds=True)

    result = await worker.run_review({"tiger_client": db}, JOB)

    assert result["status"] == "human_review_required"
    assert result["review_id"] == REVIEW_ID
    assert result["auto_posted"] is False


@pytest.mark.asyncio
async def test_failed_review_can_be_reclaimed(worker_mocks):
    db = FakeDatabase(status="failed", claim_succeeds=True)

    result = await worker.run_review({"tiger_client": db}, JOB)

    assert result["status"] == "human_review_required"
    assert result["review_id"] == REVIEW_ID
    assert any(
        "status = 'failed'" in query
        for query, _ in db.fetch_calls
    )


class FakeConnection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class PersistDatabase:
    def __init__(self):
        self.pool = FakePool()


@pytest.mark.asyncio
async def test_pending_approval_uses_idempotent_upsert():
    db = PersistDatabase()

    await worker._persist_review(
        db,
        review_id=REVIEW_ID,
        installation_id=123,
        review_result={
            "status": "human_review_required",
            "findings": [],
            "requires_human_review": True,
            "auto_post_allowed": False,
            "gate_reasons": ["Approval required."],
        },
    )

    queries = [
        query for query, _ in db.pool.connection.calls
    ]
    update_query = next(
        query for query in queries
        if "UPDATE pr_review_records" in query
    )
    hitl_query = next(
        query for query in queries
        if "INSERT INTO hitl_reviews" in query
    )

    assert "updated_at = now()" in update_query
    assert "ON CONFLICT (review_id) WHERE status = 'pending'" in hitl_query
    assert "DO UPDATE SET reason = EXCLUDED.reason" in hitl_query
