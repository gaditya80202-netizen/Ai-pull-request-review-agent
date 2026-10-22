import pytest

from backend.evaluation.cases import (
    EVALUATION_CASES,
    validate_evaluation_cases,
)
from backend.evaluation.scoring import score_case, summarize_scores
from backend.integrations.diff_parser import parse_diff


def test_dataset_has_eight_unique_cases():
    validate_evaluation_cases()

    assert len(EVALUATION_CASES) == 16
    assert len({case.case_id for case in EVALUATION_CASES}) == 16


@pytest.mark.parametrize("agent", ["security", "quality", "tests", "docs"])
def test_each_agent_has_a_bug_case_and_clean_control(agent):
    cases = [case for case in EVALUATION_CASES if case.agent == agent]

    assert len(cases) == 4
    assert sum(case.expect_finding for case in cases) == 2
    assert sum(not case.expect_finding for case in cases) == 2


@pytest.mark.parametrize(
    "case",
    EVALUATION_CASES,
    ids=lambda case: case.case_id,
)
def test_expected_finding_anchor_is_on_an_added_line(case):
    parsed = parse_diff(case.diff)

    assert parsed
    if case.expect_finding:
        assert case.expected_path in parsed
        assert case.expected_line in parsed[case.expected_path]


def test_expected_issue_detection_is_scored():
    case = next(
        item for item in EVALUATION_CASES
        if item.case_id == "security_sql_injection"
    )

    score = score_case(case, [{
        "file_path": case.expected_path,
        "line_start": case.expected_line,
        "summary": "User input is concatenated into SQL",
    }])

    assert score["true_positive"] == 1
    assert score["false_negative"] == 0
    assert score["false_positive_count"] == 0


def test_missed_bug_and_false_positive_are_counted():
    cases = [
        next(item for item in EVALUATION_CASES
             if item.case_id == "quality_wrong_clamp_bound"),
        next(item for item in EVALUATION_CASES
             if item.case_id == "quality_correct_clamp"),
    ]

    scores = [
        score_case(cases[0], []),
        score_case(cases[1], [{
            "file_path": "backend/utils/numbers.py",
            "line_start": 1,
            "summary": "Unjustified finding",
        }]),
    ]

    summary = summarize_scores(scores)

    assert summary["false_negatives"] == 1
    assert summary["false_positives"] == 1
    assert summary["evaluation_gate_passed"] is False


def test_clean_control_passes_when_no_findings_are_returned():
    case = next(
        item for item in EVALUATION_CASES
        if item.case_id == "docs_documented_public_api"
    )

    score = score_case(case, [])

    assert score["true_negative"] == 1
    assert score["false_positive_count"] == 0
