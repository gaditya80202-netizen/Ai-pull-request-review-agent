from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    log_level: str = "INFO"

    redis_url: str = "redis://localhost:6379/0"
    tiger_database_url: str | None = None

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str | None = None

    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    cloudflare_embedding_model: str = (
        "@cf/qwen/qwen3-embedding-0.6b"
    )

    confidence_threshold: float = 0.7
    daily_budget_usd: float = 5.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
