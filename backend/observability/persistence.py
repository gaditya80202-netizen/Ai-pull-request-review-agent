"""Persist safe per-agent execution telemetry."""

import json
from statistics import fmean
from typing import Any
from uuid import UUID, uuid4

from backend.economics.pricing import calculate_cost_usd


AGENT_NAMES = ("security", "quality", "tests", "docs")


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _safe_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value[:200]
    return None


def _agent_confidence(findings: list[dict[str, Any]], agent: str):
    values = []

    for finding in findings:
        if not isinstance(finding, dict):
            continue

        agreed_by = finding.get("agreed_by") or []
        if finding.get("agent_type") != agent and agent not in agreed_by:
            continue

        value = finding.get("confidence")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= value <= 1
        ):
            values.append(float(value))

    return fmean(values) if values else None


async def persist_agent_events(
    connection,
    *,
    installation_id: int,
    review_id: str,
    agent_status: dict[str, Any],
    findings: list[dict[str, Any]],
) -> int:
    """Write one event per specialist inside the caller's transaction.

    Costs are estimates based on the configured conservative token rates.
    If model pricing or token usage is unavailable, cost_usd is NULL.
    """

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
    ):
        raise ValueError("installation_id must be a positive integer")

    try:
        parent_span = UUID(str(review_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("review_id must be a valid UUID") from None

    if not isinstance(agent_status, dict):
        raise ValueError("agent_status must be a dictionary")

    if not isinstance(findings, list):
        raise ValueError("findings must be a list")

    sql = """
        INSERT INTO agent_events (
            ts,
            installation_id,
            review_id,
            agent,
            span_id,
            parent_span,
            event_type,
            model,
            prompt_version,
            tokens_in,
            tokens_out,
            cost_usd,
            latency_ms,
            outcome,
            confidence,
            payload
        )
        VALUES (
            now(), $1, $2::uuid, $3, $4::uuid, $5::uuid,
            $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb
        )
    """

    written = 0

    for agent in AGENT_NAMES:
        status = agent_status.get(agent)
        if not isinstance(status, dict):
            status = {"status": "not_reported"}

        model = _safe_text(status.get("model"))
        prompt_version = _safe_text(status.get("prompt_version"))
        tokens_in = _nonnegative_int(status.get("tokens_in"))
        tokens_out = _nonnegative_int(status.get("tokens_out"))
        latency_ms = _nonnegative_int(status.get("latency_ms"))
        outcome = _safe_text(status.get("status")) or "unknown"

        cost = None
        cost_basis = "unavailable"

        if model and tokens_in is not None and tokens_out is not None:
            try:
                cost = calculate_cost_usd(
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
                cost_basis = "estimated_from_provider_tokens"
            except ValueError:
                cost_basis = "unknown_model_pricing"

        payload = {
            "error_type": _safe_text(status.get("error_type")),
            "accepted_count": _nonnegative_int(
                status.get("accepted_count")
            ),
            "rejected_count": _nonnegative_int(
                status.get("rejected_count")
            ),
            "cost_basis": cost_basis,
            "usage_available": (
                model is not None
                and tokens_in is not None
                and tokens_out is not None
            ),
        }

        await connection.execute(
            sql,
            installation_id,
            parent_span,
            agent,
            uuid4(),
            parent_span,
            "agent_run",
            model,
            prompt_version,
            tokens_in,
            tokens_out,
            cost,
            latency_ms,
            outcome,
            _agent_confidence(findings, agent),
            json.dumps(payload, separators=(",", ":")),
        )

        written += 1

    return written
