from __future__ import annotations

from typing import Any

import asyncpg

from backend.core.settings import settings


class TimescaleClient:
    """Manage a reusable asynchronous PostgreSQL connection pool."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 5,
    ) -> None:
        self._dsn = dsn if dsn is not None else settings.tiger_database_url
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError(
                "Database pool is not connected. Call connect() first."
            )
        return self._pool

    async def connect(self) -> None:
        """Create the pool and verify database connectivity."""
        if self._pool is not None:
            return

        if not self._dsn:
            raise RuntimeError(
                "TIGER_DATABASE_URL is not configured."
            )

        if self._min_size < 1 or self._max_size < self._min_size:
            raise ValueError("Invalid database pool size configuration.")

        pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=15,
            command_timeout=30,
            server_settings={
                "application_name": "ai-pr-review-agent",
            },
        )

        try:
            async with pool.acquire() as connection:
                value = await connection.fetchval("SELECT 1")

                if value != 1:
                    raise RuntimeError(
                        "Database connectivity check returned an "
                        "unexpected result."
                    )
        except BaseException:
            await pool.close()
            raise

        self._pool = pool

    async def close(self) -> None:
        """Close the pool and release its connections."""
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            await pool.close()

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self.pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def __aenter__(self) -> TimescaleClient:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()
