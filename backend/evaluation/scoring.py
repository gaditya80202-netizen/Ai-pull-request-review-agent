"""Score specialist findings against explicit evaluation expectations."""

from typing import Any

from backend.evaluation.cases import EvaluationCase


def _finding_dict(finding: Any) -> dict[str, Any]:
    if isinstance(finding, dict):
        return finding

    if hasattr(finding, "model_dump"):
        return finding.model_dump(mode="json")

    raise ValueError("Evaluation finding has an unsupported representation.")


def score_case(
    case: EvaluationCase,
    findings: list[Any],
) -> dict[str, Any]:
    """Score one case using its expected file and added-line anchor."""

    normalized = [_finding_dict(item) for item in findings]

    anchored_index = None
    if case.expect_finding:
        for index, finding in enumerate(normalized):
            if (
                finding.get("file_path") == case.expected_path
                and finding.get("line_start") == case.expected_line
            ):
                anchored_index = index
                break

    detected = anchored_index is not None
    false_negative = case.expect_finding and not detected
    true_positive = case.expect_finding and detected
    true_negative = not case.expect_finding and not normalized

    # On a positive case, one correctly anchored finding earns the TP.
    # Other findings are counted conservatively as potential false positives.
    false_positives = len(normalized) - (
        1 if anchored_index is not None else 0
    )

    return {
        "case_id": case.case_id,
        "agent": case.agent,
        "expect_finding": case.expect_finding,
        "expected_path": case.expected_path,
        "expected_line": case.expected_line,
        "detected_expected_issue": detected,
        "finding_count": len(normalized),
        "true_positive": int(bool(true_positive)),
        "false_negative": int(bool(false_negative)),
        "true_negative": int(bool(true_negative)),
        "false_positive_count": false_positives,
        "findings": normalized,
    }


def summarize_scores(
    scored_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    tp = sum(item["true_positive"] for item in scored_cases)
    fn = sum(item["false_negative"] for item in scored_cases)
    tn = sum(item["true_negative"] for item in scored_cases)
    fp = sum(item["false_positive_count"] for item in scored_cases)

    positive_cases = tp + fn
    clean_cases = sum(
        1 for item in scored_cases if not item["expect_finding"]
    )

    precision_denominator = tp + fp

    return {
        "cases_total": len(scored_cases),
        "true_positives": tp,
        "false_negatives": fn,
        "true_negatives": tn,
        "false_positives": fp,
        "precision": (
            tp / precision_denominator
            if precision_denominator else None
        ),
        "recall": tp / positive_cases if positive_cases else None,
        "clean_control_pass_rate": (
            tn / clean_cases if clean_cases else None
        ),
        "evaluation_gate_passed": False,
        "note": (
            "This small synthetic benchmark is an initial check, not "
            "sufficient evidence to enable automatic posting."
        ),
    }
