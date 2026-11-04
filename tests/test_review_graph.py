import asyncio
from types import SimpleNamespace

import pytest

from backend.models.findings import LLMFinding
from backend.orchestrator.graph import (
    AGENT_NAMES,
    build_review_graph,
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


def make_result(findings=None, rejected=None, name="mock"):
    return SimpleNamespace(
        findings=findings or [],
        rejected_findings=rejected or [],
        prompt_version=f"{name}_v1",
        model="mock-model",
        tokens_in=10,
        tokens_out=5,
        latency_ms=20,
    )


def sample_finding():
    return LLMFinding.model_validate({
        "severity": "HIGH",
        "category": "logic",
        "summary": "An important result is not checked.",
        "file_path": "example.py",
        "line_start": 2,
        "line_end": None,
        "suggestion": None,
        "confidence": 0.9,
        "rationale": "An invalid result could be used downstream.",
    })


def make_agents(calls, *, error_agent=None, slow_agent=None,
                rejected_agent=None, finding_agent=None):
    functions = {}

    for name in AGENT_NAMES:
        async def fake_agent(
            diff,
            *,
            repo_context="",
            _name=name,
        ):
            calls.add(_name)

            assert diff == DIFF
            assert _name in AGENT_NAMES

            if _name == slow_agent:
                await asyncio.sleep(0.2)

            if _name == error_agent:
                raise RuntimeError("Mock specialist failure")

            findings = (
                [sample_finding()]
                if _name == finding_agent
                else []
            )

            rejected = (
                ["Mock invalid line reference"]
                if _name == rejected_agent
                else []
            )

            return make_result(
                findings,
                rejected,
                name=_name,
            )

        functions[name] = fake_agent

    return functions


def initial_state():
    return {
        "review_id": "test-review-1",
        "installation_id": 123,
        "repo": "demo-org/demo-repo",
        "pr_number": 1,
        "head_sha": "abc123",
        "diff": DIFF,
    }


@pytest.mark.asyncio
async def test_all_four_agents_run_and_join(monkeypatch):
    # This unit test checks agent orchestration, not live repository search.
    async def fake_hybrid_search(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "backend.orchestrator.retrieval_context.hybrid_search",
        fake_hybrid_search,
    )

    calls = set()
    graph = build_review_graph(make_agents(calls))

    result = await graph.ainvoke(initial_state())

    assert calls == set(AGENT_NAMES)
    assert set(result["agent_status"]) == set(AGENT_NAMES)
    assert all(
        item["status"] == "success"
        for item in result["agent_status"].values()
    )

    assert result["result"]["status"] == "human_review_required"
    assert result["result"]["auto_post_allowed"] is False
    assert result["result"]["auto_post_allowed"] is False
    assert result["result"]["finding_count"] == 0


@pytest.mark.asyncio
async def test_verified_head_sha_does_not_add_sha_gate_reason(monkeypatch):
    async def fake_hybrid_search(*args, **kwargs):
        return []

    monkeypatch.setattr(
        "backend.orchestrator.retrieval_context.hybrid_search",
        fake_hybrid_search,
    )

    calls = set()
    graph = build_review_graph(make_agents(calls))
    state = initial_state()
    state["head_sha_verified"] = True

    result = await graph.ainvoke(state)
    aggregate_result = result["result"]

    assert "The current pull-request head SHA has not been verified by the caller." not in aggregate_result["gate_reasons"]
    assert "The evaluation gate has not passed." in aggregate_result["gate_reasons"]
    assert "Automatic posting is disabled." in aggregate_result["gate_reasons"]


@pytest.mark.asyncio
async def test_findings_are_collected_and_agent_identity_is_assigned():
    calls = set()
    graph = build_review_graph(
        make_agents(calls, finding_agent="security")
    )

    result = await graph.ainvoke(initial_state())

    findings = result["result"]["findings"]

    assert len(findings) == 1
    assert findings[0]["agent_type"] == "security"
    assert findings[0]["line_start"] == 2
    assert result["result"]["critical_finding_count"] == 0
    assert result["result"]["auto_post_allowed"] is False


@pytest.mark.asyncio
async def test_one_agent_failure_requires_human_review():
    calls = set()
    graph = build_review_graph(
        make_agents(calls, error_agent="security")
    )

    result = await graph.ainvoke(initial_state())

    assert calls == set(AGENT_NAMES)
    assert result["agent_status"]["security"]["status"] == "failed"
    assert result["result"]["status"] == "human_review_required"
    assert result["result"]["requires_human_review"] is True
    assert "security" in result["result"]["incomplete_agents"]


@pytest.mark.asyncio
async def test_rejected_model_findings_require_human_review():
    calls = set()
    graph = build_review_graph(
        make_agents(calls, rejected_agent="quality")
    )

    result = await graph.ainvoke(initial_state())

    assert result["agent_status"]["quality"]["status"] == "partial"
    assert result["result"]["rejected_finding_count"] == 1
    assert result["result"]["status"] == "human_review_required"
    assert result["result"]["auto_post_allowed"] is False


@pytest.mark.asyncio
async def test_agent_timeout_is_recorded_and_other_agents_finish():
    calls = set()
    graph = build_review_graph(
        make_agents(calls, slow_agent="tests"),
        timeout_seconds=0.05,
    )

    result = await graph.ainvoke(initial_state())

    assert calls == set(AGENT_NAMES)
    assert result["agent_status"]["tests"]["status"] == "timeout"
    assert result["result"]["status"] == "human_review_required"
    assert result["result"]["auto_post_allowed"] is False
