from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Application
    app_env: str = "development"
    log_level: str = "INFO"

    # Redis and Timescale/PostgreSQL
    redis_url: str = "redis://localhost:6379/0"
    tiger_database_url: str | None = None

    # GitHub webhook
    github_webhook_secret: str | None = None

    # GitHub App authentication
    github_app_id: str | None = None
    github_installation_id: int | None = None
    github_private_key_path: str = "github-app.pem"
    github_test_repo: str | None = None
    github_api_version: str = "2022-11-28"

    # DeepSeek LLM
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str | None = None
    deepseek_thinking: str = "disabled"
    llm_max_output_tokens: int = 4096
    max_context_tokens: int = 12000

    # Cloudflare Workers AI embeddings
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    cloudflare_embedding_model: str = "@cf/qwen/qwen3-embedding-0.6b"
    embed_dim: int = 1024
    embed_batch_size: int = 16

    # Review policy and budget
    confidence_threshold: float = 0.7
    daily_budget_usd: float = 0.50
    auto_post_enabled: bool = False
    evaluation_gate_passed: bool = False

    # Single-operator human-review API
    hitl_api_token: str | None = None
    hitl_reviewer_id: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
