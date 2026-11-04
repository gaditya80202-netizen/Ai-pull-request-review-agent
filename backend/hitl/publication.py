"""Guarded publication of a human-approved AI pull-request review.

This module is called only by the authenticated HITL decision endpoint.
It never creates commits, edits files, pushes branches, merges PRs, or
submits GitHub's APPROVE event. It posts one COMMENT review after
rechecking the live PR head SHA.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from backend.core.settings import settings
from backend.integrations.github_client import GitHubClient
from backend.security.masking import mask_secrets

logger = logging.getLogger(__name__)

_SUCCESS_STATUSES = {"published"}


def _normalise_uuid(value: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("hitl_review_id must be a valid UUID") from None


def _safe_text(value: Any, limit: int = 6000) -> str:
    if not isinstance(value, str):
        return ""
    return mask_secrets(value.strip())[:limit]


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _diff_right_lines(patch: Any) -> set[int]:
    """Return new-file line numbers represented in a GitHub diff patch."""
    if not isinstance(patch, str) or not patch:
        return set()

    result: set[int] = set()
    new_line: int | None = None
    hunk_pattern = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    for line in patch.splitlines():
        match = hunk_pattern.match(line)
        if match:
            new_line = int(match.group(1))
            continue

        if new_line is None or line.startswith("\\"):
            continue

        if line.startswith("+") and not line.startswith("+++"):
            result.add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith(" "):
            result.add(new_line)
            new_line += 1

    return result


def _location(
    finding: dict[str, Any],
    changed_lines: dict[str, set[int]],
) -> tuple[str, int, int] | None:
    """Return an inline location only when its complete range is in the diff."""
    path = finding.get("file_path")
    start = finding.get("line_start")
    end = finding.get("line_end")

    if not isinstance(path, str) or not path.strip() or path != path.strip():
        return None

    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or path not in changed_lines:
        return None

    if not _is_positive_int(start):
        return None
    if end is None:
        end = start
    if not _is_positive_int(end) or end < start:
        return None

    available_lines = changed_lines[path]
    if any(line not in available_lines for line in range(start, end + 1)):
        return None

    return path, start, end


def _review_payload(
    review_id: str,
    head_sha: str,
    findings: list[dict[str, Any]],
    changed_lines: dict[str, set[int]],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Format GitHub's review body, event and inline comments safely."""
    marker = f"<!-- ai-pr-review-id:{review_id} -->"
    event = "COMMENT"
    comments: list[dict[str, Any]] = []
    body_only: list[str] = []

    for finding in findings:
        severity = _safe_text(finding.get("severity") or "UNSPECIFIED", 40).upper()
        summary = _safe_text(finding.get("summary") or "Finding without a summary", 1500)
        rationale = _safe_text(finding.get("rationale"), 2500)
        suggestion = _safe_text(finding.get("suggestion"), 2500)
        parts = [f"**{severity}** — {summary}"]
        if rationale:
            parts.append(f"**Rationale:** {rationale}")
        if suggestion:
            parts.append(f"**Suggested fix:** {suggestion}")
        comment_body = "\n\n".join(parts)
        location = _location(finding, changed_lines)
        if location is None:
            body_only.append(f"- **{severity}** — {summary}")
            if rationale:
                body_only.append(f"  - Rationale: {rationale}")
            if suggestion:
                body_only.append(f"  - Suggested fix: {suggestion}")
            continue

        path, start, end = location
        comment: dict[str, Any] = {"path": path, "body": comment_body, "side": "RIGHT", "line": end}
        if end > start:
            comment["start_line"] = start
            comment["start_side"] = "RIGHT"
        comments.append(comment)

    body_parts = [
        "## AI pull-request review (human-approved)",
        f"Reviewed commit: `{head_sha}`.",
        "This is a review comment or change request, not an approval of the pull request.",
    ]
    if body_only:
        body_parts.extend(["### Findings without a safe inline location", "\n".join(body_only)])
    body_parts.append(marker)
    return "\n\n".join(body_parts), event, comments


