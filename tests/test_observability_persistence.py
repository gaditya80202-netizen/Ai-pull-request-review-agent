import json
from decimal import Decimal

import pytest

from backend.observability.persistence import persist_agent_events


REVIEW_ID = "11111111-1111-4111-8111-111111111111"


class FakeConnection:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return "INSERT 0 1"


def complete_status():
    return {
        name: {
            "status": "success",
            "model": "deepseek-flash",
            "prompt_version": f"{name}_v1",
            "tokens_in": 100,
            "tokens_out": 20,
            "latency_ms": 125,
            "accepted_count": 1,
            "rejected_count": 0,
        }
        for name in ("security", "quality", "tests", "docs")
    }


@pytest.mark.asyncio
async def test_persists_one_event_for_each_agent():
    conn = FakeConnection()

    written = await persist_agent_events(
        conn,
        installation_id=123,
        review_id=REVIEW_ID,
        agent_status=complete_status(),
        findings=[],
    )

    assert written == 4
    assert len(conn.calls) == 4

    agents = [args[2] for _, args in conn.calls]
    assert agents == ["security", "quality", "tests", "docs"]

    for query, args in conn.calls:
        assert "INSERT INTO agent_events" in query
        assert args[5] == "agent_run"
        assert args[6] == "deepseek-flash"
        assert args[8] == 100
        assert args[9] == 20
        assert args[10] == Decimal("0.00005400")
        payload = json.loads(args[14])
        assert payload["cost_basis"] == "estimated_from_provider_tokens"


@pytest.mark.asyncio
async def test_missing_usage_is_recorded_with_null_cost():
    conn = FakeConnection()
    statuses = {
        "security": {
            "status": "timeout",
            "error_type": "TimeoutError",
        }
    }

    await persist_agent_events(
        conn,
        installation_id=123,
        review_id=REVIEW_ID,
        agent_status=statuses,
        findings=[],
    )

    security_args = conn.calls[0][1]
    assert security_args[2] == "security"
    assert security_args[10] is None
    assert security_args[12] == "timeout"

    payload = json.loads(security_args[14])
    assert payload["usage_available"] is False
    assert payload["cost_basis"] == "unavailable"


@pytest.mark.asyncio
async def test_unknown_model_has_no_invented_cost():
    conn = FakeConnection()
    statuses = {
        "quality": {
            "status": "success",
            "model": "unknown-model",
            "tokens_in": 100,
            "tokens_out": 30,
        }
    }

    await persist_agent_events(
        conn,
        installation_id=123,
        review_id=REVIEW_ID,
        agent_status=statuses,
        findings=[],
    )

    args = next(args for _, args in conn.calls if args[2] == "quality")
    assert args[10] is None

    payload = json.loads(args[14])
    assert payload["cost_basis"] == "unknown_model_pricing"


@pytest.mark.asyncio
async def test_confidence_is_calculated_per_agent():
    conn = FakeConnection()

    statuses = complete_status()
    findings = [
        {"agent_type": "quality", "confidence": 0.8},
        {"agent_type": "quality", "confidence": 1.0},
        {"agent_type": "security", "confidence": 0.6},
    ]

    await persist_agent_events(
        conn,
        installation_id=123,
        review_id=REVIEW_ID,
        agent_status=statuses,
        findings=findings,
    )

    by_agent = {args[2]: args for _, args in conn.calls}
    assert float(by_agent["quality"][13]) == pytest.approx(0.9)
    assert float(by_agent["security"][13]) == pytest.approx(0.6)
    assert by_agent["tests"][13] is None


@pytest.mark.asyncio
async def test_invalid_identity_is_rejected():
    conn = FakeConnection()

    with pytest.raises(ValueError, match="installation_id"):
        await persist_agent_events(
            conn,
            installation_id=0,
            review_id=REVIEW_ID,
            agent_status=complete_status(),
            findings=[],
        )

    with pytest.raises(ValueError, match="review_id"):
        await persist_agent_events(
            conn,
            installation_id=123,
            review_id="not-a-uuid",
            agent_status=complete_status(),
            findings=[],
        )

    assert conn.calls == []
