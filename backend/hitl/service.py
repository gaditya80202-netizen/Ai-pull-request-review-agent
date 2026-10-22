"""Human review decisions and finding feedback.

This module records decisions only. It never posts reviews to GitHub.
The caller must authenticate the reviewer before calling these functions.
"""

from typing import Any
from uuid import UUID


FINDING_VERDICTS = {
    "true_positive",
    "false_positive",
    "needs_context",
    "duplicate",
    "not_actionable",
}


def _uuid(value: str, field: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{field} must be a valid UUID") from None


def _reviewer(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 120
    ):
        raise ValueError("reviewer must be 1–120 characters")
    return value.strip()


def _pool(database: Any):
    pool = getattr(database, "pool", None)
    if pool is None:
        raise RuntimeError("Database connection pool is unavailable")
    return pool


async def decide_review(
    database: Any,
    *,
    hitl_review_id: str,
    decision: str,
    reviewer: str,
) -> dict[str, Any]:
    """Approve or reject a pending review exactly once.

    Approval records a human decision only. It does not publish a review
    or bypass commit verification, evaluation, or posting safety gates.
    """

    hitl_review_id = _uuid(hitl_review_id, "hitl_review_id")
    reviewer = _reviewer(reviewer)

    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be 'approved' or 'rejected'")

    review_status = (
        "human_approved" if decision == "approved" else "human_rejected"
    )

    async with _pool(database).acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT
                    h.id::text AS hitl_review_id,
                    h.review_id::text AS review_id,
                    h.status AS hitl_status,
                    r.status AS review_status
                FROM hitl_reviews AS h
                JOIN pr_review_records AS r ON r.id = h.review_id
                WHERE h.id = $1::uuid
                FOR UPDATE OF h, r
                """,
                hitl_review_id,
            )

            if row is None:
                raise LookupError("Human-review request was not found")

            if row["hitl_status"] != "pending":
                raise ValueError("Human-review request is already decided")

            changed = await connection.fetchrow(
                """
                UPDATE hitl_reviews
                SET status = $2,
                    reviewer = $3,
                    decided_at = now()
                WHERE id = $1::uuid
                  AND status = 'pending'
                RETURNING id::text AS id
                """,
                hitl_review_id,
                decision,
                reviewer,
            )

            if changed is None:
                raise ValueError("Human-review request is already decided")

            await connection.execute(
                """
                UPDATE pr_review_records
                SET status = $2,
                    updated_at = now()
                WHERE id = $1::uuid
                """,
                row["review_id"],
                review_status,
            )

    return {
        "hitl_review_id": hitl_review_id,
        "review_id": row["review_id"],
        "decision": decision,
        "review_status": review_status,
        "publication_allowed": False,
        "message": (
            "Human decision recorded. GitHub posting remains disabled."
        ),
    }


async def record_finding_feedback(
    database: Any,
    *,
    finding_id: str,
    verdict: str,
    reviewer: str,
    comment: str | None = None,
) -> dict[str, Any]:
    """Append reviewer feedback for a finding."""

    finding_id = _uuid(finding_id, "finding_id")
    reviewer = _reviewer(reviewer)

    if verdict not in FINDING_VERDICTS:
        raise ValueError(
            f"verdict must be one of: {', '.join(sorted(FINDING_VERDICTS))}"
        )

    if comment is not None:
        if not isinstance(comment, str):
            raise ValueError("comment must be text or None")
        if len(comment) > 2000:
            raise ValueError("comment must be at most 2000 characters")
        comment = comment.strip() or None

    async with _pool(database).acquire() as connection:
        async with connection.transaction():
            finding = await connection.fetchrow(
                """
                SELECT id::text AS id
                FROM finding_records
                WHERE id = $1::uuid
                FOR KEY SHARE
                """,
                finding_id,
            )

            if finding is None:
                raise LookupError("Finding was not found")

            await connection.execute(
                """
                INSERT INTO hitl_feedback (
                    finding_id,
                    verdict,
                    comment,
                    created_by
                )
                VALUES ($1::uuid, $2, $3, $4)
                """,
                finding_id,
                verdict,
                comment,
                reviewer,
            )

    return {
        "finding_id": finding_id,
        "verdict": verdict,
        "recorded": True,
    }