def _result(
    publication_status: str,
    message: str,
    *,
    review_id: str | None = None,
    github_review_id: int | None = None,
    event: str | None = None,
    already_published: bool = False,
) -> dict[str, Any]:
    return {
        "publication_status": publication_status,
        "publication_allowed": publication_status in _SUCCESS_STATUSES,
        "publication_event": event,
        "github_review_id": github_review_id,
        "already_published": already_published,
        "review_id": review_id,
        "message": message,
    }


async def _set_publication(
    database: Any,
    review_id: str,
    publication_status: str,
    *,
    event: str | None = None,
    github_review_id: int | None = None,
    error: str | None = None,
    review_status: str | None = None,
) -> None:
    await database.execute(
        """
        UPDATE pr_review_records
        SET publication_status = $2,
            publication_event = COALESCE($3, publication_event),
            github_review_id = COALESCE($4, github_review_id),
            publication_error = $5,
            status = COALESCE($6, status),
            updated_at = now()
        WHERE id = $1::uuid
        """,
        review_id,
        publication_status,
        event,
        github_review_id,
        (error[:500] if error else None),
        review_status,
    )


def _marked_review(reviews: list[dict[str, Any]], marker: str, head_sha: str) -> dict[str, Any] | None:
    for item in reviews:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        commit_id = item.get("commit_id")
        if isinstance(body, str) and marker in body and commit_id == head_sha:
            return item
    return None


