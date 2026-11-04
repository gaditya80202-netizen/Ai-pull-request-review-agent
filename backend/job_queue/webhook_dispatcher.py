"""Redis redispatcher for durable webhook inbox records."""

import json
import logging
from typing import Any

from backend.webhook_receiver.inbox import (
    get_delivery,
    list_retryable_deliveries,
    mark_delivery,
)

logger = logging.getLogger(__name__)

SUPPORTED_PR_ACTIONS = {"opened", "synchronize", "reopened"}


def _decode_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Inbox payload is not a JSON object.")


def _pr_job_payload(delivery: dict[str, Any]) -> dict[str, Any]:
    payload = _decode_payload(delivery.get("payload"))
    repository = payload.get("repository") or {}
    pull_request = payload.get("pull_request") or {}
    installation = payload.get("installation") or {}
    head = pull_request.get("head") or {}

    if not all(
        isinstance(item, dict)
        for item in (repository, pull_request, installation, head)
    ):
        raise ValueError("Malformed pull-request inbox payload.")

    installation_id = installation.get("id")
    repo = repository.get("full_name")
    pr_number = pull_request.get("number")
    head_sha = head.get("sha")

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
        or not isinstance(repo, str)
        or repo.count("/") != 1
        or any(not item for item in repo.split("/"))
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number < 1
        or not isinstance(head_sha, str)
        or not head_sha
        or len(head_sha) > 64
    ):
        raise ValueError("Invalid PR fields in webhook inbox payload.")

    return {
        "delivery_id": delivery["delivery_id"],
        "installation_id": installation_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "inbox_managed": True,
    }


def _reindex_job_payload(delivery: dict[str, Any]):
    """Reconstruct a default-branch reindex job from an inbox record."""

    payload = _decode_payload(delivery.get("payload"))
    repository = payload.get("repository") or {}
    installation = payload.get("installation") or {}

    if not isinstance(repository, dict) or not isinstance(installation, dict):
        raise TypeError("Malformed push inbox payload.")

    repo = repository.get("full_name")
    default_branch = repository.get("default_branch")
    ref = payload.get("ref")
    head_sha = payload.get("after")
    installation_id = installation.get("id")

    if payload.get("deleted") is True:
        return None

    if (
        not isinstance(ref, str)
        or not isinstance(default_branch, str)
        or not default_branch
        or ref != f"refs/heads/{default_branch}"
    ):
        return None

    if (
        not isinstance(repo, str)
        or repo.count("/") != 1
        or any(not part for part in repo.split("/"))
        or not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
        or not isinstance(head_sha, str)
        or len(head_sha) != 40
    ):
        raise ValueError("Invalid default-branch push fields.")

    return {
        "delivery_id": delivery["delivery_id"],
        "installation_id": installation_id,
        "repo": repo,
        "ref": ref,
        "head_sha": head_sha,
        "inbox_managed": True,
    }


async def dispatch_pending_webhooks(
    ctx,
    *,
    limit: int = 50,
) -> dict[str, int]:
    """Queue retryable PR deliveries without losing the durable inbox row."""

    database = ctx.get("tiger_client")
    queue = ctx.get("redis") or ctx.get("arq_pool")

    if database is None or queue is None:
        logger.warning("Webhook dispatcher lacks database or Redis.")
        return {"scanned": 0, "queued": 0, "deferred": 0, "dead_letter": 0}

    candidates = await list_retryable_deliveries(database, limit=limit)
    queued = 0
    deferred = 0
    dead_letter = 0

    for candidate in candidates:
        delivery_id = candidate.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            deferred += 1
            continue

        delivery = await get_delivery(database, delivery_id)
        if delivery is None:
            deferred += 1
            continue

        try:
            event_name = delivery.get("event_name")
            action = delivery.get("action")

            attempts = candidate.get("attempts", 0)
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 0
            ):
                raise ValueError("Invalid inbox attempt count.")

            if event_name == "pull_request":
                if action not in SUPPORTED_PR_ACTIONS:
                    await mark_delivery(
                        database,
                        delivery_id,
                        status="ignored",
                        last_error="Unsupported PR action",
                    )
                    deferred += 1
                    continue

                job_function = "run_review"
                job_payload = _pr_job_payload(delivery)

            elif event_name == "push":
                job_payload = _reindex_job_payload(delivery)
                if job_payload is None:
                    await mark_delivery(
                        database,
                        delivery_id,
                        status="ignored",
                        last_error="Not a default-branch push",
                    )
                    deferred += 1
                    continue
                job_function = "reindex_repo"

            else:
                await mark_delivery(
                    database,
                    delivery_id,
                    status="ignored",
                    last_error="Unsupported webhook event",
                )
                deferred += 1
                continue

            # Worker increments attempts when claiming; deterministic job
            # IDs make repeated dispatcher ticks safe for the same attempt.
            job_id = f"github-{delivery_id}-a{attempts + 1}"

            job = await queue.enqueue_job(
                job_function,
                job_payload,
                _job_id=job_id,
            )

            if job is None:
                # A job with this ID may already be queued or retained.
                # Leave the inbox state recoverable; the worker owns it.
                deferred += 1
                continue

            queued += 1

        except Exception as exc:
            logger.warning(
                "Webhook delivery %s could not be redispatched (%s).",
                delivery_id,
                type(exc).__name__,
            )
            deferred += 1

    return {
        "scanned": len(candidates),
        "queued": queued,
        "deferred": deferred,
        "dead_letter": dead_letter,
    }
