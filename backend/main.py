from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend.core.settings import settings
from backend.memory.tiger_client import TimescaleClient
from backend.webhook_receiver.router import router as github_webhook_router
from backend.hitl.router import router as hitl_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    tiger_client = TimescaleClient()
    arq_pool = None

    try:
        await tiger_client.connect()
        arq_pool = await create_pool(
            RedisSettings.from_dsn(settings.redis_url)
        )

        app.state.tiger_client = tiger_client
        app.state.db_pool = tiger_client.pool
        app.state.arq_pool = arq_pool

        yield
    finally:
        try:
            if arq_pool is not None:
                await arq_pool.aclose()
        finally:
            try:
                await tiger_client.close()
            finally:
                app.state.tiger_client = None
                app.state.db_pool = None
                app.state.arq_pool = None

app = FastAPI(
    title="AI Pull-Request Review Agent",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(github_webhook_router)
app.include_router(hitl_router)


class DemoJobRequest(BaseModel): #FastAPI validates the JSON using our Pydantic model
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
        raise HTTPException(503, "The job was not enqueued")

    return {
        "status": "queued",
        "job_id": job.job_id,
    }
