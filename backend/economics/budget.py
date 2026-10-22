"""Atomic daily budget reservations and idempotent settlement."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING
from uuid import UUID
from typing import Any

from backend.core.settings import settings
from backend.economics.pricing import estimate_review_reservation_usd


MONEY_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reason: str
    reserved_usd: Decimal
    budget_day: date | None


def _review_id(value: str | UUID) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("review_id must be a valid UUID") from None


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative monetary amount")

    try:
        amount = Decimal(str(value))
    except Exception:
        raise ValueError(
            f"{field} must be a non-negative monetary amount"
        ) from None

    if not amount.is_finite() or amount < 0:
        raise ValueError(
            f"{field} must be a non-negative monetary amount"
        )

    return amount.quantize(MONEY_QUANTUM, rounding=ROUND_CEILING)


def _pool(database):
    pool = getattr(database, "pool", None)
    if pool is None:
        raise RuntimeError("Database connection pool is unavailable")
    return pool


async def reserve_review_budget(
    database,
    review_id: str | UUID,
    amount_usd: Decimal | None = None,
) -> BudgetReservation:
    """Reserve the estimated cost, enforcing the UTC daily budget atomically."""

    review_id = _review_id(review_id)
    amount = (
        _money(amount_usd, "amount_usd")
        if amount_usd is not None
        else estimate_review_reservation_usd()
    )
    limit = _money(settings.daily_budget_usd, "daily_budget_usd")

    if amount <= 0:
        raise ValueError("Reservation amount must be greater than zero")
    if limit <= 0:
        raise ValueError("Daily budget must be greater than zero")

    async with _pool(database).acquire() as connection:
        async with connection.transaction():
            # Serialize attempts to reserve for the same review.
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                review_id,
            )

            today = await connection.fetchval(
                "SELECT (now() AT TIME ZONE 'UTC')::date"
            )

            current = await connection.fetchrow(
                """
                SELECT budget_day, reserved_usd, actual_cost_usd, status
                FROM llm_budget_reservations
                WHERE review_id = $1::uuid
                FOR UPDATE
                """,
                review_id,
            )

            if current and current["status"] == "settled":
                return BudgetReservation(
                    False, "already_settled",
                    Decimal(str(current["reserved_usd"])),
                    current["budget_day"],
                )

            if current and current["status"] == "reserved":
                if current["budget_day"] == today:
                    return BudgetReservation(
                        True, "already_reserved",
                        Decimal(str(current["reserved_usd"])),
                        today,
                    )

                # Move a still-open reservation to the current UTC day.
                await connection.execute(
                    """
                    UPDATE daily_budget_usage
                    SET reserved_usd =
                        GREATEST(reserved_usd - $2, 0)
                    WHERE budget_day = $1
                    """,
                    current["budget_day"],
                    current["reserved_usd"],
                )

            await connection.execute(
                """
                INSERT INTO daily_budget_usage (
                    budget_day, reserved_usd, actual_usd
                )
                VALUES ($1, 0, 0)
                ON CONFLICT (budget_day) DO NOTHING
                """,
                today,
            )

            daily = await connection.fetchrow(
                """
                SELECT reserved_usd, actual_usd
                FROM daily_budget_usage
                WHERE budget_day = $1
                FOR UPDATE
                """,
                today,
            )

            if daily is None:
                raise RuntimeError("Could not lock the daily budget row")

            reserved = Decimal(str(daily["reserved_usd"]))
            actual = Decimal(str(daily["actual_usd"]))

            if actual + reserved + amount > limit:
                if current and current["status"] == "reserved":
                    await connection.execute(
                        """
                        UPDATE llm_budget_reservations
                        SET status = 'released',
                            reserved_usd = 0,
                            updated_at = now()
                        WHERE review_id = $1::uuid
                        """,
                        review_id,
                    )

                return BudgetReservation(
                    False, "daily_budget_exceeded", amount, today
                )

            await connection.execute(
                """
                UPDATE daily_budget_usage
                SET reserved_usd = reserved_usd + $2
                WHERE budget_day = $1
                """,
                today,
                amount,
            )

            if current:
                await connection.execute(
                    """
                    UPDATE llm_budget_reservations
                    SET budget_day = $2,
                        reserved_usd = $3,
                        actual_cost_usd = 0,
                        status = 'reserved',
                        updated_at = now(),
                        settled_at = NULL
                    WHERE review_id = $1::uuid
                    """,
                    review_id,
                    today,
                    amount,
                )
            else:
                await connection.execute(
                    """
                    INSERT INTO llm_budget_reservations (
                        review_id, budget_day, reserved_usd,
                        actual_cost_usd, status
                    )
                    VALUES ($1::uuid, $2, $3, 0, 'reserved')
                    """,
                    review_id,
                    today,
                    amount,
                )

            return BudgetReservation(True, "reserved", amount, today)


async def settle_review_budget(
    database,
    review_id: str | UUID,
    actual_cost_usd: Decimal,
) -> bool:
    """Settle a reservation once. Returns False if already settled."""

    review_id = _review_id(review_id)
    actual = _money(actual_cost_usd, "actual_cost_usd")

    async with _pool(database).acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                review_id,
            )

            reservation = await connection.fetchrow(
                """
                SELECT budget_day, reserved_usd, status
                FROM llm_budget_reservations
                WHERE review_id = $1::uuid
                FOR UPDATE
                """,
                review_id,
            )

            if reservation is None:
                raise ValueError("No budget reservation exists for this review")
            if reservation["status"] == "settled":
                return False
            if reservation["status"] != "reserved":
                raise ValueError("Reservation is not in a settleable state")

            reserved = Decimal(str(reservation["reserved_usd"]))
            day = reservation["budget_day"]

            await connection.execute(
                """
                UPDATE daily_budget_usage
                SET reserved_usd = GREATEST(reserved_usd - $2, 0),
                    actual_usd = actual_usd + $3
                WHERE budget_day = $1
                """,
                day,
                reserved,
                actual,
            )

            await connection.execute(
                """
                UPDATE llm_budget_reservations
                SET actual_cost_usd = $2,
                    status = 'settled',
                    updated_at = now(),
                    settled_at = now()
                WHERE review_id = $1::uuid
                """,
                review_id,
                actual,
            )

            return True


async def release_review_budget(
    database,
    review_id: str | UUID,
) -> bool:
    """Release an unsettled reservation. Returns False if not releasable."""

    review_id = _review_id(review_id)

    async with _pool(database).acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                review_id,
            )

            reservation = await connection.fetchrow(
                """
                SELECT budget_day, reserved_usd, status
                FROM llm_budget_reservations
                WHERE review_id = $1::uuid
                FOR UPDATE
                """,
                review_id,
            )

            if reservation is None or reservation["status"] != "reserved":
                return False

            await connection.execute(
                """
                UPDATE daily_budget_usage
                SET reserved_usd =
                    GREATEST(reserved_usd - $2, 0)
                WHERE budget_day = $1
                """,
                reservation["budget_day"],
                reservation["reserved_usd"],
            )

            await connection.execute(
                """
                UPDATE llm_budget_reservations
                SET status = 'released',
                    updated_at = now()
                WHERE review_id = $1::uuid
                """,
                review_id,
            )

            return True
