from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.memory.repository_indexer import (
    collect_repository_files,
    index_repository,
)


class FakeGitHub:
    async def get_default_branch_snapshot(self, repo):
        return "main", "commit-abc", "tree-root"

    async def get_repository_tree(self, repo, tree_sha, *, recursive=True):
        if recursive:
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "README.md",
                        "type": "blob",
                        "mode": "100644",
                        "size": 20,
                        "sha": "blob-readme",
                    },
                    {
                        "path": "link.py",
                        "type": "blob",
                        "mode": "120000",
                        "size": 20,
                        "sha": "blob-link",
                    },
                ],
            }

        raise AssertionError("Unexpected non-recursive request.")

    async def get_file_text(self, repo, path, ref):
        assert ref == "commit-abc"
        assert path == "README.md"
        return "# Test repository\nA small fixture.\n"

    async def aclose(self):
        pass


class TruncatedTreeGitHub:
    async def get_repository_tree(self, repo, tree_sha, *, recursive=True):
        if recursive:
            # This partial result must be discarded.
            return {
                "truncated": True,
                "tree": [
                    {
                        "path": "partial.py",
                        "type": "blob",
                        "mode": "100644",
                        "size": 20,
                        "sha": "partial",
                    }
                ],
            }

        if tree_sha == "tree-root":
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "README.md",
                        "type": "blob",
                        "mode": "100644",
                        "size": 20,
                        "sha": "readme",
                    },
                    {
                        "path": "src",
                        "type": "tree",
                        "mode": "040000",
                        "sha": "tree-src",
                    },
                ],
            }

        if tree_sha == "tree-src":
            return {
                "truncated": False,
                "tree": [
                    {
                        "path": "main.py",
                        "type": "blob",
                        "mode": "100644",
                        "size": 30,
                        "sha": "main",
                    }
                ],
            }

        raise AssertionError(f"Unexpected tree SHA: {tree_sha}")


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeConnection:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return FakeTransaction()

    async def execute(self, sql, *args):
        normalized = " ".join(sql.lower().split())

        if "delete from code_chunks" in normalized and "not (path = any" in normalized:
            keep_paths = set(args[2])
            self.pool.chunks = {
                key: value
                for key, value in self.pool.chunks.items()
                if key[0] in keep_paths
                or key[1] != (args[0], args[1])
            }

        elif "delete from repo_file_index" in normalized and "not (path = any" in normalized:
            keep_paths = set(args[2])
            self.pool.file_records = {
                key: value
                for key, value in self.pool.file_records.items()
                if key[0] in keep_paths
                or key[1] != (args[0], args[1])
            }

        elif normalized.startswith("delete from code_chunks"):
            installation_id, repo, path = args
            self.pool.chunks = {
                key: value
                for key, value in self.pool.chunks.items()
                if not (
                    key[0] == path
                    and key[1] == (installation_id, repo)
                )
            }

        elif normalized.startswith("insert into code_chunks"):
            installation_id, repo, path = args[:3]
            chunk_index = args[4]
            self.pool.chunks[(path, (installation_id, repo), chunk_index)] = {
                "content": args[5],
                "embedding": args[6],
            }

        elif normalized.startswith("insert into repo_file_index"):
            installation_id, repo, path, content_hash, model = args
            self.pool.file_records[(path, (installation_id, repo))] = {
                "path": path,
                "content_hash": content_hash,
                "embedding_model": model,
            }

        elif normalized.startswith("insert into repo_index_state"):
            installation_id, repo, sha, model = args
            self.pool.states[(installation_id, repo)] = {
                "last_indexed_sha": sha,
                "embedding_model": model,
            }

        else:
            raise AssertionError(f"Unexpected SQL: {normalized}")


class FakePool:
    def __init__(self):
        self.file_records = {}
        self.chunks = {}
        self.states = {}

    async def fetch(self, sql, installation_id, repo):
        return [
            value
            for value in self.file_records.values()
            if (
                value["path"],
                (installation_id, repo),
            ) in self.file_records
        ]

    async def fetchval(self, sql, installation_id, repo, path):
        return sum(
            1
            for key in self.chunks
            if key[0] == path and key[1] == (installation_id, repo)
        )

    @asynccontextmanager
    async def acquire(self):
        yield FakeConnection(self)


class FakeDatabase:
    def __init__(self):
        self.pool = FakePool()
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True


class FakeEmbedder:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated embedding failure")
        return [[0.01] * 1024 for _ in texts]

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_recursive_tree_filters_symlinks():
    files = await collect_repository_files(
        FakeGitHub(), "demo-org/demo-repo", "tree-root"
    )

    assert [item["path"] for item in files] == ["README.md"]


@pytest.mark.asyncio
async def test_truncated_tree_falls_back_to_complete_subtree_walk():
    files = await collect_repository_files(
        TruncatedTreeGitHub(), "demo-org/demo-repo", "tree-root"
    )

    assert [item["path"] for item in files] == [
        "README.md",
        "src/main.py",
    ]


@pytest.mark.asyncio
async def test_indexing_is_idempotent_for_unchanged_file():
    github = FakeGitHub()
    db = FakeDatabase()
    embedder = FakeEmbedder()

    first = await index_repository(
        "demo-org/demo-repo",
        installation_id=123,
        github=github,
        embedding_client=embedder,
        database=db,
    )

    assert first.files_indexed == 1
    assert first.chunks_written == 1
    assert db.pool.states[(123, "demo-org/demo-repo")][
        "last_indexed_sha"
    ] == "commit-abc"

    second = await index_repository(
        "demo-org/demo-repo",
        installation_id=123,
        github=github,
        embedding_client=embedder,
        database=db,
    )

    assert second.files_skipped_unchanged == 1
    assert second.files_indexed == 0
    assert embedder.calls == 1


@pytest.mark.asyncio
async def test_embedding_failure_does_not_advance_index_checkpoint():
    github = FakeGitHub()
    db = FakeDatabase()
    embedder = FakeEmbedder(fail=True)

    with pytest.raises(RuntimeError, match="embedding failure"):
        await index_repository(
            "demo-org/demo-repo",
            installation_id=123,
            github=github,
            embedding_client=embedder,
            database=db,
        )

    assert db.pool.states == {}
