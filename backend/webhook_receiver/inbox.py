"""Database operations for durable GitHub webhook delivery tracking."""

import json
from typing import Any


MAX_DELIVERY_ATTEMPTS = 5
STALE_PROCESSING_MINUTES = 15

CLAIMABLE_STATUSES = ("pending", "failed")


async def record_delivery(
    database,
    *,
    delivery_id: str,
    event_name: str,
    action: str | None,
    installation_id: int | None,
    repo: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert a delivery once, returning its existing state on duplicates."""

    if not isinstance(payload, dict):
        raise ValueError("Webhook payload must be a dictionary.")

    inserted = await database.fetch(
        """
        INSERT INTO webhook_inbox (
            delivery_id,
            event_name,
            action,
            installation_id,
            repo,
            payload,
            status,
            attempts
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'pending', 0)
        ON CONFLICT (delivery_id) DO NOTHING
        RETURNING delivery_id, status, attempts, last_attempt_at
        """,
        delivery_id,
        event_name,
        action,
        installation_id,
        repo,
        json.dumps(payload, separators=(",", ":")),
    )

    if inserted:
        return {
            **dict(inserted[0]),
            "created": True,
        }

    existing = await database.fetch(
        """
        SELECT delivery_id, status, attempts, last_attempt_at
        FROM webhook_inbox
        WHERE delivery_id = $1
        """,
        delivery_id,
    )

    if not existing:
        raise RuntimeError(
            "Delivery insert conflicted, but its record could not be read."
        )

    return {
        **dict(existing[0]),
        "created": False,
    }


async def claim_delivery(database, delivery_id: str):
    """Atomically claim an unprocessed or stale webhook delivery."""

    rows = await database.fetch(
        """
        UPDATE webhook_inbox
        SET status = 'processing',
            attempts = attempts + 1,
            last_attempt_at = now(),
            last_error = NULL
        WHERE delivery_id = $1
          AND attempts < $2
          AND (
              status IN ('pending', 'failed')
              OR (
                  status = 'processing'
                  AND last_attempt_at <
                      now() - ($3 * INTERVAL '1 minute')
              )
          )
        RETURNING
            delivery_id,
            event_name,
            action,
            installation_id,
            repo,
            payload,
            status,
            attempts,
            last_attempt_at
        """,
        delivery_id,
        MAX_DELIVERY_ATTEMPTS,
        STALE_PROCESSING_MINUTES,
    )

    return dict(rows[0]) if rows else None


async def mark_delivery(
    database,
    delivery_id: str,
    *,
    status: str,
    last_error: str | None = None,
) -> None:
    """Update delivery state without storing potentially sensitive errors."""

    allowed = {
        "pending",
        "completed",
        "failed",
        "deferred",
        "ignored",
        "dead_letter",
    }
    if status not in allowed:
        raise ValueError("Unsupported webhook delivery status.")

    if last_error is not None:
        if not isinstance(last_error, str):
            raise ValueError("last_error must be text or None.")
        last_error = last_error[:100]

    await database.execute(
        """
        UPDATE webhook_inbox
        SET status = $2,
            last_error = $3
        WHERE delivery_id = $1
        """,
        delivery_id,
        status,
        last_error,
    )


async def get_delivery(database, delivery_id: str):
    rows = await database.fetch(
        """
        SELECT delivery_id, event_name, action, installation_id, repo,
               payload, status, attempts, last_error,
               created_at, last_attempt_at
        FROM webhook_inbox
        WHERE delivery_id = $1
        """,
        delivery_id,
    )
    return dict(rows[0]) if rows else None


async def list_retryable_deliveries(database, limit: int = 50):
    """Find pending, failed, or stale processing work for a dispatcher."""

    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 500
    ):
        raise ValueError("limit must be between 1 and 500.")

    rows = await database.fetch(
        """
        SELECT delivery_id, status, attempts, last_attempt_at
        FROM webhook_inbox
        WHERE attempts < $1
          AND (
              status IN ('pending', 'failed')
              OR (
                  status = 'processing'
                  AND last_attempt_at <
                      now() - ($2 * INTERVAL '1 minute')
              )
          )
        ORDER BY created_at ASC
        LIMIT $3
        """,
        MAX_DELIVERY_ATTEMPTS,
        STALE_PROCESSING_MINUTES,
        limit,
    )
    return [dict(row) for row in rows]
