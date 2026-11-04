from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from backend.integrations.diff_parser import parse_diff

SEVERITY_RANK = {
    "INFO": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

VALID_AGENTS = {"security", "quality", "tests", "docs"}


def _is_safe_repo_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path:
        return False

    parts = path.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def validate_and_deduplicate_findings(
    findings: Iterable[dict[str, Any]],
    diff: str,
    *,
    allowed_paths: Iterable[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate finding locations and merge exact duplicate reports.

    This validates diff anchors, not whether a reported bug is real.
    The raw input is retained separately by the graph for audit purposes.
    """
    changed_lines = parse_diff(diff)

    if allowed_paths is None:
        permitted_paths = set(changed_lines)
    else:
        permitted_paths = set(allowed_paths)

    merged: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    rejected: list[str] = []

    for index, raw in enumerate(findings, start=1):
        finding = dict(raw)

        path = finding.get("file_path")
        line_start = finding.get("line_start")
        line_end = finding.get("line_end")
        severity = finding.get("severity")
        category = finding.get("category")
        summary = finding.get("summary")
        confidence = finding.get("confidence")
        agent = finding.get("agent_type")

        prefix = f"Finding {index}"

        if not isinstance(path, str) or not _is_safe_repo_path(path):
            rejected.append(f"{prefix}: invalid repository path.")
            continue

        if path not in permitted_paths:
            rejected.append(f"{prefix}: file is not in the PR file list.")
            continue

        if not isinstance(line_start, int) or isinstance(line_start, bool):
            rejected.append(f"{prefix}: invalid line_start.")
            continue

        if line_start not in changed_lines.get(path, set()):
            rejected.append(
                f"{prefix}: {path}:{line_start} is not an added line in the diff."
            )
            continue

        if line_end is not None and (
            not isinstance(line_end, int)
            or isinstance(line_end, bool)
            or line_end < line_start
        ):
            rejected.append(f"{prefix}: invalid line range.")
            continue

        if severity not in SEVERITY_RANK:
            rejected.append(f"{prefix}: invalid severity.")
            continue

        if not isinstance(category, str) or not category.strip():
            rejected.append(f"{prefix}: missing category.")
            continue

        if not isinstance(summary, str) or not summary.strip():
            rejected.append(f"{prefix}: missing summary.")
            continue

        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            rejected.append(f"{prefix}: invalid confidence.")
            continue

        if agent not in VALID_AGENTS:
            rejected.append(f"{prefix}: invalid agent identity.")
            continue

        normalized_summary = " ".join(summary.casefold().split())
        normalized_category = " ".join(category.casefold().split())

        key = (
            path,
            line_start,
            normalized_category,
            normalized_summary,
        )

        existing = merged.get(key)

        if existing is None:
            finding["agreed_by"] = [agent]
            merged[key] = finding
            continue

        # Merge only findings with the same path, line, category and
        # normalized summary. Retain the strongest reported severity.
        if SEVERITY_RANK[severity] > SEVERITY_RANK[existing["severity"]]:
            existing["severity"] = severity

        existing["confidence"] = max(
            float(existing["confidence"]), float(confidence)
        )
        existing["agreed_by"] = sorted(
            set(existing["agreed_by"]) | {agent}
        )

    ordered = sorted(
        merged.values(),
        key=lambda item: (
            item["file_path"],
            item["line_start"],
            item["category"].casefold(),
            " ".join(item["summary"].casefold().split()),
        ),
    )

    return ordered, rejected
