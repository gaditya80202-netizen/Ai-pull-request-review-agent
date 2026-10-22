import base64

import httpx
import pytest

from backend.integrations.github_client import GitHubClient


@pytest.mark.asyncio
async def test_get_default_branch_sha():
    async with GitHubClient() as client:
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))

            if path == "/repos/demo-org/demo-repo":
                return httpx.Response(
                    200,
                    json={"default_branch": "main"},
                )

            if path == "/repos/demo-org/demo-repo/branches/main":
                return httpx.Response(
                    200,
                    json={"commit": {"sha": "abc123"}},
                )

            raise AssertionError(f"Unexpected API path: {path}")

        client._authenticated_request = fake_request

        branch, sha = await client.get_default_branch_sha(
            "demo-org/demo-repo"
        )

        assert branch == "main"
        assert sha == "abc123"
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_get_repository_tree_requests_recursive_tree():
    async with GitHubClient() as client:
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {
                            "path": "README.md",
                            "type": "blob",
                            "size": 12,
                            "sha": "blob123",
                        }
                    ],
                    "truncated": False,
                },
            )

        client._authenticated_request = fake_request

        result = await client.get_repository_tree(
            "demo-org/demo-repo", "abc123"
        )

        assert result["truncated"] is False
        assert result["tree"][0]["path"] == "README.md"
        assert calls[0][2]["params"] == {"recursive": "1"}


@pytest.mark.asyncio
async def test_get_repository_tree_preserves_truncated_flag():
    async with GitHubClient() as client:
        async def fake_request(method, path, **kwargs):
            return httpx.Response(
                200,
                json={"tree": [], "truncated": True},
            )

        client._authenticated_request = fake_request

        result = await client.get_repository_tree(
            "demo-org/demo-repo", "abc123"
        )

        assert result["truncated"] is True


@pytest.mark.asyncio
async def test_get_file_text_decodes_utf8_at_requested_ref():
    content = "Hello from README.\n"
    encoded = base64.b64encode(content.encode()).decode()

    async with GitHubClient() as client:
        calls = []

        async def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": encoded,
                },
            )

        client._authenticated_request = fake_request

        result = await client.get_file_text(
            "demo-org/demo-repo",
            "docs/README.md",
            "abc123",
        )

        assert result == content
        assert calls[0][2]["params"] == {"ref": "abc123"}
        assert calls[0][1].endswith("/contents/docs/README.md")


@pytest.mark.asyncio
async def test_get_file_text_rejects_invalid_path_before_request():
    async with GitHubClient() as client:
        async def unexpected_request(*args, **kwargs):
            raise AssertionError("API should not be called.")

        client._authenticated_request = unexpected_request

        with pytest.raises(ValueError, match="Invalid repository file path"):
            await client.get_file_text(
                "demo-org/demo-repo",
                "../.env",
                "abc123",
            )


@pytest.mark.asyncio
async def test_get_file_text_rejects_binary_content():
    encoded = base64.b64encode(bytes([0xFF, 0xFE])).decode()

    async with GitHubClient() as client:
        async def fake_request(method, path, **kwargs):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "base64",
                    "content": encoded,
                },
            )

        client._authenticated_request = fake_request

        with pytest.raises(ValueError, match="not valid UTF-8"):
            await client.get_file_text(
                "demo-org/demo-repo",
                "image.txt",
                "abc123",
            )
