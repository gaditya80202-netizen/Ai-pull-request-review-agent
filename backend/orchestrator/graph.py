from __future__ import annotations

import asyncio
import logging
import operator
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.agents.docs import review_docs
from backend.agents.quality import review_quality
from backend.agents.security import review_security
from backend.agents.tests_agent import review_tests
from backend.integrations.diff_parser import parse_diff
from backend.memory.retrieval import hybrid_search
from backend.orchestrator.retrieval_context import retrieve_context_for_review
from backend.security.masking import mask_secrets


logger = logging.getLogger(__name__)

AGENT_NAMES = ("security", "quality", "tests", "docs")

AgentFunction = Callable[..., Awaitable[Any]]


def merge_status(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Combine independent agent status updates."""
    return {**left, **right}


class ReviewState(TypedDict, total=False):
    review_id: str
    installation_id: int
    repo: str
    pr_number: int
    head_sha: str

    diff: str
    touched_paths: list[str]
    context: str
    retrieval_status: str

    findings: Annotated[list[dict[str, Any]], operator.add]
    rejected_findings: Annotated[list[str], operator.add]
    agent_status: Annotated[
        dict[str, dict[str, Any]], merge_status
    ]

    result: dict[str, Any]


def build_context(state: ReviewState) -> dict[str, Any]:
    """Prepare the context available in this initial implementation.

    Repository-wide retrieval and embeddings will be connected later.
    """
    diff = state.get("diff", "")

    if not diff.strip():
        raise ValueError("Review graph requires a non-empty diff.")

    paths = state.get("touched_paths")
    if paths is None:
        # This initial fallback finds files with added lines.
        # The GitHub integration can supply the complete file list later.
        paths = sorted(parse_diff(diff).keys())

    return {
        "touched_paths": paths,
        "context": state.get("context", ""),
        "findings": [],
        "rejected_findings": [],
        "agent_status": {},
    }


async def retrieve_repository_context(
    state: ReviewState,
) -> dict[str, Any]:
    """Retrieve relevant indexed code before specialists run."""

    repo = state.get("repo")
    installation_id = state.get("installation_id")

    # Preserve diff-only calls used by existing tests and manual invocations.
    if not repo and installation_id is None:
        return {
            "context": state.get("context", ""),
            "retrieval_status": "skipped_missing_identity",
        }

    if not repo or installation_id is None:
        raise ValueError(
            "Repository retrieval requires both repo and installation_id."
        )

    diff = state.get("diff", "")
    paths = state.get("touched_paths") or sorted(parse_diff(diff).keys())

    additions = []
    total_chars = 0

    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue

        additions.append(line[1:])
        total_chars += len(line)

        if len(additions) >= 25 or total_chars >= 3000:
            break

    query = (
        "Find repository code relevant to these pull-request changes.\n"
        "Changed paths:\n"
        + "\n".join(str(path)[:200] for path in paths[:30])
        + "\nAdded-line excerpts:\n"
        + "\n".join(additions)
    )

    # Avoid embedding obvious credentials found in changed lines.
    query = mask_secrets(query)[:4000]

    try:
        repository_context = await asyncio.wait_for(
            retrieve_context_for_review(
                repo=repo,
                installation_id=installation_id,
                query=query,
                max_chunks=8,
                max_context_chars=8000,
            ),
            timeout=45.0,
        )
    except Exception as exc:
        # Do not silently continue as though contextual retrieval succeeded.
        logger.error(
            "Repository context retrieval failed with %s.",
            type(exc).__name__,
        )
        raise

    if not isinstance(repository_context, str):
        raise TypeError("Repository retrieval returned an invalid context.")

    existing_context = state.get("context", "")
    combined_parts = []

    if existing_context:
        combined_parts.append(existing_context[:3900])

    combined_parts.append(repository_context)
    combined_context = "\n\n".join(combined_parts)
    combined_context = combined_context[:12000]

    status = (
        "no_results"
        if repository_context.startswith(
            ("No relevant repository context", "No usable repository context")
        )
        else "completed"
    )

    return {
        "context": combined_context,
        "retrieval_status": status,
    }


def make_agent_node(
    agent_name: str,
    agent_function: AgentFunction,
    timeout_seconds: float,
):
    """Wrap a specialist so its failure is recorded, not mistaken for success."""

    async def run_agent(state: ReviewState) -> dict[str, Any]:
        try:
            specialist_result = await asyncio.wait_for(
                agent_function(
                    state["diff"],
                    repo_context=state.get("context", ""),
                ),
                timeout=timeout_seconds,
            )

            findings = []
            for finding in specialist_result.findings:
                if hasattr(finding, "model_dump"):
                    item = finding.model_dump(mode="json")
                else:
                    item = dict(finding)

                # The application controls the specialist identity.
                item["agent_type"] = agent_name
                findings.append(item)

            rejected = list(
                specialist_result.rejected_findings
            )

            status = "partial" if rejected else "success"

            status_record = {
                "status": status,
                "error_type": None,
                "prompt_version": specialist_result.prompt_version,
                "model": specialist_result.model,
                "tokens_in": specialist_result.tokens_in,
                "tokens_out": specialist_result.tokens_out,
                "latency_ms": specialist_result.latency_ms,
                "accepted_count": len(findings),
                "rejected_count": len(rejected),
            }

            return {
                "findings": findings,
                "rejected_findings": rejected,
                "agent_status": {agent_name: status_record},
            }

        except asyncio.TimeoutError:
            logger.error(
                "Specialist %s timed out.", agent_name
            )
            status_record = {
                "status": "timeout",
                "error_type": "TimeoutError",
                "accepted_count": 0,
                "rejected_count": 0,
            }

        except Exception as exc:
            # Record a safe error class; don't log source text,
            # credentials, prompts, or the exception message.
            logger.error(
                "Specialist %s failed with %s.",
                agent_name,
                type(exc).__name__,
            )
            status_record = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "accepted_count": 0,
                "rejected_count": 0,
            }

        return {
            "findings": [],
            "rejected_findings": [],
            "agent_status": {agent_name: status_record},
        }

    return run_agent


def aggregate(state: ReviewState) -> dict[str, Any]:
    from backend.orchestrator.aggregation import aggregate_review
    return aggregate_review(state)


def build_review_graph(
    agent_functions: dict[str, AgentFunction] | None = None,
    *,
    timeout_seconds: float = 90.0,
):
    """Build and compile the four-agent review graph."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive.")

    defaults: dict[str, AgentFunction] = {
        "security": review_security,
        "quality": review_quality,
        "tests": review_tests,
        "docs": review_docs,
    }

    if agent_functions is not None:
        unknown = set(agent_functions) - set(AGENT_NAMES)
        if unknown:
            raise ValueError(
                f"Unknown specialist names: {sorted(unknown)}"
            )
        defaults.update(agent_functions)

    builder = StateGraph(ReviewState)
    builder.add_node("build_context", build_context)
    builder.add_node("retrieve_repository_context", retrieve_repository_context)

    for name in AGENT_NAMES:
        builder.add_node(
            f"{name}_agent",
            make_agent_node(
                name,
                defaults[name],
                timeout_seconds,
            ),
        )

    builder.add_node("aggregate", aggregate)
    builder.add_edge(START, "build_context")
    builder.add_edge("build_context", "retrieve_repository_context")

    # Specialists start only after repository context retrieval completes.
    for name in AGENT_NAMES:
        builder.add_edge("retrieve_repository_context", f"{name}_agent")

    # Explicit barrier: aggregate waits for all four specialists.
    builder.add_edge(
        [f"{name}_agent" for name in AGENT_NAMES],
        "aggregate",
    )

    builder.add_edge("aggregate", END)
    return builder.compile()
