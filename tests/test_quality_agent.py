import pytest

from backend.agents.quality import review_quality
from backend.integrations.deepseek_client import LLMCallResult
from backend.models.findings import LLMFindingList

DIFF = """\
diff --git a/example.py b/example.py
index 1111111..2222222 100644
--- a/example.py
+++ b/example.py
@@ -1,1 +1,2 @@
 existing = True
+result = calculate()
"""


def candidate(
    *,
    file_path="example.py",
    line_start=2,
):
    return {
        "severity": "MEDIUM",
        "category": "logic",
        "summary": "The result is not checked.",
        "file_path": file_path,
        "line_start": line_start,
        "line_end": None,
        "suggestion": "Check the result before using it.",
        "confidence": 0.8,
        "rationale": "An unexpected result could be used without validation.",
    }


class FakeLLM:
    def __init__(self, findings):
        self.findings = findings
        self.calls = 0

    async def call_llm(self, *, system_text, user_text, schema):
        self.calls += 1
        assert "Quality Review specialist" in system_text
        assert "BEGIN UNTRUSTED DIFF" in user_text
        assert schema is LLMFindingList

        return LLMCallResult(
            parsed=LLMFindingList.model_validate({
                "findings": self.findings
            }),
            tokens_in=100,
            tokens_out=50,
            latency_ms=200,
            raw_usage={},
            model="deepseek-flash",
        )


@pytest.mark.asyncio
async def test_valid_finding_is_accepted_and_agent_is_assigned():
    llm = FakeLLM([candidate()])

    result = await review_quality(DIFF, llm_client=llm)

    assert len(result.findings) == 1
    assert result.findings[0].agent_type == "quality"
    assert result.findings[0].line_start == 2
    assert result.rejected_findings == []
    assert result.requires_human_review is False
    assert result.prompt_version == "quality_v1"


@pytest.mark.asyncio
async def test_finding_on_unchanged_line_is_rejected():
    llm = FakeLLM([candidate(line_start=1)])

    result = await review_quality(DIFF, llm_client=llm)

    assert result.findings == []
    assert len(result.rejected_findings) == 1
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_finding_for_unknown_file_is_rejected():
    llm = FakeLLM([candidate(file_path="missing.py")])

    result = await review_quality(DIFF, llm_client=llm)

    assert result.findings == []
    assert len(result.rejected_findings) == 1
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_empty_findings_are_a_valid_agent_result():
    llm = FakeLLM([])

    result = await review_quality(DIFF, llm_client=llm)

    assert result.findings == []
    assert result.rejected_findings == []
    assert result.requires_human_review is False


@pytest.mark.asyncio
async def test_empty_diff_is_rejected_before_model_call():
    llm = FakeLLM([])

    with pytest.raises(ValueError, match="non-empty"):
        await review_quality("", llm_client=llm)

    assert llm.calls == 0
