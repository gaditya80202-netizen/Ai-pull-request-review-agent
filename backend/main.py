from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core.settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the ARQ connection pool when the API starts.
    app.state.arq_pool = await create_pool(
        RedisSettings.from_dsn(settings.redis_url)
    )

    try:
        yield
    finally:
        # Close the connection pool when the API shuts down.
        await app.state.arq_pool.aclose()


app = FastAPI(
    title="AI Pull-Request Review Agent",
    version="0.1.0",
    lifespan=lifespan,
)


class DemoJobRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


@app.get("/health", tags=["health"])
async def health_check():
    return {
        "status": "ok",
        "environment": settings.app_env,
    }


@app.post("/jobs/demo", status_code=202, tags=["jobs"])
async def enqueue_demo_job(
    payload: DemoJobRequest,
    request: Request,
):
    job = await request.app.state.arq_pool.enqueue_job(
        "demo_job",
        payload.message,
    )

    if job is None:
        raise HTTPException(
            status_code=503,
            detail="The job was not enqueued.",
        )

    return {
        "status": "queued",
        "job_id": job.job_id,
    }
