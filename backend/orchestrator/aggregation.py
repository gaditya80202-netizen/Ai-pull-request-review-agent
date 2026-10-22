from __future__ import annotations

from typing import Any

from backend.core.settings import settings
from backend.orchestrator.validation import (
    validate_and_deduplicate_findings,
)


AGENT_NAMES = ("security", "quality", "tests", "docs")


def aggregate_review(state: dict[str, Any]) -> dict[str, Any]:
    raw_findings = list(state.get("findings", []))
    rejected = list(state.get("rejected_findings", []))
    statuses = state.get("agent_status", {})
    diff = state.get("diff", "")

    findings, central_rejections = validate_and_deduplicate_findings(
        raw_findings,
        diff,
        allowed_paths=state.get("touched_paths"),
    )
    rejected.extend(central_rejections)

    incomplete_agents = [
        name
        for name in AGENT_NAMES
        if statuses.get(name, {}).get("status") != "success"
    ]

    critical_count = sum(
        item["severity"] == "CRITICAL" for item in findings
    )

    low_confidence_count = sum(
        float(item["confidence"]) < settings.confidence_threshold
        for item in findings
    )

    head_sha_verified = state.get("head_sha_verified") is True
    evaluation_passed = settings.evaluation_gate_passed
    auto_post_enabled = settings.auto_post_enabled

    reasons: list[str] = []

    if incomplete_agents:
        reasons.append("One or more specialist agents did not succeed.")

    if rejected:
        reasons.append("One or more findings failed validation.")

    if critical_count:
        reasons.append("A CRITICAL finding requires human approval.")

    if low_confidence_count:
        reasons.append(
            "One or more findings fall below the configured confidence threshold."
        )

    if not head_sha_verified:
        reasons.append(
            "The current pull-request head SHA has not been verified by the caller."
        )

    if not evaluation_passed:
        reasons.append("The evaluation gate has not passed.")

    if not auto_post_enabled:
        reasons.append("Automatic posting is disabled.")

    if not findings:
        reasons.append(
            "An empty findings list is not yet sufficient evidence of a clean review."
        )

    # Conservative gate: all conditions must pass before a result is
    # even marked eligible for automated posting. This module does not
    # itself post a review.
    auto_post_allowed = (
        bool(findings)
        and not incomplete_agents
        and not rejected
        and critical_count == 0
        and low_confidence_count == 0
        and head_sha_verified
        and evaluation_passed
        and auto_post_enabled
    )

    blocking_issues = (
        bool(incomplete_agents)
        or bool(rejected)
        or critical_count > 0
        or low_confidence_count > 0
        or not findings
    )

    if auto_post_allowed:
        status = "eligible_for_automated_post"
    elif blocking_issues:
        status = "human_review_required"
    else:
        status = "awaiting_human_approval"

    return {
        "result": {
            "status": status,
            "requires_human_review": not auto_post_allowed,
            "auto_post_allowed": auto_post_allowed,
            "finding_count": len(findings),
            "raw_finding_count": len(raw_findings),
            "deduplicated_count": len(raw_findings) - len(findings),
            "rejected_finding_count": len(rejected),
            "critical_finding_count": critical_count,
            "low_confidence_count": low_confidence_count,
            "incomplete_agents": incomplete_agents,
            "agent_status": statuses,
            "gate_reasons": reasons,
            "findings": findings,
            "raw_findings": raw_findings,
            "rejected_findings": rejected,
        }
    }
