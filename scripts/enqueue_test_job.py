import asyncio

from arq import create_pool
from arq.connections import RedisSettings

from backend.core.settings import settings


async def main():
    redis = await create_pool(
        RedisSettings.from_dsn(settings.redis_url)
    )

    try:
        job = await redis.enqueue_job(
            "demo_job",
            "Hello from the AI Pull-Request Review Agent!"
        )

        if job is None:
            print("The job was not enqueued because it was a duplicate.")
        else:
            print(f"Job enqueued successfully: {job.job_id}")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