async def publish_approved_review(database: Any, *, hitl_review_id: str) -> dict[str, Any]:
    """Publish one approved review, with stale-SHA and duplicate safeguards.

    The database claim is a single atomic UPDATE. If the POST's outcome is
    uncertain, this function records that uncertainty and does not retry.
    """
    hitl_review_id = _normalise_uuid(hitl_review_id)

    claimed = await database.fetch(
        """
        UPDATE pr_review_records AS r
        SET publication_status = 'publishing',
            publication_attempted_at = now(),
            publication_error = NULL,
            updated_at = now()
        WHERE r.id = (
            SELECT h.review_id
            FROM hitl_reviews AS h
            WHERE h.id = $1::uuid AND h.status = 'approved'
        )
          AND r.status = 'human_approved'
          AND r.publication_status = 'not_attempted'
        RETURNING
            r.id::text AS review_id,
            r.installation_id,
            r.repo,
            r.pr_number,
            r.head_sha,
            r.status AS review_status,
            r.publication_status,
            r.github_review_id,
            r.overall_confidence
        """,
        hitl_review_id,
    )

    if not claimed:
        current_rows = await database.fetch(
            """
            SELECT
                r.id::text AS review_id,
                r.publication_status,
                r.github_review_id,
                r.status AS review_status,
                h.status AS hitl_status,
                r.publication_event
            FROM hitl_reviews AS h
            JOIN pr_review_records AS r ON r.id = h.review_id
            WHERE h.id = $1::uuid
            """,
            hitl_review_id,
        )
        if not current_rows:
            return _result("not_found", "Human-review request was not found.")
        current = dict(current_rows[0])
        status = current.get("publication_status") or "not_attempted"
        review_id = current.get("review_id")
        github_review_id = current.get("github_review_id")
        event = current.get("publication_event")
        if status == "published":
            return _result(
                "published", "This review has already been published; no duplicate was created.",
                review_id=review_id, github_review_id=github_review_id,
                event=event, already_published=True,
            )
        if current.get("hitl_status") != "approved":
            return _result(
                "not_approved", "The human-review request is not approved; nothing was published.",
                review_id=review_id,
            )
        return _result(
            status, "Publication was not retried because an attempt is already recorded.",
            review_id=review_id, github_review_id=github_review_id, event=event,
        )

    review = dict(claimed[0])
    review_id = review["review_id"]
    repo = review["repo"]
    pr_number = int(review["pr_number"])
    expected_sha = review["head_sha"]
    marker = f"<!-- ai-pr-review-id:{review_id} -->"
    post_started = False
    event: str | None = None

    configured_installation_id = settings.github_installation_id
    if configured_installation_id is None or int(review["installation_id"]) != int(configured_installation_id):
        reason = "Review installation does not match the configured GitHub App installation."
        await _set_publication(database, review_id, "failed_before_post", error=reason)
        return _result("failed_before_post", reason, review_id=review_id)

    try:
        async with GitHubClient() as github:
            metadata = await github.get_pull_request(repo, pr_number)
            live_sha = (metadata.get("head") or {}).get("sha")
            if not isinstance(live_sha, str) or live_sha != expected_sha:
                reason = "The pull-request head changed after review; publication was blocked as stale."
                await _set_publication(database, review_id, "blocked_stale", error=reason)
                return _result("blocked_stale", reason, review_id=review_id)

            changed_files = await github.get_pull_request_files(repo, pr_number)
            changed_lines = {
                item["filename"]: _diff_right_lines(item.get("patch"))
                for item in changed_files
                if isinstance(item, dict)
                and isinstance(item.get("filename"), str)
            }
            finding_rows = await database.fetch(
                """
                SELECT severity, summary, file_path, line_start, line_end,
                       suggestion, confidence, rationale
                FROM finding_records
                WHERE review_id = $1::uuid
                ORDER BY severity, file_path NULLS LAST, line_start NULLS LAST
                """,
                review_id,
            )
            findings = [dict(item) for item in finding_rows]
            if not findings:
                reason = "No validated findings are stored; an empty review was not published."
                await _set_publication(database, review_id, "blocked_no_findings", error=reason)
                return _result("blocked_no_findings", reason, review_id=review_id)

            body, event, comments = _review_payload(review_id, expected_sha, findings, changed_lines)
            await _set_publication(database, review_id, "publishing", event=event)

            existing = _marked_review(
                await github.get_pull_request_reviews(repo, pr_number), marker, expected_sha
            )
            if existing is not None:
                raw_id = existing.get("id")
                existing_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else None
                await _set_publication(
                    database, review_id, "published", event=event,
                    github_review_id=existing_id, error=None, review_status="published",
                )
                return _result(
                    "published", "A matching GitHub review already exists; the record was reconciled.",
                    review_id=review_id, github_review_id=existing_id,
                    event=event, already_published=True,
                )

            # A second SHA check is deliberately the last GitHub read before POST.
            latest = await github.get_pull_request(repo, pr_number)
            latest_sha = (latest.get("head") or {}).get("sha")
            if not isinstance(latest_sha, str) or latest_sha != expected_sha:
                reason = "The pull-request head changed immediately before publication; publication was blocked."
                await _set_publication(database, review_id, "blocked_stale", event=event, error=reason)
                return _result("blocked_stale", reason, review_id=review_id, event=event)

            post_started = True
            posted = await github.post_review(
                repo,
                pr_number,
                expected_sha,
                body,
                event=event,
                comments=comments or None,
            )
            raw_id = posted.get("id") if isinstance(posted, dict) else None
            if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
                reason = "GitHub returned no usable review ID; publication outcome needs reconciliation."
                await _set_publication(database, review_id, "outcome_unknown", event=event, error=reason)
                return _result("outcome_unknown", reason, review_id=review_id, event=event)

            await _set_publication(
                database, review_id, "published", event=event,
                github_review_id=raw_id, error=None, review_status="published",
            )
            logger.info("Published human-approved review %s to %s#%s.", review_id, repo, pr_number)
            return _result(
                "published", "The human-approved review was published to GitHub.",
                review_id=review_id, github_review_id=raw_id, event=event,
            )

    except Exception as exc:  # noqa: BLE001
        # If POST began, never blindly retry. First try to reconcile the unique marker.
        if post_started:
            try:
                async with GitHubClient() as reconcile_client:
                    reviews = await reconcile_client.get_pull_request_reviews(repo, pr_number)
                found = _marked_review(reviews, marker, expected_sha)
                if found is not None:
                    raw_id = found.get("id")
                    found_id = raw_id if isinstance(raw_id, int) and raw_id > 0 else None
                    await _set_publication(
                        database, review_id, "published", event=event,
                        github_review_id=found_id, error=None, review_status="published",
                    )
                    return _result(
                        "published", "The GitHub review was reconciled after an uncertain response.",
                        review_id=review_id, github_review_id=found_id,
                        event=event, already_published=True,
                    )
            except Exception as reconcile_exc:  # noqa: BLE001
                logger.warning("Could not reconcile publication for review %s (%s).", review_id, type(reconcile_exc).__name__)
            status = "outcome_unknown"
            message = "GitHub publication outcome is uncertain. The POST will not be retried automatically; inspect the PR before taking further action."
        else:
            status = "failed_before_post"
            message = "Publication failed before the GitHub review POST; no automatic retry was attempted."
        logger.warning("Publication for review %s stopped with %s.", review_id, type(exc).__name__)
        await _set_publication(database, review_id, status, event=event, error=f"{type(exc).__name__}: {message}")
        return _result(status, message, review_id=review_id, event=event)

