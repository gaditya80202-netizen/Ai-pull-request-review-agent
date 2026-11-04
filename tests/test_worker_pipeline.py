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

HEAD_SHA = "a" * 40
REVIEW_ID = "11111111-1111-4111-8111-111111111111"

VALID_JOB = {
    "installation_id": 123,
    "repo": "acme/project",
    "pr_number": 4,
    "head_sha": HEAD_SHA,
    "delivery_id": "test-delivery",
}

SAMPLE_DIFF = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 Old text
+New text
"""


class FakeGitHub:
    def __init__(self, live_sha=HEAD_SHA):
        self.live_sha = live_sha

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, repo, pr_number):
        return {"head": {"sha": self.live_sha}}

    async def get_pull_request_diff(self, repo, pr_number):
        return SAMPLE_DIFF

    async def get_pull_request_files(self, repo, pr_number):
        return [{"filename": "README.md"}]


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class FakePool:
    def __init__(self):
        self.connection = FakeConnection()

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class FakeDatabase:
    def __init__(self):
        self.pool = FakePool()
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))

        if "SELECT id::text AS id, status" in query:
            return []

        if "INSERT INTO pr_review_records" in query:
            return [{"id": REVIEW_ID}]

        raise AssertionError("Unexpected database query in test: " + query)

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "OK"


class FakeGraph:
    def __init__(self, captured):
        self.captured = captured

    async def ainvoke(self, state):
        self.captured["state"] = state
        return {
            "result": {
                "status": "human_review_required",
                "findings": [],
                "requires_human_review": True,
                "auto_post_allowed": False,
                "gate_reasons": ["Test requires human review."],
            },
            "retrieval_status": "completed",
        }


def test_valid_review_job_is_normalized():
    result = worker.validate_review_job(VALID_JOB)

    assert result["repo"] == "acme/project"
    assert result["installation_id"] == 123
    assert result["pr_number"] == 4
    assert result["head_sha"] == HEAD_SHA


@pytest.mark.parametrize(
    "updates",
    [
        {"installation_id": 0},
        {"installation_id": True},
        {"repo": "bad-repository-name"},
        {"pr_number": 0},
        {"pr_number": True},
        {"head_sha": ""},
        {"head_sha": "x" * 65},
    ],
)
def test_invalid_review_job_is_rejected(updates):
    job = {**VALID_JOB, **updates}

    with pytest.raises(ValueError):
        worker.validate_review_job(job)


@pytest.mark.asyncio
async def test_installation_mismatch_is_rejected(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 999)

    with pytest.raises(ValueError, match="does not match"):
        await worker.run_review({}, VALID_JOB)


@pytest.mark.asyncio
async def test_superseded_pr_job_is_not_reviewed(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(
        worker,
        "GitHubClient",
        lambda: FakeGitHub(live_sha="b" * 40),
    )

    result = await worker.run_review(
        {"tiger_client": object()},
        VALID_JOB,
    )

    assert result["status"] == "superseded"
    assert result.get("auto_posted", True)
    assert result["live_head_sha"] == "b" * 40


@pytest.mark.asyncio
async def test_simulated_review_is_persisted_without_posting(
    monkeypatch,
):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)

    database = FakeDatabase()
    captured = {}

    monkeypatch.setattr(
        worker,
        "build_review_graph",
        lambda: FakeGraph(captured),
    )

    result = await worker.run_review(
        {"tiger_client": database},
        VALID_JOB,
    )

    assert result["status"] == "human_review_required"
    assert result["review_id"] == REVIEW_ID
    assert result["auto_posted"] is False
    assert result["retrieval_status"] == "completed"

    graph_state = captured["state"]
    assert graph_state["repo"] == "acme/project"
    assert graph_state["installation_id"] == 123
    assert graph_state["head_sha"] == HEAD_SHA
    assert graph_state["head_sha_verified"] is True
    assert graph_state["touched_paths"] == ["README.md"]

    sql_calls = database.pool.connection.calls
    assert any(
        "UPDATE pr_review_records" in query
        for query, _ in sql_calls
    )
    assert any(
        "INSERT INTO hitl_reviews" in query
        for query, _ in sql_calls
    )
    assert not any(
        "INSERT INTO finding_records" in query
        for query, _ in sql_calls
    )


@pytest.mark.asyncio
async def test_budget_denial_prevents_graph_execution(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)

    database = FakeDatabase()
    graph_called = False

    async def deny_budget(db, review_id):
        return SimpleNamespace(
            allowed=False,
            reason="daily_budget_exceeded",
            reserved_usd=0,
        )

    def forbidden_graph():
        nonlocal graph_called
        graph_called = True
        raise AssertionError("Graph must not run when budget is denied")

    monkeypatch.setattr(worker, "reserve_review_budget", deny_budget)
    monkeypatch.setattr(worker, "build_review_graph", forbidden_graph)

    result = await worker.run_review(
        {"tiger_client": database},
        VALID_JOB,
    )

    assert result["status"] == "budget_exceeded"
    assert result["auto_posted"] is False
    assert graph_called is False


@pytest.mark.asyncio
async def test_managed_webhook_is_claimed_and_completed(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)
    monkeypatch.setattr(worker, "GitHubClient", FakeGitHub)

    database = FakeDatabase()
    captured = {}
    inbox_events = []

    async def fake_claim(db, delivery_id):
        inbox_events.append(("claim", delivery_id))
        return {"delivery_id": delivery_id, "attempts": 1}

    async def fake_mark(db, delivery_id, *, status, last_error=None):
        inbox_events.append((status, delivery_id, last_error))

    monkeypatch.setattr(worker, "claim_delivery", fake_claim)
    monkeypatch.setattr(worker, "mark_delivery", fake_mark)
    monkeypatch.setattr(
        worker, "build_review_graph", lambda: FakeGraph(captured)
    )

    job = {**VALID_JOB, "inbox_managed": True}
    result = await worker.run_review({"tiger_client": database}, job)

    assert result["auto_posted"] is False
    assert inbox_events[0] == ("claim", VALID_JOB["delivery_id"])
    assert inbox_events[1][0] == "completed"


@pytest.mark.asyncio
async def test_unclaimable_delivery_does_not_run_graph(monkeypatch):
    monkeypatch.setattr(worker.settings, "github_installation_id", 123)

    database = FakeDatabase()
    graph_called = False

    async def fake_claim(db, delivery_id):
        return None

    def forbidden_graph():
        nonlocal graph_called
        graph_called = True
        raise AssertionError("Graph must not run")

    monkeypatch.setattr(worker, "claim_delivery", fake_claim)
    monkeypatch.setattr(worker, "build_review_graph", forbidden_graph)

    result = await worker.run_review(
        {"tiger_client": database},
        {**VALID_JOB, "inbox_managed": True},
    )

    assert result["status"] == "inbox_not_claimed"
    assert graph_called is False
