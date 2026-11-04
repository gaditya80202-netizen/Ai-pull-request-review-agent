"""Authenticated API endpoints for human review."""

import secrets
import re
from uuid import UUID
from typing import Literal

import httpx

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from backend.core.settings import settings
from backend.integrations.github_client import GitHubAPIError, GitHubClient
from backend.hitl.publication import (
    publish_approved_review,
    reconcile_approved_review,
)
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



async def _get_live_head_status(database, hitl_review_id: str) -> dict:
    """Compare the stored review commit with GitHub's current PR head."""
    try:
        review_id = str(UUID(str(hitl_review_id)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "hitl_review_id must be a valid UUID") from None

    rows = await database.fetch(
        """
        SELECT h.id::text AS hitl_review_id, r.repo, r.pr_number,
               r.head_sha AS reviewed_head_sha
        FROM hitl_reviews AS h
        JOIN pr_review_records AS r ON r.id = h.review_id
        WHERE h.id = $1::uuid AND h.status = 'pending'
        """,
        review_id,
    )
    if not rows:
        raise HTTPException(404, "Pending human-review request was not found")

    review = dict(rows[0])
    try:
        async with GitHubClient() as github:
            pr = await github.get_pull_request(
                review["repo"], int(review["pr_number"])
            )
    except (GitHubAPIError, RuntimeError, httpx.RequestError):
        raise HTTPException(
            503,
            "Unable to verify the current GitHub head SHA. Retry before approving.",
        ) from None

    head = pr.get("head") if isinstance(pr, dict) else None
    current_sha = head.get("sha") if isinstance(head, dict) else None
    reviewed_sha = review.get("reviewed_head_sha")

    if not isinstance(current_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", current_sha
    ):
        raise HTTPException(503, "GitHub returned an invalid head SHA")
    if not isinstance(reviewed_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", reviewed_sha
    ):
        raise HTTPException(503, "The stored review head SHA is invalid")

    matches = current_sha.lower() == reviewed_sha.lower()
    return {
        "hitl_review_id": review_id,
        "repo": review["repo"],
        "pr_number": int(review["pr_number"]),
        "reviewed_head_sha": reviewed_sha,
        "current_head_sha": current_sha,
        "verified": True,
        "matches": matches,
        "message": (
            "The review matches the current PR head."
            if matches
            else "The PR has changed since this review was generated. Refresh the review first."
        ),
    }


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



@router.get("/reviews/{hitl_review_id}/findings")
async def list_review_findings(
    hitl_review_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Return persisted findings for one pending human review."""

    authenticated_reviewer(authorization)
    database = get_database(request)

    try:
        review_id = str(UUID(str(hitl_review_id)))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(
            422, "hitl_review_id must be a valid UUID"
        ) from None

    installation_id = settings.github_installation_id
    if not isinstance(installation_id, int) or installation_id <= 0:
        raise HTTPException(
            503,
            "GitHub installation ID is required to scope findings safely",
        )

    rows = await database.fetch(
        """
        SELECT
            h.id::text AS hitl_review_id,
            r.id::text AS review_id,
            f.id::text AS finding_id,
            f.agent_type,
            f.severity,
            f.category,
            f.summary,
            f.file_path,
            f.line_start,
            f.line_end,
            f.confidence,
            f.rationale,
            f.suggestion
        FROM hitl_reviews AS h
        JOIN pr_review_records AS r ON r.id = h.review_id
        LEFT JOIN finding_records AS f ON f.review_id = r.id
        WHERE h.id = $1::uuid
          AND h.status = 'pending'
          AND r.installation_id = $2
        ORDER BY
            CASE UPPER(COALESCE(f.severity, ''))
                WHEN 'CRITICAL' THEN 1
                WHEN 'HIGH' THEN 2
                WHEN 'MEDIUM' THEN 3
                WHEN 'LOW' THEN 4
                ELSE 5
            END,
            f.agent_type,
            f.file_path NULLS LAST,
            f.line_start NULLS LAST
        """,
        review_id,
        installation_id,
    )

    if not rows:
        raise HTTPException(
            404, "Pending human-review request was not found"
        )

    findings = [
        dict(row)
        for row in rows
        if row.get("finding_id") is not None
    ]

    return {
        "hitl_review_id": review_id,
        "review_id": rows[0]["review_id"],
        "count": len(findings),
        "items": jsonable_encoder(findings),
    }



@router.get("/reviews/{hitl_review_id}/head-status")
async def get_review_head_status(
    hitl_review_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    authenticated_reviewer(authorization)
    return await _get_live_head_status(
        get_database(request), hitl_review_id
    )



@router.get("/analytics")
async def get_analytics(
    request: Request,
    authorization: str | None = Header(default=None),
):
    authenticated_reviewer(authorization)
    database = get_database(request)
    installation_id = settings.github_installation_id
    if not isinstance(installation_id, int) or installation_id <= 0:
        raise HTTPException(
            503,
            "GitHub installation ID is required to scope analytics safely",
        )

    summary_rows = await database.fetch(
        """
        SELECT COUNT(*)::bigint AS agent_event_count,
               COUNT(*) FILTER (WHERE cost_usd IS NOT NULL)::bigint
                   AS cost_event_count,
               COUNT(*) FILTER (WHERE cost_usd IS NULL)::bigint
                   AS unknown_cost_event_count,
               SUM(cost_usd) AS known_cost_usd,
               SUM(tokens_in)::bigint AS tokens_in,
               SUM(tokens_out)::bigint AS tokens_out,
               COUNT(*) FILTER (
                   WHERE tokens_in IS NOT NULL AND tokens_out IS NOT NULL
               )::bigint AS token_usage_event_count,
               COUNT(DISTINCT review_id)::bigint AS reviews_with_events,
               COUNT(DISTINCT review_id) FILTER (
                   WHERE cost_usd IS NOT NULL
               )::bigint AS reviews_with_known_cost,
               AVG(latency_ms)::double precision AS average_latency_ms
        FROM agent_events
        WHERE installation_id = $1
          AND ts >= now() - INTERVAL '30 days'
        """,
        installation_id,
    )
    summary = dict(summary_rows[0]) if summary_rows else {}
    for field in (
        "agent_event_count", "cost_event_count",
        "unknown_cost_event_count", "token_usage_event_count",
        "reviews_with_events", "reviews_with_known_cost",
    ):
        summary.setdefault(field, 0)

    known_cost = summary.get("known_cost_usd")
    known_reviews = int(summary["reviews_with_known_cost"])
    summary["average_known_cost_per_review_usd"] = (
        float(known_cost) / known_reviews
        if known_cost is not None and known_reviews > 0
        else None
    )
    summary["cost_data_complete"] = (
        int(summary["agent_event_count"]) > 0
        and int(summary["unknown_cost_event_count"]) == 0
    )
    summary["token_data_complete"] = (
        int(summary["agent_event_count"]) > 0
        and int(summary["token_usage_event_count"])
        == int(summary["agent_event_count"])
    )

    daily = await database.fetch(
        """
        SELECT date_trunc('day', ts) AS day,
               COUNT(*)::bigint AS agent_event_count,
               SUM(cost_usd) AS known_cost_usd,
               COUNT(*) FILTER (WHERE cost_usd IS NULL)::bigint
                   AS unknown_cost_event_count,
               SUM(tokens_in)::bigint AS tokens_in,
               SUM(tokens_out)::bigint AS tokens_out
        FROM agent_events
        WHERE installation_id = $1
          AND ts >= now() - INTERVAL '30 days'
        GROUP BY date_trunc('day', ts)
        ORDER BY day
        """,
        installation_id,
    )
    statuses = await database.fetch(
        """
        SELECT status, COUNT(*)::bigint AS count
        FROM pr_review_records
        WHERE installation_id = $1
          AND created_at >= now() - INTERVAL '30 days'
        GROUP BY status ORDER BY status
        """,
        installation_id,
    )
    pending = await database.fetch(
        """
        SELECT COUNT(*)::bigint AS pending_count
        FROM hitl_reviews h
        JOIN pr_review_records r ON r.id = h.review_id
        WHERE r.installation_id = $1 AND h.status = 'pending'
        """,
        installation_id,
    )
    summary["pending_reviews"] = (
        int(pending[0]["pending_count"]) if pending else 0
    )

    return jsonable_encoder({
        "period_days": 30,
        "summary": summary,
        "daily": [dict(row) for row in daily],
        "review_statuses": [dict(row) for row in statuses],
        "notes": {
            "costs": (
                "Recorded costs are estimates based on available token usage "
                "and model pricing. Unknown costs are excluded from known totals."
            ),
            "cost_per_review": (
                "Known cost divided by reviews with at least one priced event; "
                "the result may be incomplete when some event costs are unknown."
            ),
            "tokens": "Token totals may be unavailable when usage was not recorded.",
        },
    })


@router.post("/reviews/{hitl_review_id}/decision")
async def submit_review_decision(
    hitl_review_id: str,
    payload: DecisionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    reviewer = authenticated_reviewer(authorization)
    database = get_database(request)

    # Reject stale approvals before recording the human decision.
    # The publication module performs its own SHA checks as well.
    if payload.decision == "approved":
        head_status = await _get_live_head_status(database, hitl_review_id)
        if not head_status["matches"]:
            raise HTTPException(
                409,
                "Cannot approve a stale review. Refresh the review first.",
            )


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

    if result.get("decision") == "approved":
        publication = await publish_approved_review(
            database,
            hitl_review_id=hitl_review_id,
        )
        result.update(publication)
    else:
        result["publication_status"] = "not_attempted"
        result["publication_allowed"] = False
        result["message"] = "Human rejection recorded; no GitHub review was published."
    return result



@router.post("/reviews/{hitl_review_id}/reconcile")
async def reconcile_review_publication(
    hitl_review_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    authenticated_reviewer(authorization)
    database = get_database(request)
    return await reconcile_approved_review(
        database,
        hitl_review_id=hitl_review_id,
    )


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
