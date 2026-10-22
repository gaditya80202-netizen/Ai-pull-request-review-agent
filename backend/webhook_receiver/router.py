import json
import re
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from redis.exceptions import RedisError

from backend.core.settings import settings
from backend.webhook_receiver.inbox import (
    MAX_DELIVERY_ATTEMPTS,
    mark_delivery,
    record_delivery,
)
from backend.webhook_receiver.validator import valid_signature


router = APIRouter()
HANDLED_PR_ACTIONS = {"opened", "synchronize", "reopened"}
SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


async def _record_and_queue(
    request: Request,
    *,
    delivery_id: str,
    event_name: str,
    action: str | None,
    installation_id: int,
    repo: str,
    payload: dict,
    job_function: str,
    job_payload: dict,
):
    database = getattr(request.app.state, "tiger_client", None)
    if database is None:
        raise HTTPException(503, "Database is unavailable")

    arq_pool = getattr(request.app.state, "arq_pool", None)
    if arq_pool is None:
        raise HTTPException(503, "Background queue is unavailable")

    try:
        delivery = await record_delivery(
            database,
            delivery_id=delivery_id,
            event_name=event_name,
            action=action,
            installation_id=installation_id,
            repo=repo,
            payload=payload,
        )
    except Exception as exc:
        raise HTTPException(
            503,
            "Could not record webhook delivery; GitHub may retry",
        ) from exc

    status = delivery["status"]
    attempts = delivery["attempts"]

    if status in {"completed", "ignored", "dead_letter", "processing"}:
        return {
            "ok": True,
            "duplicate": True,
            "status": status,
        }

    if attempts >= MAX_DELIVERY_ATTEMPTS:
        await mark_delivery(
            database,
            delivery_id,
            status="dead_letter",
            last_error="Maximum delivery attempts reached",
        )
        return {"ok": True, "duplicate": True, "status": "dead_letter"}

    job_id = f"github-{delivery_id}-a{attempts + 1}"

    try:
        job = await arq_pool.enqueue_job(
            job_function,
            job_payload,
            _job_id=job_id,
        )
    except RedisError as exc:
        # The durable inbox record remains available for redispatch.
        raise HTTPException(
            503,
            "Queue unavailable; delivery remains in the durable inbox",
        ) from exc

    if job is None:
        return {
            "ok": True,
            "duplicate": True,
            "status": status,
        }

    return {
        "ok": True,
        "queued": True,
        "job_id": job.job_id,
        "delivery_id": delivery_id,
    }


@router.post("/webhook/github", status_code=202, tags=["github"])
async def github_webhook(request: Request):
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

    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}

    if event == "push":
        if not isinstance(repository, dict) or not isinstance(installation, dict):
            raise HTTPException(400, "Malformed push payload")

        repo = repository.get("full_name")
        default_branch = repository.get("default_branch")
        ref = payload.get("ref")
        installation_id = installation.get("id")

        # Ignore tags and other refs. Only index the repository's default
        # branch; deleting that branch is not a normal reindex operation.
        if (
            not isinstance(ref, str)
            or not ref.startswith("refs/heads/")
            or not isinstance(default_branch, str)
            or not default_branch
            or ref != f"refs/heads/{default_branch}"
            or payload.get("deleted") is True
        ):
            return {
                "ok": True,
                "ignored": True,
                "reason": "Not a push to the current default branch",
            }

        after = payload.get("after")
        if (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id <= 0
            or not isinstance(repo, str)
            or repo.count("/") != 1
            or any(not part for part in repo.split("/"))
            or not isinstance(after, str)
            or not SHA_PATTERN.fullmatch(after)
        ):
            raise HTTPException(400, "Required push fields are invalid")

        job_payload = {
            "delivery_id": delivery_id,
            "installation_id": installation_id,
            "repo": repo,
            "ref": ref,
            "head_sha": after,
            "inbox_managed": True,
        }

        return await _record_and_queue(
            request,
            delivery_id=delivery_id,
            event_name="push",
            action=None,
            installation_id=installation_id,
            repo=repo,
            payload=payload,
            job_function="reindex_repo",
            job_payload=job_payload,
        )

    if event != "pull_request":
        return {"ok": True, "ignored": True}

    action = payload.get("action")
    if action not in HANDLED_PR_ACTIONS:
        return {"ok": True, "ignored": True}

    pull_request = payload.get("pull_request") or {}
    head = pull_request.get("head") if isinstance(pull_request, dict) else None

    if not isinstance(head, dict):
        raise HTTPException(400, "Malformed pull-request head")

    installation_id = installation.get("id")
    repo = repository.get("full_name")
    pr_number = pull_request.get("number")
    head_sha = head.get("sha")

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number < 1
        or not isinstance(repo, str)
        or repo.count("/") != 1
        or any(not part for part in repo.split("/"))
        or not isinstance(head_sha, str)
        or not head_sha
        or len(head_sha) > 64
    ):
        raise HTTPException(400, "Required pull-request fields are invalid")

    job_payload = {
        "delivery_id": delivery_id,
        "installation_id": installation_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "inbox_managed": True,
    }

    return await _record_and_queue(
        request,
        delivery_id=delivery_id,
        event_name="pull_request",
        action=action,
        installation_id=installation_id,
        repo=repo,
        payload=payload,
        job_function="run_review",
        job_payload=job_payload,
    )
