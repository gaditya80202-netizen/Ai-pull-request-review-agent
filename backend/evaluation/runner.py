"""Dry-run and budget-controlled live evaluation of review specialists."""

import argparse
import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from backend.agents.docs import review_docs
from backend.agents.quality import review_quality
from backend.agents.security import review_security
from backend.agents.tests_agent import review_tests
from backend.core.settings import settings
from backend.economics.budget import (
    release_review_budget,
    reserve_review_budget,
    settle_review_budget,
)
from backend.economics.pricing import (
    calculate_cost_usd,
    estimate_review_reservation_usd,
)
from backend.evaluation.cases import (
    EVALUATION_CASES,
    validate_evaluation_cases,
)
from backend.evaluation.scoring import score_case, summarize_scores
from backend.memory.tiger_client import TimescaleClient

logger = logging.getLogger(__name__)

AGENTS = {
    "security": review_security,
    "quality": review_quality,
    "tests": review_tests,
    "docs": review_docs,
}

SYNTHETIC_REPO = "__evaluation__/synthetic-benchmark"
SYNTHETIC_PR_NUMBER = 0
SYNTHETIC_INSTALLATION_ID = 0


def _write_report(payload: dict) -> Path:
    """Write a JSON report without storing API credentials or raw diffs."""
    directory = Path(__file__).resolve().parent / "results"
    directory.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_short_id = payload["run_id"].replace("-", "")[:8]
    path = directory / f"evaluation_{timestamp}_{run_short_id}.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def _print_dry_run() -> None:
    validate_evaluation_cases()

    print("\nAI Pull-Request Review Agent — Evaluation Plan")
    print("=" * 54)

    for number, case in enumerate(EVALUATION_CASES, start=1):
        expectation = (
            f"Expected finding: {case.expected_path}:{case.expected_line}"
            if case.expect_finding
            else "Expected result: no findings"
        )
        print(f"{number}. {case.case_id}")
        print(f"   Agent: {case.agent}")
        print(f"   {expectation}")

    try:
        estimate = estimate_review_reservation_usd(
            agent_count=len(EVALUATION_CASES)
        )
        print(f"\nMaximum planned budget reservation: ${estimate}")
    except (ValueError, TypeError):
        print("\nBudget estimate unavailable; check model and token settings.")

    print(f"Configured daily budget: ${settings.daily_budget_usd}")
    print("=" * 54)
    print(f"Validated cases: {len(EVALUATION_CASES)}")
    print("Mode: DRY RUN — no API calls or database writes.")
    print("Live evaluation requires the explicit --live flag.")


