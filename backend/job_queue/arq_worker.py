from arq.connections import RedisSettings

from backend.core.settings import settings


async def demo_job(ctx, message: str) -> dict:
    print(f"[demo_job] Received: {message}", flush=True)
    return {"status": "ok", "message": message}


class WorkerSettings:
    functions = [demo_job]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = 30
    max_jobs = 5
