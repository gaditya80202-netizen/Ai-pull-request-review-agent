import pytest
from pydantic import ValidationError

from backend.models.findings import (
    Finding,
    LLMFinding,
    LLMFindingList,
)


def sample_finding():
    return {
        "severity": "HIGH",
        "category": "input_validation",
        "summary": "The input is not validated.",
        "file_path": "backend/example.py",
        "line_start": 12,
        "line_end": 14,
        "suggestion": "Validate the input before using it.",
        "confidence": 0.85,
        "rationale": "Unvalidated input can reach a sensitive operation.",
    }


def test_valid_llm_finding():
    finding = LLMFinding.model_validate(sample_finding())

    assert finding.severity.value == "HIGH"
    assert finding.line_start == 12
    assert finding.confidence == 0.85


def test_confidence_must_be_between_zero_and_one():
    data = sample_finding()
    data["confidence"] = 1.5

    with pytest.raises(ValidationError):
        LLMFinding.model_validate(data)


def test_line_numbers_must_be_valid():
    data = sample_finding()
    data["line_start"] = 0

    with pytest.raises(ValidationError):
        LLMFinding.model_validate(data)


def test_line_end_cannot_precede_line_start():
    data = sample_finding()
    data["line_start"] = 15
    data["line_end"] = 10

    with pytest.raises(ValidationError):
        LLMFinding.model_validate(data)


def test_llm_cannot_assign_its_own_agent_type():
    data = sample_finding()
    data["agent_type"] = "security"

    with pytest.raises(ValidationError):
        LLMFinding.model_validate(data)


def test_application_can_assign_agent_type():
    data = sample_finding()
    data["agent_type"] = "security"

    finding = Finding.model_validate(data)

    assert finding.agent_type == "security"


def test_agent_can_return_no_findings():
    result = LLMFindingList.model_validate({"findings": []})

    assert result.findings == []
