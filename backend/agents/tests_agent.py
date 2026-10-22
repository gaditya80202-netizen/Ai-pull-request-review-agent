from typing import Any

from backend.agents.specialist_runner import (
    SpecialistResult,
    run_specialist,
)


async def review_tests(
    diff: str,
    *,
    repo_context: str = "",
    llm_client: Any | None = None,
) -> SpecialistResult:
    return await run_specialist(
        diff,
        agent_type="tests",
        repo_context=repo_context,
        llm_client=llm_client,
    )
