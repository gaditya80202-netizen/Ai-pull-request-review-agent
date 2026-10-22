import json

import pytest

from backend.webhook_receiver import inbox


class FakeDatabase:
    def __init__(self, fetch_results=None):
        self.fetch_results = list(fetch_results or [])
        self.fetch_calls = []
        self.execute_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "UPDATE 1"


@pytest.mark.asyncio
async def test_record_delivery_inserts_once():
    db = FakeDatabase(fetch_results=[[
        {
            "delivery_id": "delivery-1",
            "status": "pending",
            "attempts": 0,
            "last_attempt_at": None,
        }
    ]])

    result = await inbox.record_delivery(
        db,
        delivery_id="delivery-1",
        event_name="pull_request",
        action="opened",
        installation_id=123,
        repo="owner/repository",
        payload={"repo": "owner/repository", "pr_number": 1},
    )

    assert result["created"] is True
    query, args = db.fetch_calls[0]
    assert "ON CONFLICT (delivery_id) DO NOTHING" in query
    assert json.loads(args[-1])["pr_number"] == 1


@pytest.mark.asyncio
async def test_duplicate_delivery_returns_existing_state():
    db = FakeDatabase(fetch_results=[
        [],
        [{
            "delivery_id": "delivery-1",
            "status": "completed",
            "attempts": 1,
            "last_attempt_at": None,
        }],
    ])

    result = await inbox.record_delivery(
        db,
        delivery_id="delivery-1",
        event_name="pull_request",
        action="opened",
        installation_id=123,
        repo="owner/repository",
        payload={"pr_number": 1},
    )

    assert result["created"] is False
    assert result["status"] == "completed"
    assert len(db.fetch_calls) == 2


@pytest.mark.asyncio
async def test_claim_uses_atomic_status_and_attempt_guard():
    db = FakeDatabase(fetch_results=[[
        {
            "delivery_id": "delivery-1",
            "event_name": "pull_request",
            "status": "processing",
            "attempts": 1,
        }
    ]])

    result = await inbox.claim_delivery(db, "delivery-1")

    assert result["status"] == "processing"
    query, args = db.fetch_calls[0]
    assert "attempts = attempts + 1" in query
    assert "status IN ('pending', 'failed')" in query
    assert args[1] == inbox.MAX_DELIVERY_ATTEMPTS


@pytest.mark.asyncio
async def test_unclaimable_delivery_returns_none():
    db = FakeDatabase(fetch_results=[[]])

    result = await inbox.claim_delivery(db, "delivery-1")

    assert result is None


@pytest.mark.asyncio
async def test_mark_delivery_rejects_unknown_status():
    db = FakeDatabase()

    with pytest.raises(ValueError, match="Unsupported"):
        await inbox.mark_delivery(
            db, "delivery-1", status="mystery"
        )

    assert db.execute_calls == []


@pytest.mark.asyncio
async def test_mark_delivery_limits_error_text():
    db = FakeDatabase()

    await inbox.mark_delivery(
        db,
        "delivery-1",
        status="failed",
        last_error="E" * 300,
    )

    _, args = db.execute_calls[0]
    assert args[1] == "failed"
    assert len(args[2]) == 100


@pytest.mark.asyncio
async def test_retryable_delivery_query_is_bounded():
    db = FakeDatabase(fetch_results=[[{
        "delivery_id": "delivery-1",
        "status": "pending",
        "attempts": 0,
        "last_attempt_at": None,
    }]])

    rows = await inbox.list_retryable_deliveries(db, limit=20)

    assert len(rows) == 1
    assert db.fetch_calls[0][1][-1] == 20

    with pytest.raises(ValueError):
        await inbox.list_retryable_deliveries(db, limit=1000)
