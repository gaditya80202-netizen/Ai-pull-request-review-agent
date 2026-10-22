from decimal import Decimal

import pytest

from backend.economics.pricing import (
    calculate_cost_usd,
    estimate_review_reservation_usd,
)


def test_calculates_conservative_peak_cost():
    cost = calculate_cost_usd(
        model="deepseek-flash",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
    )
    assert cost == Decimal("1.50000000")


def test_small_call_cost():
    cost = calculate_cost_usd(
        model="deepseek-flash",
        tokens_in=250,
        tokens_out=10,
    )
    assert cost == Decimal("0.00008700")


@pytest.mark.parametrize("tokens", [-1, True, 1.5, "10"])
def test_rejects_invalid_token_counts(tokens):
    with pytest.raises(ValueError):
        calculate_cost_usd(
            model="deepseek-flash",
            tokens_in=tokens,
            tokens_out=0,
        )


def test_unknown_model_fails_closed():
    with pytest.raises(ValueError, match="No verified pricing"):
        calculate_cost_usd(
            model="unknown-model",
            tokens_in=100,
            tokens_out=100,
        )


def test_review_reservation_covers_four_agents_and_retry():
    reservation = estimate_review_reservation_usd(
        model="deepseek-flash",
    )
    assert reservation > Decimal("0.0877824")
    assert reservation < Decimal("0.50")


def test_invalid_agent_count_is_rejected():
    with pytest.raises(ValueError, match="agent_count"):
        estimate_review_reservation_usd(agent_count=0)
