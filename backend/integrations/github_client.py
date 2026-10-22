from __future__ import annotations

import time
import base64
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from backend.core.settings import settings


class GitHubAPIError(RuntimeError):
    """A GitHub API request failed."""


class GitHubTransientError(GitHubAPIError):
    """GitHub returned a temporary error that may be retried."""


class GitHubClient:
    BASE_URL = "https://api.github.com"
    TOKEN_CACHE_SECONDS = 50 * 60
    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=45.0,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": settings.github_api_version,
                "User-Agent": "ai-pr-review-agent",
            },
        )
        self._installation_token: str | None = None
        self._token_valid_until = 0.0

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _create_app_jwt(self) -> str:
        """Create a short-lived JWT signed by the GitHub App private key."""
        if not settings.github_app_id:
            raise RuntimeError("GITHUB_APP_ID is not configured.")

        key_path = Path(settings.github_private_key_path)
        if not key_path.is_absolute():
            project_root = Path(__file__).resolve().parents[2]
            key_path = project_root / key_path

        if not key_path.is_file():
            raise RuntimeError(
                "GitHub App private key file was not found at the configured path."
            )

        private_key = key_path.read_text(encoding="utf-8")

        now = int(time.time())
        claims = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": settings.github_app_id,
        }

        return jwt.encode(claims, private_key, algorithm="RS256")

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = await self._http.request(
            method,
            path,
            headers=headers,
            params=params,
            json=json_body,
        )

        if response.status_code in self.RETRYABLE_STATUS_CODES:
            raise GitHubTransientError(
                f"GitHub returned temporary HTTP {response.status_code}."
            )

        if response.is_error:
            detail = response.text[:500]
            raise GitHubAPIError(
                f"GitHub returned HTTP {response.status_code} "
                f"for {method} {path}: {detail}"
            )

        return response

    @retry(
        retry=retry_if_exception_type(
            (httpx.RequestError, GitHubTransientError)
        ),
        wait=wait_exponential_jitter(initial=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        return await self._send_once(
            method,
            path,
            headers=headers,
            params=params,
            json_body=json_body,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> httpx.Response:
        request = (
            self._request_with_retries
            if retryable
            else self._send_once
        )
        return await request(
            method,
            path,
            headers=headers,
            params=params,
            json_body=json_body,
        )

    async def get_installation_token(self) -> str:
        """Return a cached installation token, refreshing it when necessary."""
        if (
            self._installation_token
            and time.monotonic() < self._token_valid_until
        ):
            return self._installation_token

        if not settings.github_installation_id:
            raise RuntimeError(
                "GITHUB_INSTALLATION_ID is not configured."
            )

        app_jwt = self._create_app_jwt()

        response = await self._request(
            "POST",
            f"/app/installations/"
            f"{settings.github_installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )

        data = response.json()
        token = data.get("token")

        if not isinstance(token, str) or not token:
            raise GitHubAPIError(
                "GitHub's installation-token response did not contain a token."
            )

        self._installation_token = token
        self._token_valid_until = (
            time.monotonic() + self.TOKEN_CACHE_SECONDS
        )

        return token

    async def _authenticated_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> httpx.Response:
        token = await self.get_installation_token()

        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)

        return await self._request(
            method,
            path,
            headers=request_headers,
            params=params,
            json_body=json_body,
            retryable=retryable,
        )

    @staticmethod
    def _repository_path(full_name: str) -> str:
        parts = full_name.split("/")

        if (
            len(parts) != 2
            or not parts[0]
            or not parts[1]
            or any(part in {".", ".."} for part in parts)
        ):
            raise ValueError(
                "Repository must be in OWNER/REPOSITORY format."
            )

        owner, repository = parts
        return f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"

    async def get_repository(
        self, full_name: str
    ) -> dict[str, Any]:
        """Fetch metadata for a repository the installation can access."""
        response = await self._authenticated_request(
            "GET", self._repository_path(full_name)
        )
        return response.json()

    async def get_default_branch_sha(
        self, full_name: str
    ) -> tuple[str, str]:
        """Return the default branch name and its current commit SHA."""
        repository = await self.get_repository(full_name)
        branch = repository.get("default_branch")

        if not isinstance(branch, str) or not branch:
            raise GitHubAPIError(
                "GitHub did not return a default branch."
            )

        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/branches/"
            f"{quote(branch, safe='')}",
        )

        data = response.json()
        sha = data.get("commit", {}).get("sha")

        if not isinstance(sha, str) or not sha:
            raise GitHubAPIError(
                "GitHub did not return the default branch commit SHA."
            )

        return branch, sha

    async def get_default_branch_snapshot(
        self, full_name: str
    ) -> tuple[str, str, str]:
        """Return default branch, commit SHA and that commit's tree SHA."""
        repository = await self.get_repository(full_name)
        branch = repository.get("default_branch")

        if not isinstance(branch, str) or not branch:
            raise GitHubAPIError(
                "GitHub did not return a default branch."
            )

        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/branches/"
            f"{quote(branch, safe='')}",
        )

        data = response.json()
        commit = data.get("commit", {})
        commit_sha = commit.get("sha")
        tree_sha = (
            commit.get("commit", {})
            .get("tree", {})
            .get("sha")
        )

        if not isinstance(commit_sha, str) or not commit_sha:
            raise GitHubAPIError(
                "GitHub did not return the commit SHA."
            )

        if not isinstance(tree_sha, str) or not tree_sha:
            raise GitHubAPIError(
                "GitHub did not return the commit's tree SHA."
            )

        return branch, commit_sha, tree_sha

    async def get_repository_tree(
        self,
        full_name: str,
        tree_sha: str,
        *,
        recursive: bool = True,
    ) -> dict[str, Any]:
        """Fetch a tree; recursive=False enables safe truncation fallback."""
        if not tree_sha or "/" in tree_sha:
            raise ValueError("A valid tree SHA is required.")

        params = {"recursive": "1"} if recursive else None

        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/git/trees/"
            f"{quote(tree_sha, safe='')}",
            params=params,
        )

        data = response.json()

        if not isinstance(data, dict) or not isinstance(
            data.get("tree"), list
        ):
            raise GitHubAPIError(
                "GitHub returned an invalid repository tree."
            )

        return data

    async def get_file_text(
        self, full_name: str, path: str, ref: str
    ) -> str:
        """Fetch a UTF-8 text file at an exact branch, tag, or commit SHA."""
        if (
            not path
            or path.startswith("/")
            or "\\\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("Invalid repository file path.")

        if not ref or "\\\\" in ref:
            raise ValueError("A valid Git reference is required.")

        encoded_path = quote(path, safe="/")
        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/contents/{encoded_path}",
            params={"ref": ref},
        )

        data = response.json()

        if data.get("type") != "file":
            raise GitHubAPIError(
                "GitHub did not return a regular file."
            )

        if data.get("encoding") != "base64":
            raise GitHubAPIError(
                "GitHub did not return supported Base64 file content."
            )

        encoded_content = data.get("content")
        if not isinstance(encoded_content, str):
            raise GitHubAPIError(
                "GitHub response did not contain file content."
            )

        try:
            raw = base64.b64decode(
                "".join(encoded_content.split()),
                validate=True,
            )
            text = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(
                "Repository file is not valid UTF-8 text."
            ) from exc

        if "\x00" in text:
            raise ValueError(
                "Repository file appears to contain binary data."
            )

        return text

    async def get_pull_request(
        self, full_name: str, pr_number: int
    ) -> dict[str, Any]:
        """Fetch pull-request metadata, including its current head SHA."""
        if pr_number < 1:
            raise ValueError("Pull-request number must be positive.")

        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/pulls/{pr_number}",
        )
        return response.json()

    async def get_pull_request_diff(
        self, full_name: str, pr_number: int
    ) -> str:
        """Fetch the unified diff for a pull request."""
        if pr_number < 1:
            raise ValueError("Pull-request number must be positive.")

        response = await self._authenticated_request(
            "GET",
            f"{self._repository_path(full_name)}/pulls/{pr_number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        return response.text

    async def get_pull_request_files(
        self, full_name: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Fetch the changed-file list, following pagination."""
        if pr_number < 1:
            raise ValueError("Pull-request number must be positive.")

        files: list[dict[str, Any]] = []
        page = 1

        while page <= 100:
            response = await self._authenticated_request(
                "GET",
                f"{self._repository_path(full_name)}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
            )

            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubAPIError(
                    "GitHub returned an unexpected pull-request file list."
                )

            files.extend(batch)

            if len(batch) < 100:
                return files

            page += 1

        raise GitHubAPIError(
            "Pull request file list exceeded the configured pagination limit."
        )

    async def get_pull_request_reviews(
        self, full_name: str, pr_number: int
    ) -> list[dict[str, Any]]:
        """Fetch all reviews for a pull request, following pagination."""
        if pr_number < 1:
            raise ValueError("Pull-request number must be positive.")

        reviews: list[dict[str, Any]] = []
        page = 1

        while page <= 100:
            response = await self._authenticated_request(
                "GET",
                f"{self._repository_path(full_name)}/pulls/{pr_number}/reviews",
                params={"per_page": 100, "page": page},
            )

            batch = response.json()
            if not isinstance(batch, list):
                raise GitHubAPIError(
                    "GitHub returned an unexpected review list."
                )

            reviews.extend(batch)

            if len(batch) < 100:
                return reviews

            page += 1

        raise GitHubAPIError(
            "Review list exceeded the configured pagination limit."
        )

    async def post_review(
        self,
        full_name: str,
        pr_number: int,
        commit_sha: str,
        body: str,
        *,
        event: str = "COMMENT",
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Submit a GitHub review.

        This method deliberately does not retry the review submission
        automatically: a network timeout can happen after GitHub accepts
        a review, and blindly retrying could create a duplicate.
        """
        if pr_number < 1:
            raise ValueError("Pull-request number must be positive.")
        if not commit_sha.strip():
            raise ValueError("A commit SHA is required.")
        if not body.strip():
            raise ValueError("The review body cannot be empty.")
        if event not in {"COMMENT", "APPROVE", "REQUEST_CHANGES"}:
            raise ValueError("Unsupported GitHub review event.")

        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "body": body,
            "event": event,
        }

        if comments:
            payload["comments"] = comments

        response = await self._authenticated_request(
            "POST",
            f"{self._repository_path(full_name)}/pulls/{pr_number}/reviews",
            json_body=payload,
            retryable=False,
        )
        return response.json()
