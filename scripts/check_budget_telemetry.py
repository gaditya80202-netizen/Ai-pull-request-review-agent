import asyncio
from decimal import Decimal
from uuid import uuid4

from backend.core.settings import settings
from backend.economics.budget import (
    reserve_review_budget,
    settle_review_budget,
)
from backend.economics.pricing import calculate_cost_usd
from backend.memory.tiger_client import TimescaleClient
from backend.observability.persistence import persist_agent_events


async def first_row(db, query, *args):
    rows = await db.fetch(query, *args)
    return rows[0] if rows else None


async def main():
    probe = uuid4().hex
    repo = f"__telemetry_probe__/{probe}"
    installation_id = settings.github_installation_id or 987654
    db = TimescaleClient()
    review_id = None
    reservation_day = None
    original_daily_row_existed = False
    baseline_reserved = Decimal("0")
    baseline_actual = Decimal("0")
    succeeded = False

    statuses = {
        name: {
            "status": "success",
            "model": "deepseek-flash",
            "prompt_version": f"{name}_probe",
            "tokens_in": 1000,
            "tokens_out": 100,
            "latency_ms": 125,
            "accepted_count": 0,
            "rejected_count": 0,
        }
        for name in ("security", "quality", "tests", "docs")
    }

    try:
        await db.connect()

        before_day = await db.fetchval(
            "SELECT (now() AT TIME ZONE 'UTC')::date"
        )
        before_row = await first_row(
            db,
            """
            SELECT reserved_usd, actual_usd
            FROM daily_budget_usage
            WHERE budget_day = $1
            """,
            before_day,
        )

        original_daily_row_existed = before_row is not None
        if before_row:
            baseline_reserved = Decimal(str(before_row["reserved_usd"]))
            baseline_actual = Decimal(str(before_row["actual_usd"]))

        inserted = await db.fetch(
            """
            INSERT INTO pr_review_records (
                installation_id, repo, pr_number,
                head_sha, delivery_id, status
            )
            VALUES ($1, $2, 2100000000, $3, $4, 'running')
            RETURNING id::text AS id
            """,
            installation_id,
            repo,
            f"telemetry-probe-{probe}",
            f"telemetry-probe-{probe}",
        )
        review_id = inserted[0]["id"]

        reservation = await reserve_review_budget(
            db, review_id, Decimal("0.050000")
        )
        if not reservation.allowed:
            raise RuntimeError(
                f"Probe reservation denied: {reservation.reason}"
            )

        reservation_day = reservation.budget_day
        print("PASS: Budget reservation created.")

        async with db.pool.acquire() as connection:
            async with connection.transaction():
                count = await persist_agent_events(
                    connection,
                    installation_id=installation_id,
                    review_id=review_id,
                    agent_status=statuses,
                    findings=[],
                )

        assert count == 4
        print("PASS: Four telemetry events persisted.")

        rows = await db.fetch(
            """
            SELECT agent, model, tokens_in, tokens_out, cost_usd
            FROM agent_events
            WHERE review_id = $1::uuid
            ORDER BY agent
            """,
            review_id,
        )
        assert len(rows) == 4

        event_total = sum(
            (Decimal(str(row["cost_usd"])) for row in rows),
            start=Decimal("0"),
        )
        expected_total = sum(
            (
                calculate_cost_usd(
                    model="deepseek-flash",
                    tokens_in=1000,
                    tokens_out=100,
                )
                for _ in statuses
            ),
            start=Decimal("0"),
        )

        assert event_total == expected_total, (
            f"Event cost {event_total} != expected {expected_total}"
        )
        print(f"PASS: Telemetry cost total = ${event_total}")

        assert await settle_review_budget(
            db, review_id, expected_total
        )
        assert not await settle_review_budget(
            db, review_id, expected_total
        )
        print("PASS: Settlement is idempotent.")

        daily = await first_row(
            db,
            """
            SELECT reserved_usd, actual_usd
            FROM daily_budget_usage
            WHERE budget_day = $1
            """,
            reservation_day,
        )
        assert daily is not None

        daily_reserved = Decimal(str(daily["reserved_usd"]))
        daily_actual = Decimal(str(daily["actual_usd"]))

        assert daily_reserved == baseline_reserved, (
            f"Reserved total changed unexpectedly: {daily_reserved}"
        )
        assert daily_actual - baseline_actual == expected_total, (
            f"Daily actual delta is {daily_actual - baseline_actual}; "
            f"expected {expected_total}"
        )

        print("PASS: Daily totals reconcile with telemetry.")
        succeeded = True

    finally:
        if db.pool is not None:
            try:
                async with db.pool.acquire() as connection:
                    async with connection.transaction():
                        ledger = None
                        if review_id is not None:
                            ledger = await connection.fetchrow(
                                """
                                SELECT budget_day, reserved_usd,
                                       actual_cost_usd, status
                                FROM llm_budget_reservations
                                WHERE review_id = $1::uuid
                                FOR UPDATE
                                """,
                                review_id,
                            )

                        # Reverse only this probe's budget contribution.
                        if ledger is not None:
                            if ledger["status"] == "reserved":
                                await connection.execute(
                                    """
                                    UPDATE daily_budget_usage
                                    SET reserved_usd =
                                        GREATEST(reserved_usd - $2, 0)
                                    WHERE budget_day = $1
                                    """,
                                    ledger["budget_day"],
                                    ledger["reserved_usd"],
                                )
                            elif ledger["status"] == "settled":
                                await connection.execute(
                                    """
                                    UPDATE daily_budget_usage
                                    SET actual_usd =
                                        GREATEST(actual_usd - $2, 0)
                                    WHERE budget_day = $1
                                    """,
                                    ledger["budget_day"],
                                    ledger["actual_cost_usd"],
                                )

                        if review_id is not None:
                            await connection.execute(
                                """
                                DELETE FROM agent_events
                                WHERE review_id = $1::uuid
                                """,
                                review_id,
                            )
                            await connection.execute(
                                """
                                DELETE FROM pr_review_records
                                WHERE id = $1::uuid AND repo = $2
                                """,
                                review_id,
                                repo,
                            )

                        if (
                            reservation_day is not None
                            and not original_daily_row_existed
                        ):
                            await connection.execute(
                                """
                                DELETE FROM daily_budget_usage
                                WHERE budget_day = $1
                                  AND reserved_usd = 0
                                  AND actual_usd = 0
                                """,
                                reservation_day,
                            )
            finally:
                await db.close()

        if succeeded:
            print("PASS: Temporary review and telemetry rows cleaned up.")


asyncio.run(main())
