from typing import Any

from backend.agents.specialist_runner import (
    SpecialistResult,
    run_specialist,
)

QualityAgentResult = SpecialistResult


async def review_quality(
    diff: str,
    *,
    repo_context: str = "",
    llm_client: Any | None = None,
) -> QualityAgentResult:
    return await run_specialist(
        diff,
        agent_type="quality",
        repo_context=repo_context,
        llm_client=llm_client,
    )
