from backend.orchestrator.validation import (
    validate_and_deduplicate_findings,
)


DIFF = """\
diff --git a/example.py b/example.py
index 1111111..2222222 100644
--- a/example.py
+++ b/example.py
@@ -1,1 +1,2 @@
 existing = True
+result = calculate()
"""


def finding(
    *,
    agent="quality",
    path="example.py",
    line=2,
    severity="MEDIUM",
    summary="The result is not checked.",
    category="logic",
    line_end=None,
    confidence=0.8,
):
    return {
        "severity": severity,
        "category": category,
        "summary": summary,
        "file_path": path,
        "line_start": line,
        "line_end": line_end,
        "suggestion": None,
        "confidence": confidence,
        "rationale": "A concrete failure scenario.",
        "agent_type": agent,
    }


def test_valid_finding_is_accepted():
    accepted, rejected = validate_and_deduplicate_findings(
        [finding()], DIFF
    )

    assert len(accepted) == 1
    assert accepted[0]["agreed_by"] == ["quality"]
    assert rejected == []


def test_path_outside_pr_file_list_is_rejected():
    accepted, rejected = validate_and_deduplicate_findings(
        [finding(path="other.py")],
        DIFF,
        allowed_paths=["example.py", "other.py"],
    )

    assert accepted == []
    assert len(rejected) == 1


def test_unchanged_line_is_rejected():
    accepted, rejected = validate_and_deduplicate_findings(
        [finding(line=1)], DIFF
    )

    assert accepted == []
    assert len(rejected) == 1


def test_exact_duplicates_merge_and_keep_strongest_severity():
    accepted, rejected = validate_and_deduplicate_findings(
        [
            finding(agent="quality", severity="MEDIUM"),
            finding(
                agent="security",
                severity="HIGH",
                summary=" THE RESULT   IS NOT CHECKED. ",
                confidence=0.9,
            ),
        ],
        DIFF,
    )

    assert rejected == []
    assert len(accepted) == 1
    assert accepted[0]["severity"] == "HIGH"
    assert accepted[0]["agreed_by"] == ["quality", "security"]
    assert accepted[0]["confidence"] == 0.9


def test_distinct_issues_on_same_line_are_not_merged():
    accepted, rejected = validate_and_deduplicate_findings(
        [
            finding(summary="First issue."),
            finding(summary="Different issue."),
        ],
        DIFF,
    )

    assert rejected == []
    assert len(accepted) == 2


def test_invalid_line_range_is_rejected():
    accepted, rejected = validate_and_deduplicate_findings(
        [finding(line_end=1)], DIFF
    )

    assert accepted == []
    assert len(rejected) == 1


def test_unsafe_repository_path_is_rejected():
    accepted, rejected = validate_and_deduplicate_findings(
        [finding(path="../example.py")], DIFF
    )

    assert accepted == []
    assert len(rejected) == 1
