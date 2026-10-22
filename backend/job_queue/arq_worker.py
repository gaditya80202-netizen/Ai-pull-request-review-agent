from __future__ import annotations

import logging
from statistics import fmean
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from backend.economics.budget import (
    reserve_review_budget,
    settle_review_budget,
)
from backend.economics.pricing import calculate_cost_usd

from backend.core.settings import settings
from backend.integrations.diff_parser import parse_diff
from backend.integrations.github_client import GitHubClient
from backend.memory.tiger_client import TimescaleClient
from backend.memory.repository_indexer import index_repository
from backend.job_queue.webhook_dispatcher import dispatch_pending_webhooks
from backend.observability.persistence import persist_agent_events
from backend.webhook_receiver.inbox import (
    MAX_DELIVERY_ATTEMPTS,
    claim_delivery,
    mark_delivery,
)
from backend.orchestrator.graph import build_review_graph
from backend.security.masking import mask_secrets


logger = logging.getLogger(__name__)


def validate_review_job(job: Any) -> dict[str, Any]:
    """Validate the internal payload before processing a PR."""

    if not isinstance(job, dict):
        raise ValueError("Review job must be a dictionary.")

    installation_id = job.get("installation_id")
    repo = job.get("repo")
    pr_number = job.get("pr_number")
    head_sha = job.get("head_sha")

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
    ):
        raise ValueError("Invalid installation_id.")

    if (
        not isinstance(repo, str)
        or repo.count("/") != 1
        or any(not part.strip() for part in repo.split("/"))
        or repo != repo.strip()
    ):
        raise ValueError("Invalid repository name.")

    if (
        not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or pr_number < 1
    ):
        raise ValueError("Invalid pull-request number.")

    if (
        not isinstance(head_sha, str)
        or not head_sha.strip()
        or len(head_sha) > 64
    ):
        raise ValueError("Invalid pull-request head SHA.")

    return {
        "installation_id": installation_id,
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "delivery_id": job.get("delivery_id"),
        "inbox_managed": job.get("inbox_managed") is True,
    }


async def demo_job(ctx, message: str) -> dict:
    print(f"[demo_job] Received: {message}", flush=True)
    return {"status": "ok", "message": message}


