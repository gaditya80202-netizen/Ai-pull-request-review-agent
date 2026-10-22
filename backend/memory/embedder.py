from __future__ import annotations

import math
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from backend.core.settings import settings


def _is_retryable(exc: BaseException) -> bool:
    """Retry temporary network errors, HTTP 429 and server errors."""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True

    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500

    return False


class CloudflareEmbeddingClient:
    """Generate validated embeddings through Cloudflare Workers AI."""

    def __init__(self, api_client: Any | None = None) -> None:
        if settings.embed_dim != 1024:
            raise RuntimeError(
                "The configured embedding dimension must be 1024."
            )

        if settings.embed_batch_size < 1:
            raise RuntimeError(
                "EMBED_BATCH_SIZE must be at least 1."
            )

        if api_client is None:
            if not settings.cloudflare_account_id:
                raise RuntimeError(
                    "CLOUDFLARE_ACCOUNT_ID is not configured."
                )

            if not settings.cloudflare_api_token:
                raise RuntimeError(
                    "CLOUDFLARE_API_TOKEN is not configured."
                )

            self._client = AsyncOpenAI(
                api_key=settings.cloudflare_api_token,
                base_url=(
                    "https://api.cloudflare.com/client/v4/accounts/"
                    f"{settings.cloudflare_account_id}/ai/v1"
                ),
                timeout=30.0,
                max_retries=0,
            )
        else:
            # Allows mocked responses during automated tests.
            self._client = api_client

    async def aclose(self) -> None:
        await self._client.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=settings.cloudflare_embedding_model,
            input=texts,
        )

        rows = sorted(response.data, key=lambda row: row.index)

        if len(rows) != len(texts):
            raise ValueError(
                "Embedding response count does not match input count."
            )

        indices = [row.index for row in rows]
        if indices != list(range(len(texts))):
            raise ValueError(
                "Embedding response has missing or duplicate indices."
            )

        vectors: list[list[float]] = []

        for row in rows:
            vector = row.embedding

            if len(vector) != settings.embed_dim:
                raise ValueError(
                    "Unexpected embedding dimension: "
                    f"expected {settings.embed_dim}, got {len(vector)}."
                )

            if not all(
                isinstance(value, (int, float))
                and math.isfinite(value)
                for value in vector
            ):
                raise ValueError(
                    "Embedding contains invalid numerical values."
                )

            vectors.append([float(value) for value in vector])

        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in bounded batches."""
        if not texts:
            return []

        if any(
            not isinstance(text, str) or not text.strip()
            for text in texts
        ):
            raise ValueError(
                "Every input must be a non-empty string."
            )

        batch_size = settings.embed_batch_size
        result: list[list[float]] = []

        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            vectors = await self._embed_batch(batch)
            result.extend(vectors)

        if len(result) != len(texts):
            raise ValueError(
                "Final embedding count does not match input count."
            )

        return result