async def reconcile_approved_review(database: Any, *, hitl_review_id: str) -> dict[str, Any]:
    """Reconcile an approved review against GitHub without issuing a POST."""
    hitl_review_id = _normalise_uuid(hitl_review_id)

    rows = await database.fetch(
        """
        SELECT
            r.id::text AS review_id,
            r.installation_id,
            r.repo,
            r.pr_number,
            r.head_sha,
            r.status AS review_status,
            r.publication_status,
            r.github_review_id,
            r.publication_event,
            h.status AS hitl_status
        FROM hitl_reviews AS h
        JOIN pr_review_records AS r ON r.id = h.review_id
        WHERE h.id = $1::uuid
        """,
        hitl_review_id,
    )

    if not rows:
        return _result("not_found", "Human-review request was not found.")

    review = dict(rows[0])
    review_id = review["review_id"]
    status = review.get("publication_status") or "not_attempted"

    if review.get("hitl_status") != "approved" or review.get("review_status") not in {
        "human_approved", "published"
    }:
        return _result(
            "not_approved",
            "The review is not approved for publication reconciliation.",
            review_id=review_id,
        )

    if status == "published":
        return _result(
            "published",
            "The review is already marked as published.",
            review_id=review_id,
            github_review_id=review.get("github_review_id"),
            event=review.get("publication_event"),
            already_published=True,
        )

    if status not in {"publishing", "outcome_unknown"}:
        return _result(
            status,
            "Reconciliation is only available for interrupted or uncertain publication attempts.",
            review_id=review_id,
            github_review_id=review.get("github_review_id"),
            event=review.get("publication_event"),
        )

    configured_installation_id = settings.github_installation_id
    if (
        configured_installation_id is None
        or int(review["installation_id"]) != int(configured_installation_id)
    ):
        return _result(
            status,
            "The GitHub App installation does not match; reconciliation was not attempted.",
            review_id=review_id,
            event=review.get("publication_event"),
        )

    marker = f"<!-- ai-pr-review-id:{review_id} -->"
    repo = review["repo"]
    pr_number = int(review["pr_number"])
    expected_sha = review["head_sha"]

    try:
        async with GitHubClient() as github:
            metadata = await github.get_pull_request(repo, pr_number)
            live_sha = (metadata.get("head") or {}).get("sha")

            if not isinstance(live_sha, str) or live_sha != expected_sha:
                return _result(
                    "blocked_stale",
                    "The PR head changed; publication reconciliation was blocked.",
                    review_id=review_id,
                    event=review.get("publication_event"),
                )

            reviews = await github.get_pull_request_reviews(repo, pr_number)

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Reconciliation failed for review %s (%s).",
            review_id,
            type(exc).__name__,
        )
        return _result(
            status,
            "GitHub could not be checked. The publication outcome remains unresolved; no POST was issued.",
            review_id=review_id,
            event=review.get("publication_event"),
        )

    found = _marked_review(reviews, marker, expected_sha)
    if found is None:
        return _result(
            "outcome_unknown",
            "No matching review was found. This does not prove the POST failed; no retry was issued.",
            review_id=review_id,
            event=review.get("publication_event"),
        )

    raw_id = found.get("id")
    found_id = raw_id if _is_positive_int(raw_id) else None

    await _set_publication(
        database,
        review_id,
        "published",
        event=review.get("publication_event"),
        github_review_id=found_id,
        error=None,
        review_status="published",
    )

    return _result(
        "published",
        "The existing GitHub review was found and reconciled; no new review was posted.",
        review_id=review_id,
        github_review_id=found_id,
        event=review.get("publication_event"),
        already_published=True,
    )
