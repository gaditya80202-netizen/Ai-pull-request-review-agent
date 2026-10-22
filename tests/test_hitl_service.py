from contextlib import asynccontextmanager

import pytest

from backend.hitl.service import (
    decide_review,
    record_finding_feedback,
)


HITL_ID = "11111111-1111-4111-8111-111111111111"
REVIEW_ID = "22222222-2222-4222-8222-222222222222"
FINDING_ID = "33333333-3333-4333-8333-333333333333"


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, hitl_status="pending", finding_exists=True):
        self.hitl_status = hitl_status
        self.finding_exists = finding_exists
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))

        if "JOIN pr_review_records" in query:
            if self.hitl_status is None:
                return None
            return {
                "hitl_review_id": HITL_ID,
                "review_id": REVIEW_ID,
                "hitl_status": self.hitl_status,
                "review_status": "human_review_required",
            }

        if "UPDATE hitl_reviews" in query:
            if self.hitl_status != "pending":
                return None
            self.hitl_status = args[1]
            return {"id": HITL_ID}

        if "FROM finding_records" in query:
            if not self.finding_exists:
                return None
            return {"id": FINDING_ID}

        raise AssertionError("Unexpected query: " + query)

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


class FakeDatabase:
    def __init__(self, connection):
        self.pool = FakePool(connection)


@pytest.mark.asyncio
async def test_approval_records_decision_without_publication():
    conn = FakeConnection()
    result = await decide_review(
        FakeDatabase(conn),
        hitl_review_id=HITL_ID,
        decision="approved",
        reviewer="reviewer-1",
    )

    assert result["decision"] == "approved"
    assert result["review_status"] == "human_approved"
    assert result["publication_allowed"] is False
    assert any(
        "decided_at = now()" in query for query, _ in conn.calls
    )
    assert any(
        "SET status = $2" in query
        and "pr_review_records" in query
        for query, _ in conn.calls
    )


@pytest.mark.asyncio
async def test_rejection_records_decision():
    conn = FakeConnection()
    result = await decide_review(
        FakeDatabase(conn),
        hitl_review_id=HITL_ID,
        decision="rejected",
        reviewer="reviewer-1",
    )

    assert result["decision"] == "rejected"
    assert result["review_status"] == "human_rejected"
    assert result["publication_allowed"] is False


@pytest.mark.asyncio
async def test_non_pending_request_cannot_be_decided_again():
    conn = FakeConnection(hitl_status="approved")

    with pytest.raises(ValueError, match="already decided"):
        await decide_review(
            FakeDatabase(conn),
            hitl_review_id=HITL_ID,
            decision="rejected",
            reviewer="reviewer-1",
        )


@pytest.mark.asyncio
async def test_missing_review_request_is_reported():
    conn = FakeConnection(hitl_status=None)

    with pytest.raises(LookupError, match="not found"):
        await decide_review(
            FakeDatabase(conn),
            hitl_review_id=HITL_ID,
            decision="approved",
            reviewer="reviewer-1",
        )


@pytest.mark.asyncio
async def test_invalid_decision_is_rejected():
    with pytest.raises(ValueError, match="decision must"):
        await decide_review(
            FakeDatabase(FakeConnection()),
            hitl_review_id=HITL_ID,
            decision="publish",
            reviewer="reviewer-1",
        )


@pytest.mark.asyncio
async def test_finding_feedback_is_persisted():
    conn = FakeConnection()
    result = await record_finding_feedback(
        FakeDatabase(conn),
        finding_id=FINDING_ID,
        verdict="false_positive",
        reviewer="reviewer-1",
        comment="This is expected behavior.",
    )

    assert result["recorded"] is True
    assert result["verdict"] == "false_positive"

    query, args = next(
        (query, args)
        for query, args in conn.calls
        if "INSERT INTO hitl_feedback" in query
    )
    assert args == (
        FINDING_ID,
        "false_positive",
        "This is expected behavior.",
        "reviewer-1",
    )


@pytest.mark.asyncio
async def test_feedback_rejects_unknown_finding():
    conn = FakeConnection(finding_exists=False)

    with pytest.raises(LookupError, match="Finding was not found"):
        await record_finding_feedback(
            FakeDatabase(conn),
            finding_id=FINDING_ID,
            verdict="true_positive",
            reviewer="reviewer-1",
        )


@pytest.mark.asyncio
async def test_feedback_validates_verdict_and_comment_size():
    with pytest.raises(ValueError, match="verdict must be one of"):
        await record_finding_feedback(
            FakeDatabase(FakeConnection()),
            finding_id=FINDING_ID,
            verdict="maybe",
            reviewer="reviewer-1",
        )

    with pytest.raises(ValueError, match="at most 2000"):
        await record_finding_feedback(
            FakeDatabase(FakeConnection()),
            finding_id=FINDING_ID,
            verdict="true_positive",
            reviewer="reviewer-1",
            comment="x" * 2001,
        )
