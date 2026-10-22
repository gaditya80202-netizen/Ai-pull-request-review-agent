"""Authenticated API endpoints for human review."""

import secrets
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.encoders import jsonable_encoder

from backend.core.settings import settings
from backend.hitl.service import decide_review, record_finding_feedback


router = APIRouter(prefix="/api/hitl", tags=["human-review"])

FeedbackVerdict = Literal[
    "true_positive",
    "false_positive",
    "needs_context",
    "duplicate",
    "not_actionable",
]


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]


class FindingFeedbackRequest(BaseModel):
    verdict: FeedbackVerdict
    comment: str | None = Field(default=None, max_length=2000)


def authenticated_reviewer(authorization: str | None) -> str:
    """Authenticate the configured single operator without trusting the body."""

    expected_token = settings.hitl_api_token
    reviewer_id = settings.hitl_reviewer_id

    if (
        not isinstance(expected_token, str)
        or len(expected_token) < 32
        or not isinstance(reviewer_id, str)
        or not reviewer_id.strip()
    ):
        raise HTTPException(
            503,
            "Human-review API authentication is not configured",
        )

    if not isinstance(authorization, str):
        raise HTTPException(401, "Bearer authentication is required")

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(401, "Bearer authentication is required")

    if not secrets.compare_digest(parts[1], expected_token):
        raise HTTPException(401, "Invalid API token")

    return reviewer_id.strip()


def get_database(request: Request):
    database = getattr(request.app.state, "tiger_client", None)
    if database is None:
        raise HTTPException(503, "Database is unavailable")
    return database


@router.get("/reviews/pending")
async def list_pending_reviews(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    authorization: str | None = Header(default=None),
):
    authenticated_reviewer(authorization)
    database = get_database(request)

    rows = await database.fetch(
        """
        SELECT
            h.id::text AS hitl_review_id,
            h.review_id::text AS review_id,
            h.reason,
            h.status AS hitl_status,
            h.created_at,
            r.repo,
            r.pr_number,
            r.head_sha,
            r.status AS review_status,
            (
                SELECT COUNT(*)
                FROM finding_records AS f
                WHERE f.review_id = r.id
            ) AS finding_count
        FROM hitl_reviews AS h
        JOIN pr_review_records AS r ON r.id = h.review_id
        WHERE h.status = 'pending'
        ORDER BY h.created_at ASC
        LIMIT $1
        """,
        limit,
    )

    return {
        "items": jsonable_encoder([dict(row) for row in rows]),
        "count": len(rows),
        "publication_allowed": False,
    }


@router.post("/reviews/{hitl_review_id}/decision")
async def submit_review_decision(
    hitl_review_id: str,
    payload: DecisionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    reviewer = authenticated_reviewer(authorization)
    database = get_database(request)

    try:
        result = await decide_review(
            database,
            hitl_review_id=hitl_review_id,
            decision=payload.decision,
            reviewer=reviewer,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "already decided" in message else 422
        raise HTTPException(status_code, message) from None

    # This endpoint records a decision; it never publishes to GitHub.
    return result


@router.post("/findings/{finding_id}/feedback")
async def submit_finding_feedback(
    finding_id: str,
    payload: FindingFeedbackRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    reviewer = authenticated_reviewer(authorization)
    database = get_database(request)

    try:
        return await record_finding_feedback(
            database,
            finding_id=finding_id,
            verdict=payload.verdict,
            reviewer=reviewer,
            comment=payload.comment,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
