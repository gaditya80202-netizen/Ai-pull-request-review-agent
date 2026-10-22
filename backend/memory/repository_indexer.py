from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from backend.core.settings import settings
from backend.integrations.github_client import (
    GitHubClient,
    GitHubAPIError,
)
from backend.memory.chunker import split_text_into_chunks
from backend.memory.embedder import CloudflareEmbeddingClient
from backend.memory.file_filter import (
    MAX_FILE_BYTES,
    should_index_file,
)
from backend.memory.tiger_client import TimescaleClient


CHUNK_LINES = 60
CHUNK_OVERLAP_LINES = 10
MAX_CHUNK_CHARS = 6000
MAX_FILES_PER_PASS = 500
MAX_TREE_ENTRIES = 200_000


@dataclass
class IndexResult:
    repo: str
    branch: str
    commit_sha: str
    files_seen: int
    files_eligible: int
    files_indexed: int
    files_skipped_unchanged: int
    files_filtered: int
    chunks_written: int
    embedding_model: str


def _regular_blob(entry: dict[str, Any]) -> bool:
    """Accept ordinary Git files; skip symlinks and submodules."""
    return (
        entry.get("type") == "blob"
        and entry.get("mode") in {"100644", "100755"}
        and isinstance(entry.get("path"), str)
        and bool(entry.get("path"))
    )


async def collect_repository_files(
    github: Any,
    repo: str,
    tree_sha: str,
    *,
    max_tree_entries: int = MAX_TREE_ENTRIES,
) -> list[dict[str, Any]]:
    """Return a complete file listing for a commit's tree.

    If recursive retrieval is truncated, discard that partial result and
    traverse subtrees individually. Never return a partial tree as complete.
    """
    if max_tree_entries < 1:
        raise ValueError("max_tree_entries must be positive.")

    tree = await github.get_repository_tree(
        repo,
        tree_sha,
        recursive=True,
    )

    files: dict[str, dict[str, Any]] = {}

    if not tree.get("truncated", False):
        entries = tree["tree"]

        if len(entries) > max_tree_entries:
            raise GitHubAPIError(
                "Repository tree exceeded the configured indexing limit."
            )

        for entry in entries:
            if _regular_blob(entry):
                path = entry["path"]
                if path in files:
                    raise GitHubAPIError(
                        "GitHub returned duplicate repository paths."
                    )
                files[path] = entry
    else:
        # The recursive tree was incomplete, so do not use its partial
        # file list. Traverse the root and each subtree separately.
        stack: list[tuple[str, str]] = [(tree_sha, "")]
        visited_trees: set[str] = set()
        entries_seen = 0

        while stack:
            current_sha, prefix = stack.pop()

            if current_sha in visited_trees:
                raise GitHubAPIError(
                    "Repository tree contains a repeated subtree reference."
                )
            visited_trees.add(current_sha)

            subtree = await github.get_repository_tree(
                repo,
                current_sha,
                recursive=False,
            )

            if subtree.get("truncated", False):
                raise GitHubAPIError(
                    "A non-recursive subtree was truncated; "
                    "refusing to mark indexing complete."
                )

            for entry in subtree["tree"]:
                entries_seen += 1

                if entries_seen > max_tree_entries:
                    raise GitHubAPIError(
                        "Repository exceeded the configured tree-entry limit."
                    )

                name = entry.get("path")
                if not isinstance(name, str) or not name:
                    raise GitHubAPIError(
                        "GitHub returned an invalid tree path."
                    )

                combined_path = f"{prefix}{name}"

                if entry.get("type") == "tree":
                    subtree_sha = entry.get("sha")
                    if not isinstance(subtree_sha, str) or not subtree_sha:
                        raise GitHubAPIError(
                            "GitHub returned a subtree without a SHA."
                        )

                    stack.append((subtree_sha, combined_path + "/"))

                elif _regular_blob(entry):
                    entry = {**entry, "path": combined_path}

                    if combined_path in files:
                        raise GitHubAPIError(
                            "GitHub returned duplicate repository paths."
                        )

                    files[combined_path] = entry

    return [files[path] for path in sorted(files)]


def _vector_literal(vector: list[float]) -> str:
    """Format a validated numerical vector for PostgreSQL's vector type."""
    if len(vector) != settings.embed_dim:
        raise ValueError("Embedding dimension mismatch.")

    if not all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for value in vector
    ):
        raise ValueError("Embedding contains an invalid numerical value.")

    return "[" + ",".join(format(float(value), ".9g") for value in vector) + "]"


