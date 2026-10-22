import json
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from redis.exceptions import RedisError

from backend.core.settings import settings
from backend.webhook_receiver.validator import valid_signature


router = APIRouter()

HANDLED_PR_ACTIONS = {"opened", "synchronize", "reopened"}
DELIVERY_TTL_SECONDS = 7 * 24 * 60 * 60


@router.post("/webhook/github", status_code=202, tags=["github"])
async def github_webhook(request: Request):
    # Verify the original request bytes before interpreting the payload.
    body = await request.body()
    secret = settings.github_webhook_secret

    if not secret:
        raise HTTPException(503, "Webhook secret is not configured")

    if not valid_signature(
        secret,
        body,
        request.headers.get("x-hub-signature-256"),
    ):
        raise HTTPException(401, "Invalid webhook signature")

    event = request.headers.get("x-github-event")
    delivery_header = request.headers.get("x-github-delivery")

    if not delivery_header:
        raise HTTPException(400, "Missing GitHub delivery ID")

    try:
        delivery_id = str(UUID(delivery_header))
    except ValueError:
        raise HTTPException(400, "Invalid GitHub delivery ID") from None

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON payload") from None

    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected a JSON object")

    # This endpoint currently processes pull requests only.
    if event != "pull_request":
        return {"ok": True, "ignored": True}

    if payload.get("action") not in HANDLED_PR_ACTIONS:
        return {"ok": True, "ignored": True}

    repository = payload.get("repository") or {}
    pull_request = payload.get("pull_request") or {}
    installation = payload.get("installation") or {}
    head = pull_request.get("head") or {}

    installation_id = installation.get("id")
    repo = repository.get("full_name")
    pr_number = pull_request.get("number")
    head_sha = head.get("sha")

    if (
        not isinstance(installation_id, int)
        or not isinstance(pr_number, int)
        or not repo
        or not head_sha
    ):
        raise HTTPException(400, "Required pull-request fields are missing")

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise HTTPException(503, "Background queue is unavailable")

    delivery_key = f"github:delivery:{delivery_id}"

    job_payload = {
        "delivery_id": delivery_id,
        "installation_id": installation_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
    }

    try:
        # Ignore deliveries already recorded by this application.
        if await arq_pool.get(delivery_key):
            return {"ok": True, "duplicate": True}

        # ARQ's custom job ID prevents duplicate enqueues while the
        # corresponding job or retained result still exists.
        job = await arq_pool.enqueue_job(
            "run_review",
            job_payload,
            _job_id=f"github-{delivery_id}",
        )

        if job is None:
            await arq_pool.set(
                delivery_key,
                "queued",
                ex=DELIVERY_TTL_SECONDS,
                nx=True,
            )
            return {"ok": True, "duplicate": True}

        await arq_pool.set(
            delivery_key,
            "queued",
            ex=DELIVERY_TTL_SECONDS,
            nx=True,
        )

        return {
            "ok": True,
            "queued": True,
            "job_id": job.job_id,
        }

    except RedisError as exc:
        raise HTTPException(
            503,
            "Queue unavailable; GitHub may retry this delivery",
        ) from exc
