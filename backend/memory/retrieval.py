"""Hybrid retrieval over indexed repository chunks."""

import math
from typing import Any

from backend.core.settings import settings
from backend.memory.embedder import CloudflareEmbeddingClient
from backend.memory.tiger_client import TimescaleClient

RRF_K = 60
MAX_TOP_K = 50
MAX_CANDIDATE_K = 200


HYBRID_SEARCH_SQL = """
WITH query_input AS (
    SELECT
        $1::vector AS query_vector,
        plainto_tsquery('english', $2) AS query_ts
),
vector_hits AS (
    SELECT
        c.id,
        ROW_NUMBER() OVER (
            ORDER BY c.embedding <=> q.query_vector ASC
        ) AS vector_rank
    FROM code_chunks AS c
    CROSS JOIN query_input AS q
    WHERE c.repo = $3
      AND c.installation_id = $4
      AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> q.query_vector ASC
    LIMIT $5
),
text_hits AS (
    SELECT
        c.id,
        ROW_NUMBER() OVER (
            ORDER BY ts_rank_cd(c.content_tsv, q.query_ts) DESC
        ) AS text_rank
    FROM code_chunks AS c
    CROSS JOIN query_input AS q
    WHERE c.repo = $3
      AND c.installation_id = $4
      AND c.content_tsv @@ q.query_ts
    ORDER BY ts_rank_cd(c.content_tsv, q.query_ts) DESC
    LIMIT $5
),
combined_ranks AS (
    SELECT
        COALESCE(v.id, t.id) AS id,
        v.vector_rank,
        t.text_rank
    FROM vector_hits AS v
    FULL OUTER JOIN text_hits AS t ON v.id = t.id
)
SELECT
    c.id,
    c.path,
    c.symbol,
    c.chunk_index,
    c.content,
    c.token_count,
    r.vector_rank,
    r.text_rank,
    (
        CASE
            WHEN r.vector_rank IS NOT NULL
            THEN 1.0 / ($7 + r.vector_rank)
            ELSE 0.0
        END
        +
        CASE
            WHEN r.text_rank IS NOT NULL
            THEN 1.0 / ($7 + r.text_rank)
            ELSE 0.0
        END
    ) AS rrf_score
FROM combined_ranks AS r
JOIN code_chunks AS c ON c.id = r.id
ORDER BY rrf_score DESC, c.path ASC, c.chunk_index ASC
LIMIT $6
"""


async def hybrid_search(
    *,
    repo: str,
    installation_id: int,
    query: str,
    top_k: int = 8,
    candidate_k: int = 30,
    database: TimescaleClient | None = None,
    embedding_client: CloudflareEmbeddingClient | None = None,
) -> list[dict[str, Any]]:
    """Return repository chunks ranked by vector + full-text relevance.

    If database or embedding_client is supplied, the caller owns its lifecycle.
    A supplied database must already be connected.
    """

    if not isinstance(repo, str) or not repo.strip() or "/" not in repo:
        raise ValueError("repo must be in 'owner/repository' format")

    if (
        not isinstance(installation_id, int)
        or isinstance(installation_id, bool)
        or installation_id <= 0
    ):
        raise ValueError("installation_id must be a positive integer")

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")

    if len(query) > 8000:
        raise ValueError("query must be at most 8,000 characters")

    if not isinstance(top_k, int) or isinstance(top_k, bool):
        raise TypeError("top_k must be an integer")

    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}")

    if not isinstance(candidate_k, int) or isinstance(candidate_k, bool):
        raise TypeError("candidate_k must be an integer")

    if not top_k <= candidate_k <= MAX_CANDIDATE_K:
        raise ValueError(
            f"candidate_k must be between top_k and {MAX_CANDIDATE_K}"
        )

    owns_database = database is None
    owns_embedding_client = embedding_client is None

    db = database
    embedder = embedding_client

    try:
        if db is None:
            if not settings.tiger_database_url:
                raise RuntimeError(
                    "TIGER_DATABASE_URL is missing from .env"
                )
            db = TimescaleClient(settings.tiger_database_url)
            await db.connect()

        if embedder is None:
            embedder = CloudflareEmbeddingClient()

        # Mixing embedding models in the same repository index can produce
        # invalid similarity scores, so fail before embedding the query.
        mismatched_files = await db.fetch(
            """
            SELECT path, embedding_model
            FROM repo_file_index
            WHERE repo = $1
              AND installation_id = $2
              AND embedding_model IS DISTINCT FROM $3
            LIMIT 5
            """,
            repo,
            installation_id,
            settings.cloudflare_embedding_model,
        )

        if mismatched_files:
            details = [
                {
                    "path": row["path"],
                    "embedding_model": row["embedding_model"],
                }
                for row in mismatched_files
            ]
            raise RuntimeError(
                "Repository index contains files embedded with a different "
                f"model. Re-index the repository before searching. Details: "
                f"{details}"
            )

        vectors = await embedder.embed([query.strip()])

        if len(vectors) != 1:
            raise RuntimeError(
                "Embedding provider did not return exactly one query vector"
            )

        vector = vectors[0]

        if len(vector) != settings.embed_dim:
            raise RuntimeError(
                f"Query embedding has {len(vector)} dimensions; "
                f"expected {settings.embed_dim}"
            )

        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            for value in vector
        ):
            raise RuntimeError(
                "Query embedding contains invalid numeric values"
            )

        # pgvector accepts text in the form '[0.1,0.2,...]'.
        vector_literal = (
            "[" + ",".join(format(float(value), ".9g") for value in vector)
            + "]"
        )

        rows = await db.fetch(
            HYBRID_SEARCH_SQL,
            vector_literal,
            query.strip(),
            repo,
            installation_id,
            candidate_k,
            top_k,
            RRF_K,
        )

        return [dict(row) for row in rows]

    finally:
        if owns_embedding_client and embedder is not None:
            await embedder.aclose()

        if owns_database and db is not None:
            await db.close()
