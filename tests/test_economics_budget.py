from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal

import pytest

from backend.economics.budget import (
    reserve_review_budget,
    settle_review_budget,
    release_review_budget,
)


REVIEW_ID = "11111111-1111-4111-8111-111111111111"
TODAY = date(2026, 10, 23)


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, reservation=None, daily=None):
        self.reservation = reservation
        self.daily = daily or {
            "reserved_usd": Decimal("0"),
            "actual_usd": Decimal("0"),
        }
        self.calls = []

    def transaction(self):
        return FakeTransaction()

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        if "AT TIME ZONE 'UTC'" in query:
            return TODAY
        raise AssertionError("Unexpected fetchval query")

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))

        if "FROM llm_budget_reservations" in query:
            return self.reservation

        if "FROM daily_budget_usage" in query:
            return self.daily

        raise AssertionError("Unexpected fetchrow query")


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
async def test_reserve_succeeds_and_records_reservation(monkeypatch):
    from backend.core.settings import settings

    monkeypatch.setattr(settings, "daily_budget_usd", 0.50)
    conn = FakeConnection()
    result = await reserve_review_budget(
        FakeDatabase(conn), REVIEW_ID, Decimal("0.10")
    )

    assert result.allowed is True
    assert result.reason == "reserved"
    assert result.reserved_usd == Decimal("0.100000")
    assert any(
        "INSERT INTO llm_budget_reservations" in query
        for query, _ in conn.calls
    )


@pytest.mark.asyncio
async def test_reservation_fails_when_daily_budget_would_be_exceeded(
    monkeypatch,
):
    from backend.core.settings import settings

    monkeypatch.setattr(settings, "daily_budget_usd", 0.50)
    conn = FakeConnection(
        daily={
            "reserved_usd": Decimal("0.20"),
            "actual_usd": Decimal("0.25"),
        }
    )

    result = await reserve_review_budget(
        FakeDatabase(conn), REVIEW_ID, Decimal("0.10")
    )

    assert result.allowed is False
    assert result.reason == "daily_budget_exceeded"
    assert not any(
        "INSERT INTO llm_budget_reservations" in query
        for query, _ in conn.calls
    )


@pytest.mark.asyncio
async def test_existing_same_day_reservation_is_idempotent():
    conn = FakeConnection(reservation={
        "budget_day": TODAY,
        "reserved_usd": Decimal("0.10"),
        "actual_cost_usd": Decimal("0"),
        "status": "reserved",
    })

    result = await reserve_review_budget(
        FakeDatabase(conn), REVIEW_ID, Decimal("0.15")
    )

    assert result.allowed is True
    assert result.reason == "already_reserved"
    assert result.reserved_usd == Decimal("0.10")


@pytest.mark.asyncio
async def test_settlement_is_idempotent_after_first_settlement():
    conn = FakeConnection(reservation={
        "budget_day": TODAY,
        "reserved_usd": Decimal("0.10"),
        "status": "settled",
    })

    result = await settle_review_budget(
        FakeDatabase(conn), REVIEW_ID, Decimal("0.02")
    )

    assert result is False
    assert not any(
        "SET actual_cost_usd" in query
        for query, _ in conn.calls
    )


@pytest.mark.asyncio
async def test_release_updates_reservation_state():
    conn = FakeConnection(reservation={
        "budget_day": TODAY,
        "reserved_usd": Decimal("0.10"),
        "status": "reserved",
    })

    result = await release_review_budget(FakeDatabase(conn), REVIEW_ID)

    assert result is True
    assert any(
        "SET status = 'released'" in query
        for query, _ in conn.calls
    )


@pytest.mark.asyncio
async def test_releasing_settled_reservation_is_noop():
    conn = FakeConnection(reservation={
        "budget_day": TODAY,
        "reserved_usd": Decimal("0.10"),
        "status": "settled",
    })

    result = await release_review_budget(FakeDatabase(conn), REVIEW_ID)

    assert result is False