async def _run_live() -> int:
    validate_evaluation_cases()

    missing_settings = []
    if not settings.tiger_database_url:
        missing_settings.append("TIGER_DATABASE_URL")
    if not settings.deepseek_api_key:
        missing_settings.append("DEEPSEEK_API_KEY")
    if not settings.deepseek_model:
        missing_settings.append("DEEPSEEK_MODEL")

    if missing_settings:
        raise RuntimeError(
            "Missing required settings: " + ", ".join(missing_settings)
        )

    # One specialist call per case, with one possible correction per call.
    reservation_amount = estimate_review_reservation_usd(
        agent_count=len(EVALUATION_CASES)
    )

    run_uuid = uuid4()
    started_at = datetime.now(UTC).isoformat()
    head_sha = hashlib.sha1(run_uuid.bytes).hexdigest()
    delivery_id = f"eval-{run_uuid.hex}"

    report = {
        "schema_version": 1,
        "run_id": str(run_uuid),
        "started_at": started_at,
        "mode": "live",
        "status": "starting",
        "model": settings.deepseek_model,
        "cases_planned": len(EVALUATION_CASES),
        "synthetic_record": {
            "installation_id": SYNTHETIC_INSTALLATION_ID,
            "repo": SYNTHETIC_REPO,
            "pr_number": SYNTHETIC_PR_NUMBER,
        },
        "reserved_usd": str(reservation_amount),
        "cases": [],
    }

    database = TimescaleClient()
    review_id = None
    reservation = None
    settlement_done = False
    api_attempted = False
    usage_complete = True
    total_reported_cost = Decimal(0)
    case_scores = []

    try:
        await database.connect()

        # The budget ledger references pr_review_records, so create a clearly
        # marked synthetic record; this does not contact GitHub.
        review_id = await database.fetchval(
            """
            INSERT INTO pr_review_records (
                installation_id, repo, pr_number, head_sha,
                delivery_id, status
            )
            VALUES (0, $1, 0, $2, $3, 'evaluation_running')
            RETURNING id::text
            """,
            SYNTHETIC_REPO,
            head_sha,
            delivery_id,
        )
        report["review_id"] = str(review_id)

        reservation = await reserve_review_budget(
            database,
            review_id,
            amount_usd=reservation_amount,
        )

        report["budget_reservation"] = {
            "allowed": reservation.allowed,
            "reason": reservation.reason,
            "reserved_usd": str(reservation.reserved_usd),
            "budget_day": (
                reservation.budget_day.isoformat()
                if reservation.budget_day
                else None
            ),
        }

        if not reservation.allowed:
            report["status"] = "evaluation_budget_blocked"
            await database.execute(
                """
                UPDATE pr_review_records SET status = $2
                WHERE id = $1::uuid
                """,
                review_id,
                report["status"],
            )
            report["finished_at"] = datetime.now(UTC).isoformat()
            path = _write_report(report)
            print(f"Budget reservation denied: {reservation.reason}")
            print("No model calls were made.")
            print(f"Report: {path}")
            return 2

        try:
            for case in EVALUATION_CASES:
                try:
                    api_attempted = True
                    result = await AGENTS[case.agent](
                        case.diff,
                        repo_context="",
                    )

                    findings = [
                        finding.model_dump(mode="json")
                        for finding in result.findings
                    ]
                    score = score_case(case, findings)
                    case_scores.append(score)

                    case_cost = None
                    if (
                        result.tokens_in is not None
                        and result.tokens_out is not None
                    ):
                        try:
                            cost = calculate_cost_usd(
                                model=result.model,
                                tokens_in=result.tokens_in,
                                tokens_out=result.tokens_out,
                            )
                            total_reported_cost += cost
                            case_cost = str(cost)
                        except (ValueError, TypeError):
                            # Unknown model pricing must not understate spend.
                            usage_complete = False
                    else:
                        usage_complete = False

                    report["cases"].append({
                        "case_id": case.case_id,
                        "agent": case.agent,
                        "status": "completed",
                        "model": result.model,
                        "prompt_version": result.prompt_version,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "latency_ms": result.latency_ms,
                        "rejected_finding_count": len(
                            result.rejected_findings
                        ),
                        "reported_cost_usd": case_cost,
                        "score": score,
                    })

                except Exception as exc:  # noqa: BLE001
                    # A failed request may still have incurred a charge.
                    # Stop and settle the full reservation conservatively.
                    usage_complete = False
                    score = score_case(case, [])
                    case_scores.append(score)
                    report["cases"].append({
                        "case_id": case.case_id,
                        "agent": case.agent,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "score": score,
                    })
                    print(
                        f"Case {case.case_id} failed "
                        f"({type(exc).__name__}); stopping the run."
                    )
                    break

            all_cases_succeeded = (
                len(report["cases"]) == len(EVALUATION_CASES)
                and all(
                    item["status"] == "completed"
                    for item in report["cases"]
                )
            )

            if all_cases_succeeded and usage_complete:
                settlement_amount = total_reported_cost
                settlement_basis = "reported_token_usage"
            else:
                # Do not understate spend when an attempt or token usage
                # is missing. This is a conservative upper-bound settlement.
                settlement_amount = reservation.reserved_usd
                settlement_basis = (
                    "full_reservation_due_to_incomplete_usage_or_error"
                )

            if api_attempted:
                await settle_review_budget(
                    database,
                    review_id,
                    settlement_amount,
                )
            else:
                await release_review_budget(database, review_id)

            settlement_done = True

            if not all_cases_succeeded:
                final_status = "evaluation_incomplete"
            elif not usage_complete:
                final_status = "evaluation_completed_usage_incomplete"
            else:
                final_status = "evaluation_completed"

            await database.execute(
                """
                UPDATE pr_review_records SET status = $2
                WHERE id = $1::uuid
                """,
                review_id,
                final_status,
            )

            report["status"] = final_status
            report["summary"] = summarize_scores(case_scores)
            report["settlement"] = {
                "basis": settlement_basis,
                "reported_token_cost_usd": str(total_reported_cost),
                "settled_cost_usd": str(settlement_amount),
                "reserved_usd": str(reservation.reserved_usd),
                "note": (
                    "Conservative settlement may exceed actual provider "
                    "charges when usage is incomplete."
                    if settlement_basis != "reported_token_usage"
                    else "Calculated from aggregated provider token usage."
                ),
            }
            report["finished_at"] = datetime.now(UTC).isoformat()
            path = _write_report(report)

            print(f"\nStatus: {final_status}")
            print(json.dumps(report["summary"], indent=2))
            print(f"Cost settlement: {settlement_basis}")
            print(f"JSON report: {path}")
            print("Evaluation gate remains disabled.")
            return 0 if all_cases_succeeded else 1

        except BaseException:
            # Ensure interruption after a possible API request cannot leave
            # an open budget reservation behind.
            if reservation.allowed and not settlement_done:
                if api_attempted:
                    await settle_review_budget(
                        database, review_id, reservation.reserved_usd
                    )
                else:
                    await release_review_budget(database, review_id)
                settlement_done = True
            raise

    except BaseException:
        if review_id is not None:
            try:
                await database.execute(
                    """
                    UPDATE pr_review_records SET status = $2
                    WHERE id = $1::uuid
                    """,
                    review_id,
                    "evaluation_interrupted",
                )
            except Exception as cleanup_exc:  # noqa: BLE001
                logger.warning(
                    "Could not mark interrupted evaluation in database: %s",
                    type(cleanup_exc).__name__,
                )

        report["status"] = "evaluation_interrupted"
        report["cases"] = report.get("cases", [])
        report["summary"] = summarize_scores(case_scores)
        report["finished_at"] = datetime.now(UTC).isoformat()
        try:
            path = _write_report(report)
            print(f"Partial evaluation report: {path}")
        except Exception as report_exc:  # noqa: BLE001
            logger.warning(
                "Could not write partial evaluation report: %s",
                type(report_exc).__name__,
            )
        raise

    finally:
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate the four review specialists."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Make paid DeepSeek calls with a database budget reservation.",
    )
    args = parser.parse_args()

    if not args.live:
        _print_dry_run()
        return

    try:
        exit_code = asyncio.run(_run_live())
    except KeyboardInterrupt:
        print("Evaluation interrupted.")
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001
        # Avoid printing database DSNs or other potentially sensitive details.
        print(f"Evaluation failed: {type(exc).__name__}.")
        raise SystemExit(1) from None

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
