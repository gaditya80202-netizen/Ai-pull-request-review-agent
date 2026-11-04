import pytest

from backend.agents.docs import review_docs
from backend.agents.security import review_security
from backend.agents.tests_agent import review_tests
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


def finding(line_start=2):
    return {
        "severity": "MEDIUM",
        "category": "review_issue",
        "summary": "A test finding.",
        "file_path": "example.py",
        "line_start": line_start,
        "line_end": None,
        "suggestion": None,
        "confidence": 0.8,
        "rationale": "A concrete reason for this finding.",
    }


class FakeLLM:
    def __init__(self, findings):
        self.findings = findings
        self.calls = 0
        self.system_text = ""

    async def call_llm(self, *, system_text, user_text, schema):
        self.calls += 1
        self.system_text = system_text

        assert "BEGIN UNTRUSTED DIFF" in user_text
        assert schema is LLMFindingList

        return LLMCallResult(
            parsed=LLMFindingList.model_validate({
                "findings": self.findings
            }),
            tokens_in=100,
            tokens_out=30,
            latency_ms=120,
            raw_usage={},
            model="deepseek-flash",
        )


@pytest.mark.parametrize(
    ("agent_type", "review_function", "prompt_title"),
    [
        ("security", review_security, "Security Review specialist"),
        ("tests", review_tests, "Test Coverage Review specialist"),
        ("docs", review_docs, "Documentation Review specialist"),
    ],
)
@pytest.mark.asyncio
async def test_agent_assigns_its_own_identity(
    agent_type, review_function, prompt_title
):
    llm = FakeLLM([finding()])

    result = await review_function(DIFF, llm_client=llm)

    assert len(result.findings) == 1
    assert result.findings[0].agent_type == agent_type
    assert result.findings[0].line_start == 2
    assert result.rejected_findings == []
    assert prompt_title in llm.system_text


@pytest.mark.asyncio
async def test_security_agent_rejects_unchanged_line():
    llm = FakeLLM([finding(line_start=1)])

    result = await review_security(DIFF, llm_client=llm)

    assert result.findings == []
    assert len(result.rejected_findings) == 1
    assert result.requires_human_review is True


@pytest.mark.asyncio
async def test_docs_agent_rejects_empty_diff_before_model_call():
    llm = FakeLLM([])

    with pytest.raises(ValueError, match="non-empty"):
        await review_docs("", llm_client=llm)

    assert llm.calls == 0