async def index_repository(
    repo: str,
    *,
    installation_id: int,
    github: Any | None = None,
    embedding_client: Any | None = None,
    database: Any | None = None,
    max_files: int = MAX_FILES_PER_PASS,
) -> IndexResult:
    """Index one repository snapshot and update its checkpoint on success.

    Existing file hashes allow successful files to be skipped on a retry.
    Repository-wide cleanup and last_indexed_sha advancement occur only
    after every eligible file has been handled successfully.
    """
    if installation_id < 1:
        raise ValueError("installation_id must be positive.")

    if not repo or len(repo.split("/")) != 2:
        raise ValueError("Repository must be in OWNER/REPOSITORY format.")

    if max_files < 1:
        raise ValueError("max_files must be positive.")

    own_github = github is None
    own_embedder = embedding_client is None
    own_database = database is None

    try:
        if github is None:
            github = GitHubClient()

        if embedding_client is None:
            embedding_client = CloudflareEmbeddingClient()

        if database is None:
            database = TimescaleClient()

        await database.connect()

        branch, commit_sha, tree_sha = (
            await github.get_default_branch_snapshot(repo)
        )

        tree_files = await collect_repository_files(
            github, repo, tree_sha
        )

        eligible_files = [
            entry
            for entry in tree_files
            if isinstance(entry.get("size"), int)
            and should_index_file(entry["path"], entry["size"])
        ]

        if len(eligible_files) > max_files:
            raise RuntimeError(
                f"Repository has {len(eligible_files)} eligible files, "
                f"exceeding the limit of {max_files}. "
                "No complete-index checkpoint was recorded."
            )

        model = settings.cloudflare_embedding_model

        existing_rows = await database.pool.fetch(
            """
            SELECT path, content_hash, embedding_model
            FROM repo_file_index
            WHERE installation_id = $1 AND repo = $2
            """,
            installation_id,
            repo,
        )

        existing = {row["path"]: row for row in existing_rows}
        eligible_paths: list[str] = []
        indexed_files = 0
        skipped_unchanged = 0
        chunks_written = 0

        for entry in eligible_files:
            path = entry["path"]
            eligible_paths.append(path)

            # Fetch the content at the exact commit, not the moving branch.
            content = await github.get_file_text(
                repo,
                path,
                commit_sha,
            )

            raw_size = len(content.encode("utf-8"))
            if raw_size > MAX_FILE_BYTES:
                raise RuntimeError(
                    f"File {path} exceeded the byte limit after retrieval."
                )

            content_hash = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()

            chunks = split_text_into_chunks(
                path,
                content,
                chunk_lines=CHUNK_LINES,
                overlap_lines=CHUNK_OVERLAP_LINES,
                max_chunk_chars=MAX_CHUNK_CHARS,
            )

            previous = existing.get(path)

            if (
                previous is not None
                and previous["content_hash"] == content_hash
                and previous["embedding_model"] == model
            ):
                stored_count = await database.pool.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM code_chunks
                    WHERE installation_id = $1
                      AND repo = $2
                      AND path = $3
                    """,
                    installation_id,
                    repo,
                    path,
                )

                if stored_count == len(chunks):
                    skipped_unchanged += 1
                    continue

            # Only masked chunk text is sent to the embedding provider.
            vectors = await embedding_client.embed(
                [chunk.content for chunk in chunks]
            )

            if len(vectors) != len(chunks):
                raise ValueError(
                    f"Embedding count mismatch for {path}."
                )

            vector_literals = [
                _vector_literal(vector) for vector in vectors
            ]

            # Replace one file's chunks and index record atomically.
            async with database.pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        """
                        DELETE FROM code_chunks
                        WHERE installation_id = $1
                          AND repo = $2
                          AND path = $3
                        """,
                        installation_id,
                        repo,
                        path,
                    )

                    for chunk, vector in zip(chunks, vector_literals):
                        await connection.execute(
                            """
                            INSERT INTO code_chunks (
                                installation_id, repo, path, symbol,
                                chunk_index, content, embedding, token_count
                            )
                            VALUES ($1, $2, $3, $4, $5, $6, $7::vector, $8)
                            """,
                            installation_id,
                            repo,
                            path,
                            chunk.symbol,
                            chunk.chunk_index,
                            chunk.content,
                            vector,
                            None,
                        )

                    await connection.execute(
                        """
                        INSERT INTO repo_file_index (
                            installation_id, repo, path,
                            content_hash, embedding_model, last_indexed_at
                        )
                        VALUES ($1, $2, $3, $4, $5, now())
                        ON CONFLICT (installation_id, repo, path)
                        DO UPDATE SET
                            content_hash = EXCLUDED.content_hash,
                            embedding_model = EXCLUDED.embedding_model,
                            last_indexed_at = now()
                        """,
                        installation_id,
                        repo,
                        path,
                        content_hash,
                        model,
                    )

            indexed_files += 1
            chunks_written += len(chunks)

        # Do not remove stale entries until every eligible file succeeded.
        # Update the repository SHA in the same transaction as cleanup.
        async with database.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    DELETE FROM code_chunks
                    WHERE installation_id = $1
                      AND repo = $2
                      AND NOT (path = ANY($3::text[]))
                    """,
                    installation_id,
                    repo,
                    eligible_paths,
                )

                await connection.execute(
                    """
                    DELETE FROM repo_file_index
                    WHERE installation_id = $1
                      AND repo = $2
                      AND NOT (path = ANY($3::text[]))
                    """,
                    installation_id,
                    repo,
                    eligible_paths,
                )

                await connection.execute(
                    """
                    INSERT INTO repo_index_state (
                        installation_id, repo, last_indexed_sha,
                        embedding_model, updated_at
                    )
                    VALUES ($1, $2, $3, $4, now())
                    ON CONFLICT (installation_id, repo)
                    DO UPDATE SET
                        last_indexed_sha = EXCLUDED.last_indexed_sha,
                        embedding_model = EXCLUDED.embedding_model,
                        updated_at = now()
                    """,
                    installation_id,
                    repo,
                    commit_sha,
                    model,
                )

        return IndexResult(
            repo=repo,
            branch=branch,
            commit_sha=commit_sha,
            files_seen=len(tree_files),
            files_eligible=len(eligible_files),
            files_indexed=indexed_files,
            files_skipped_unchanged=skipped_unchanged,
            files_filtered=len(tree_files) - len(eligible_files),
            chunks_written=chunks_written,
            embedding_model=model,
        )

    finally:
        if own_github and github is not None:
            await github.aclose()

        if own_embedder and embedding_client is not None:
            await embedding_client.aclose()

        if own_database and database is not None:
            await database.close()
