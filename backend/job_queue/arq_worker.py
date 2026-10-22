from arq.connections import RedisSettings

from backend.core.settings import settings


async def demo_job(ctx, message: str) -> dict:
    print(f"[demo_job] Received: {message}", flush=True)
    return {"status": "ok", "message": message}


async def run_review(ctx, job: dict) -> dict:
    repo = job["repo"]
    pr_number = job["pr_number"]
    head_sha = job["head_sha"]

    print(
        f"[run_review] Received PR job: "
        f"{repo}#{pr_number} at {head_sha}",
        flush=True,
    )

    # The GitHub client and AI review graph will be added later.
    return {
        "status": "received",
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
    }


class WorkerSettings:
    functions = [demo_job, run_review]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = 300
    max_jobs = 5