async def _persist_review(
    database: TimescaleClient,
    *,
    review_id: str,
    installation_id: int,
    review_result: dict[str, Any],
) -> str:
    """Atomically store findings, final status and human-review request."""

    findings = review_result.get("findings", [])
    if not isinstance(findings, list):
        raise ValueError("Review graph returned an invalid findings list.")

    requested_status = review_result.get(
        "status", "human_review_required"
    )

    # This worker deliberately does not publish reviews to GitHub.
    # Even if the aggregation gate permits it, a human must approve it.
    auto_post_would_be_allowed = (
        review_result.get("auto_post_allowed") is True
    )

    if auto_post_would_be_allowed:
        persisted_status = "awaiting_human_approval"
    else:
        persisted_status = (
            requested_status
            if isinstance(requested_status, str)
            else "human_review_required"
        )

    confidences = [
        float(item["confidence"])
        for item in findings
        if isinstance(item, dict)
        and isinstance(item.get("confidence"), (int, float))
        and not isinstance(item.get("confidence"), bool)
    ]
    overall_confidence = fmean(confidences) if confidences else None

    reasons = review_result.get("gate_reasons", [])
    if not isinstance(reasons, list):
        reasons = []

    reasons = [
        str(reason)[:500]
        for reason in reasons
        if isinstance(reason, str)
    ]

    requires_human_review = (
        review_result.get("requires_human_review") is True
        or auto_post_would_be_allowed
    )

    if auto_post_would_be_allowed:
        reasons.append(
            "This worker does not publish reviews; human approval is required."
        )

    hitl_reason = "; ".join(reasons) or "Human approval is required."
    hitl_reason = hitl_reason[:2000]

    pool = database.pool
    if pool is None:
        raise RuntimeError("Database connection pool is unavailable.")

    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                DELETE FROM finding_records
                WHERE review_id = $1::uuid
                """,
                review_id,
            )

            for finding in findings:
                if not isinstance(finding, dict):
                    raise ValueError("Invalid finding returned by graph.")

                await connection.execute(
                    """
                    INSERT INTO finding_records (
                        review_id,
                        agent_type,
                        severity,
                        category,
                        summary,
                        file_path,
                        line_start,
                        line_end,
                        suggestion,
                        confidence,
                        rationale,
                        agreed_by,
                        disputed
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4, $5, $6, $7,
                        $8, $9, $10, $11, $12, $13
                    )
                    """,
                    review_id,
                    finding["agent_type"],
                    finding["severity"],
                    finding.get("category"),
                    finding["summary"],
                    finding.get("file_path"),
                    finding.get("line_start"),
                    finding.get("line_end"),
                    finding.get("suggestion"),
                    finding.get("confidence"),
                    finding.get("rationale"),
                    finding.get("agreed_by") or [],
                    finding.get("disputed", False),
                )

            await persist_agent_events(
                connection,
                installation_id=installation_id,
                review_id=review_id,
                agent_status=review_result.get("agent_status", {}),
                findings=findings,
            )

            await connection.execute(
                """
                UPDATE pr_review_records
                SET status = $2,
                    overall_confidence = $3,
                    updated_at = now()
                WHERE id = $1::uuid
                """,
                review_id,
                persisted_status,
                overall_confidence,
            )

            if requires_human_review:
                await connection.execute(
                    """
                    INSERT INTO hitl_reviews (
                        review_id, reason, status
                    )
                    VALUES ($1::uuid, $2, 'pending')
                    ON CONFLICT (review_id) WHERE status = 'pending'
                    DO UPDATE SET reason = EXCLUDED.reason
                    """,
                    review_id,
                    hitl_reason,
                )

    return persisted_status




def _actual_review_cost(
    graph_output: dict[str, Any],
    reserved_usd: Decimal,
) -> Decimal:
    """Calculate known costs, falling back conservatively if usage is missing."""

    statuses = graph_output.get("agent_status")
    if not isinstance(statuses, dict):
        return reserved_usd

    names = ("security", "quality", "tests", "docs")
    costs = []

    for name in names:
        record = statuses.get(name)
        if not isinstance(record, dict):
            return reserved_usd

        model = record.get("model")
        tokens_in = record.get("tokens_in")
        tokens_out = record.get("tokens_out")

        if (
            not isinstance(model, str)
            or not isinstance(tokens_in, int)
            or isinstance(tokens_in, bool)
            or not isinstance(tokens_out, int)
            or isinstance(tokens_out, bool)
        ):
            return reserved_usd

        try:
            costs.append(
                calculate_cost_usd(
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            )
        except ValueError:
            return reserved_usd

    return sum(costs, start=Decimal("0"))


async def run_review(ctx, job: dict) -> dict:
    """Fetch a PR, run the four specialists, and persist the result."""

    payload = validate_review_job(job)

    installation_id = payload["installation_id"]
    repo = payload["repo"]
    pr_number = payload["pr_number"]
    requested_head_sha = payload["head_sha"]
    delivery_id = payload["delivery_id"]

    configured_installation_id = settings.github_installation_id

    if configured_installation_id is None:
        raise RuntimeError(
            "GITHUB_INSTALLATION_ID is not configured."
        )

    # GitHubClient currently authenticates using the installation ID in
    # settings. Reject a mismatch rather than using the wrong installation.
    if installation_id != configured_installation_id:
        raise ValueError(
            "Webhook installation does not match the configured GitHub "
            "installation."
        )

    database = ctx.get("tiger_client")
    owns_database = database is None
    review_id: str | None = None
    budget_reservation = None
    budget_settled = False
    inbox_managed = payload["inbox_managed"]
    inbox_claimed = False
    inbox_attempts = 0

    if database is None:
        database = TimescaleClient()
        await database.connect()

    async def finish_inbox(status, error=None):
        if inbox_managed and inbox_claimed:
            await mark_delivery(
                database,
                delivery_id,
                status=status,
                last_error=error,
            )

    try:
        if inbox_managed:
            if not isinstance(delivery_id, str) or not delivery_id:
                raise ValueError(
                    "Inbox-managed job is missing its delivery ID."
                )

            claimed_delivery = await claim_delivery(
                database, delivery_id
            )

            if claimed_delivery is None:
                return {
                    "status": "inbox_not_claimed",
                    "repo": repo,
                    "pr_number": pr_number,
                    "auto_posted": False,
                }

            inbox_claimed = True
            inbox_attempts = claimed_delivery["attempts"]

        # Verify the live PR head before spending tokens or storing results.
        async with GitHubClient() as github:
            metadata = await github.get_pull_request(repo, pr_number)
            live_head_sha = (metadata.get("head") or {}).get("sha")

            if not isinstance(live_head_sha, str) or not live_head_sha:
                raise RuntimeError(
                    "GitHub returned no valid pull-request head SHA."
                )

            if live_head_sha != requested_head_sha:
                logger.info(
                    "Skipping superseded PR job for %s#%s.",
                    repo,
                    pr_number,
                )
                await finish_inbox("completed")
                return {
                    "status": "superseded",
                    "repo": repo,
                    "pr_number": pr_number,
                    "requested_head_sha": requested_head_sha,
                    "live_head_sha": live_head_sha,
                }

            diff = await github.get_pull_request_diff(repo, pr_number)
            changed_files = await github.get_pull_request_files(
                repo, pr_number
            )

        if not diff.strip():
            raise ValueError("The pull request has an empty diff.")

        # Mask likely secrets without changing line count, preserving diff
        # anchors used by finding validation.
        safe_diff = mask_secrets(diff)

        touched_paths = sorted({
            item["filename"]
            for item in changed_files
            if isinstance(item, dict)
            and isinstance(item.get("filename"), str)
            and item["filename"]
        })

        if not touched_paths:
            touched_paths = sorted(parse_diff(safe_diff).keys())

        # Avoid rerunning a completed review of this exact commit.
        existing = await database.fetch(
            """
            SELECT id::text AS id, status, updated_at
            FROM pr_review_records
            WHERE installation_id = $1
              AND repo = $2
              AND pr_number = $3
              AND head_sha = $4
            """,
            installation_id,
            repo,
            pr_number,
            requested_head_sha,
        )

        if existing:
            existing_id = existing[0]["id"]
            existing_status = existing[0]["status"]

            if existing_status == "running":
                # An atomic conditional update lets only one worker reclaim
                # a review that has been stuck for more than 15 minutes.
                claimed = await database.fetch(
                    """
                    UPDATE pr_review_records
                    SET delivery_id = $2, updated_at = now()
                    WHERE id = $1::uuid
                      AND status = 'running'
                      AND updated_at < now() - INTERVAL '15 minutes'
                    RETURNING id::text AS id
                    """,
                    existing_id,
                    delivery_id,
                )

                if not claimed:
                    await finish_inbox("completed")
                    return {
                        "status": "already_running",
                        "review_id": existing_id,
                        "repo": repo,
                        "pr_number": pr_number,
                    }

                review_id = claimed[0]["id"]

            elif existing_status == "failed":
                # Claim failed reviews atomically to prevent concurrent retry.
                claimed = await database.fetch(
                    """
                    UPDATE pr_review_records
                    SET status = 'running',
                        delivery_id = $2,
                        updated_at = now()
                    WHERE id = $1::uuid
                      AND status = 'failed'
                    RETURNING id::text AS id
                    """,
                    existing_id,
                    delivery_id,
                )

                if not claimed:
                    await finish_inbox("completed")
                    return {
                        "status": "already_running",
                        "review_id": existing_id,
                        "repo": repo,
                        "pr_number": pr_number,
                    }

                review_id = claimed[0]["id"]

            else:
                await finish_inbox("completed")
                return {
                    "status": "already_processed",
                    "review_id": existing_id,
                    "previous_status": existing_status,
                    "repo": repo,
                    "pr_number": pr_number,
                }

        else:
            # Create the first review record for this PR head.
            # The unique constraint protects concurrent duplicate jobs.
            inserted = await database.fetch(
                """
                INSERT INTO pr_review_records (
                    installation_id,
                    repo,
                    pr_number,
                    head_sha,
                    delivery_id,
                    status
                )
                VALUES ($1, $2, $3, $4, $5, 'running')
                ON CONFLICT (
                    installation_id, repo, pr_number, head_sha
                ) DO NOTHING
                RETURNING id::text AS id
                """,
                installation_id,
                repo,
                pr_number,
                requested_head_sha,
                delivery_id,
            )

            if not inserted:
                await finish_inbox("completed")
                return {
                    "status": "duplicate",
                    "repo": repo,
                    "pr_number": pr_number,
                }

            review_id = inserted[0]["id"]

        # Reserve the conservative maximum before any specialist calls.
        budget_reservation = await reserve_review_budget(
            database, review_id
        )

        if not budget_reservation.allowed:
            await database.execute(
                """
                UPDATE pr_review_records
                SET status = 'failed', updated_at = now()
                WHERE id = $1::uuid
                  AND status = 'running'
                  AND delivery_id IS NOT DISTINCT FROM $2
                """,
                review_id,
                delivery_id,
            )
            logger.warning(
                "Review %s was not started because budget reservation "
                "was denied (%s).",
                review_id,
                budget_reservation.reason,
            )
            await finish_inbox(
                "failed",
                f"Budget reservation denied: {budget_reservation.reason}",
            )
            return {
                "status": "budget_exceeded",
                "review_id": review_id,
                "repo": repo,
                "pr_number": pr_number,
                "reason": budget_reservation.reason,
                "auto_posted": False,
            }

        initial_state = {
            "review_id": review_id,
            "installation_id": installation_id,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": requested_head_sha,
            "head_sha_verified": True,
            "diff": safe_diff,
            "touched_paths": touched_paths,
        }

        graph = build_review_graph()
        graph_output = await graph.ainvoke(initial_state)

        review_result = graph_output.get("result")
        if not isinstance(review_result, dict):
            raise RuntimeError(
                "Review graph returned no valid aggregate result."
            )

        actual_cost = _actual_review_cost(
            graph_output,
            budget_reservation.reserved_usd,
        )
        await settle_review_budget(
            database,
            review_id,
            actual_cost,
        )
        budget_settled = True

        persisted_status = await _persist_review(
            database,
            review_id=review_id,
            installation_id=installation_id,
            review_result=review_result,
        )

        logger.info(
            "Review completed for %s#%s with status %s.",
            repo,
            pr_number,
            persisted_status,
        )

        await finish_inbox("completed")
        return {
            "status": persisted_status,
            "review_id": review_id,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": requested_head_sha,
            "finding_count": len(review_result.get("findings", [])),
            "retrieval_status": graph_output.get(
                "retrieval_status", "unknown"
            ),
            "auto_posted": False,
        }

    except Exception as exc:
        logger.error(
            "Review worker failed for %s#%s with %s.",
            repo,
            pr_number,
            type(exc).__name__,
        )

        if inbox_claimed:
            inbox_status = (
                "dead_letter"
                if inbox_attempts >= MAX_DELIVERY_ATTEMPTS
                else "failed"
            )
            try:
                await finish_inbox(inbox_status, type(exc).__name__)
            except Exception as inbox_exc:
                logger.error(
                    "Could not update webhook inbox status (%s).",
                    type(inbox_exc).__name__,
                )

        # If execution failed after a reservation was secured and before
        # settlement, account conservatively for the full reserved amount.
        if (
            review_id is not None
            and budget_reservation is not None
            and budget_reservation.allowed
            and not budget_settled
        ):
            try:
                await settle_review_budget(
                    database,
                    review_id,
                    budget_reservation.reserved_usd,
                )
                budget_settled = True
            except Exception as budget_exc:
                logger.error(
                    "Could not settle failed review budget (%s).",
                    type(budget_exc).__name__,
                )

        if review_id is not None:
            try:
                await database.execute(
                    """
                    UPDATE pr_review_records
                    SET status = 'failed', updated_at = now()
                    WHERE id = $1::uuid
                      AND status = 'running'
                      AND delivery_id IS NOT DISTINCT FROM $2
                    """,
                    review_id,
                    delivery_id,
                )
            except Exception as status_exc:
                logger.error(
                    "Could not update failed review status (%s).",
                    type(status_exc).__name__,
                )

        raise

    finally:
        if owns_database:
            await database.close()


async def reindex_repo(ctx, job: dict) -> dict:
    """Reindex the current default-branch snapshot after a push."""

    if not isinstance(job, dict):
        raise ValueError("Reindex job must be a dictionary.")

    installation_id = job.get("installation_id")
    repo = job.get("repo")
    ref = job.get("ref")
    head_sha = job.get("head_sha")
    delivery_id = job.get("delivery_id")
    inbox_managed = job.get("inbox_managed") is True

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
    ):
        raise ValueError("Invalid reindex installation ID.")

    if (
        not isinstance(repo, str)
        or repo.count("/") != 1
        or any(not part for part in repo.split("/"))
    ):
        raise ValueError("Invalid reindex repository name.")

    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        raise ValueError("Reindex job must target a branch ref.")

    if (
        not isinstance(head_sha, str)
        or len(head_sha) != 40
        or any(c not in "0123456789abcdefABCDEF" for c in head_sha)
    ):
        raise ValueError("Invalid reindex commit SHA.")

    configured_installation_id = settings.github_installation_id
    if configured_installation_id is None:
        raise RuntimeError("GITHUB_INSTALLATION_ID is not configured.")
    if installation_id != configured_installation_id:
        raise ValueError(
            "Webhook installation does not match the configured installation."
        )

    database = ctx.get("tiger_client")
    owns_database = database is None
    inbox_claimed = False
    inbox_attempts = 0

    if database is None:
        database = TimescaleClient()
        await database.connect()

    async def finish_inbox(status: str, error: str | None = None):
        if inbox_managed and inbox_claimed:
            await mark_delivery(
                database,
                delivery_id,
                status=status,
                last_error=error,
            )

    try:
        if inbox_managed:
            if not isinstance(delivery_id, str) or not delivery_id:
                raise ValueError(
                    "Inbox-managed reindex job requires a delivery ID."
                )

            claimed = await claim_delivery(database, delivery_id)
            if claimed is None:
                return {
                    "status": "inbox_not_claimed",
                    "repo": repo,
                    "auto_posted": False,
                }

            inbox_claimed = True
            inbox_attempts = claimed["attempts"]

        # Ensure this ref is still the default branch before indexing.
        async with GitHubClient() as github:
            metadata = await github.get_repository(repo)

        default_branch = metadata.get("default_branch")
        expected_ref = (
            f"refs/heads/{default_branch}"
            if isinstance(default_branch, str) and default_branch
            else None
        )

        if ref != expected_ref:
            await finish_inbox("ignored", "Push no longer targets default branch")
            return {
                "status": "ignored_not_default_branch",
                "repo": repo,
                "ref": ref,
                "auto_posted": False,
            }

        # The indexer fetches the current default-branch snapshot itself.
        # It may be newer than this event's `after` SHA; indexing the latest
        # snapshot avoids leaving the index behind after rapid successive pushes.
        result = await index_repository(
            repo,
            installation_id=installation_id,
        )

        # Confirm the branch is still the default branch after indexing.
        async with GitHubClient() as github:
            latest_metadata = await github.get_repository(repo)

        latest_default = latest_metadata.get("default_branch")
        if result.branch != latest_default:
            raise RuntimeError(
                "Default branch changed during repository indexing; retry needed."
            )

        await finish_inbox("completed")

        logger.info(
            "Repository reindex completed for %s at %s.",
            repo,
            result.commit_sha,
        )

        return {
            "status": "reindexed",
            "repo": repo,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
            "files_seen": result.files_seen,
            "files_eligible": result.files_eligible,
            "files_indexed": result.files_indexed,
            "files_skipped_unchanged": result.files_skipped_unchanged,
            "files_filtered": result.files_filtered,
            "chunks_written": result.chunks_written,
            "auto_posted": False,
        }

    except Exception as exc:
        logger.error(
            "Repository reindex failed for %s with %s.",
            repo,
            type(exc).__name__,
        )

        if inbox_claimed:
            retry_status = (
                "dead_letter"
                if inbox_attempts >= MAX_DELIVERY_ATTEMPTS
                else "failed"
            )
            try:
                await finish_inbox(retry_status, type(exc).__name__)
            except Exception as inbox_exc:
                logger.error(
                    "Could not update reindex inbox status (%s).",
                    type(inbox_exc).__name__,
                )

        raise

    finally:
        if owns_database:
            await database.close()


async def startup(ctx):
    """Create shared database resources when the worker starts."""

    client = TimescaleClient()

    try:
        await client.connect()
    except BaseException:
        await client.close()
        raise

    ctx["tiger_client"] = client
    ctx["db_pool"] = client.pool


async def shutdown(ctx):
    """Release shared database resources when the worker stops."""

    client = ctx.get("tiger_client")

    try:
        if client is not None:
            await client.close()
    finally:
        ctx.pop("tiger_client", None)
        ctx.pop("db_pool", None)


class WorkerSettings:
    functions = [demo_job, run_review, reindex_repo]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    job_timeout = 300
    max_jobs = 5

    # Recover pending, failed and stale webhook deliveries every five minutes.
    cron_jobs = [
        cron(
            dispatch_pending_webhooks,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            unique=True,
        )
    ]
