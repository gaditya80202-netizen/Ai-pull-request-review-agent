from fastapi import FastAPI

from backend.core.settings import settings


app = FastAPI(
    title="AI Pull-Request Review Agent",
    version="0.1.0",
)


@app.get("/health", tags=["health"])
async def health_check():
    return {
        "status": "ok",
        "environment": settings.app_env,
    }
