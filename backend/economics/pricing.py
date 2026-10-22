"""Conservative DeepSeek API cost estimates.

Rates are USD per million tokens at peak pricing. Input tokens are charged
as cache misses for estimation, even when the provider might bill less.
Recheck the official DeepSeek pricing page when updating these constants.
"""

from decimal import Decimal, ROUND_CEILING

from backend.core.settings import settings


MILLION = Decimal("1000000")
COST_QUANTUM = Decimal("0.00000001")
RESERVATION_QUANTUM = Decimal("0.000001")

# Peak rates from the official DeepSeek API pricing documentation.
# Unknown models fail closed until their pricing is configured.
PEAK_PRICES_PER_MILLION = {
    "deepseek-flash": {
        "input": Decimal("0.30"),
        "output": Decimal("1.20"),
    },
}


def _validate_tokens(value: int, field: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def calculate_cost_usd(
    *,
    model: str,
    tokens_in: int,
    tokens_out: int,
) -> Decimal:
    """Calculate conservative cost from provider-reported token usage."""

    _validate_tokens(tokens_in, "tokens_in")
    _validate_tokens(tokens_out, "tokens_out")

    prices = PEAK_PRICES_PER_MILLION.get(model)
    if prices is None:
        raise ValueError(f"No verified pricing configured for model: {model}")

    cost = (
        Decimal(tokens_in) * prices["input"]
        + Decimal(tokens_out) * prices["output"]
    ) / MILLION

    return cost.quantize(COST_QUANTUM, rounding=ROUND_CEILING)


def estimate_review_reservation_usd(
    *,
    model: str | None = None,
    agent_count: int = 4,
    safety_margin: Decimal = Decimal("1.10"),
) -> Decimal:
    """Estimate the maximum model spend for a four-agent review.

    Assumes each agent may make two calls: the initial attempt and one
    correction attempt. This estimate is valid only when the input-token
    cap is enforced before each API call.
    """

    selected_model = model or settings.deepseek_model
    if not selected_model:
        raise ValueError("DEEPSEEK_MODEL is not configured.")

    # Validate the model even when every token limit is zero.
    if selected_model not in PEAK_PRICES_PER_MILLION:
        raise ValueError(
            f"No verified pricing configured for model: {selected_model}"
        )

    if (
        not isinstance(agent_count, int)
        or isinstance(agent_count, bool)
        or agent_count < 1
    ):
        raise ValueError("agent_count must be a positive integer")

    if not isinstance(safety_margin, Decimal) or safety_margin < 1:
        raise ValueError("safety_margin must be a Decimal >= 1")

    input_limit = settings.max_context_tokens
    output_limit = settings.llm_max_output_tokens

    if input_limit < 1 or output_limit < 1:
        raise ValueError("Configured token limits must be positive")

    correction_output_limit = min(output_limit * 2, 8192)

    total_input_tokens = agent_count * 2 * input_limit
    total_output_tokens = agent_count * (
        output_limit + correction_output_limit
    )

    estimated_cost = calculate_cost_usd(
        model=selected_model,
        tokens_in=total_input_tokens,
        tokens_out=total_output_tokens,
    )

    return (estimated_cost * safety_margin).quantize(
        RESERVATION_QUANTUM,
        rounding=ROUND_CEILING,
    )
