from __future__ import annotations

import pytest

from backend.hitl import publication

HITL_ID = "11111111-1111-4111-8111-111111111111"
REVIEW_ID = "22222222-2222-4222-8222-222222222222"
HEAD_SHA = "a" * 40


class FakeDatabase:
    def __init__(self, *, live_status="human_approved", hitl_status="approved", publication_status="not_attempted", head_sha=HEAD_SHA, findings=None):
        self.row = {
            "review_id": REVIEW_ID,
            "installation_id": 123,
            "repo": "acme/project",
            "pr_number": 4,
            "head_sha": head_sha,
            "review_status": live_status,
            "publication_status": publication_status,
            "github_review_id": None,
            "overall_confidence": 0.9,
            "publication_event": None,
        }
        self.hitl_status = hitl_status
        self.findings = findings if findings is not None else [{
            "severity": "HIGH",
            "summary": "Potential data loss",
            "file_path": "src/main.py",
            "line_start": 12,
            "line_end": 12,
            "suggestion": "Check the result before deleting data.",
            "confidence": 0.91,
            "rationale": "The deletion happens before the result is validated.",
        }]

    async def fetch(self, query, *args):
        if "UPDATE pr_review_records AS r" in query:
            if self.hitl_status != "approved" or self.row["review_status"] != "human_approved" or self.row["publication_status"] != "not_attempted":
                return []
            self.row["publication_status"] = "publishing"
            return [dict(self.row)]
        if (
            "FROM hitl_reviews AS h" in query
            and "JOIN pr_review_records AS r ON r.id = h.review_id" in query
        ):
            return [{
                **self.row,
                "hitl_status": self.hitl_status,
            }]
        if "FROM hitl_reviews AS h" in query and "publication_status" in query:
            return [{
                "review_id": self.row["review_id"],
                "publication_status": self.row["publication_status"],
                "github_review_id": self.row["github_review_id"],
                "review_status": self.row["review_status"],
                "hitl_status": self.hitl_status,
                "publication_event": self.row["publication_event"],
            }]
        if "FROM finding_records" in query:
            return self.findings
        raise AssertionError("Unexpected database fetch query: " + query)

    async def execute(self, query, *args):
        if "UPDATE pr_review_records" not in query:
            raise AssertionError("Unexpected database execute query: " + query)
        _review_id, status, event, github_review_id, error, review_status = args
        self.row["publication_status"] = status
        if event is not None:
            self.row["publication_event"] = event
        if github_review_id is not None:
            self.row["github_review_id"] = github_review_id
        self.row["publication_error"] = error
        if review_status is not None:
            self.row["review_status"] = review_status
        return "UPDATE 1"


class FakeGitHub:
    def __init__(self, *, live_sha=HEAD_SHA, fail_post=False, reviews=None):
        self.live_sha = live_sha
        self.fail_post = fail_post
        self.reviews = list(reviews or [])
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_pull_request(self, repo, pr_number):
        return {"head": {"sha": self.live_sha}}

    async def get_pull_request_files(self, repo, pr_number):
        return [{"filename": "src/main.py", "patch": "@@ -11,0 +12,1 @@\n+added line"}]

    async def get_pull_request_reviews(self, repo, pr_number):
        return list(self.reviews)

    async def post_review(self, repo, pr_number, commit_sha, body, *, event="COMMENT", comments=None):
        self.posts.append({"repo": repo, "pr_number": pr_number, "sha": commit_sha, "body": body, "event": event, "comments": comments})
        if self.fail_post:
            raise TimeoutError("simulated network timeout")
        result = {"id": 987, "commit_id": commit_sha, "body": body, "state": "changes_requested" if event == "REQUEST_CHANGES" else "commented"}
        self.reviews.append(result)
        return result


@pytest.fixture(autouse=True)
def installation_id(monkeypatch):
    monkeypatch.setattr(publication.settings, "github_installation_id", 123)


@pytest.mark.asyncio
async def test_approved_high_finding_posts_comment_once(monkeypatch):
    database = FakeDatabase()
    github = FakeGitHub()
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)

    assert result["publication_status"] == "published"
    assert result["publication_allowed"] is True
    assert result["publication_event"] == "COMMENT"
    assert result["github_review_id"] == 987
    assert len(github.posts) == 1
    assert github.posts[0]["comments"][0]["path"] == "src/main.py"
    assert github.posts[0]["comments"][0]["line"] == 12
    assert "<!-- ai-pr-review-id:" in github.posts[0]["body"]
    assert database.row["review_status"] == "published"

    repeated = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)
    assert repeated["publication_status"] == "published"
    assert repeated["already_published"] is True
    assert len(github.posts) == 1


@pytest.mark.asyncio
async def test_stale_head_blocks_publication(monkeypatch):
    database = FakeDatabase()
    github = FakeGitHub(live_sha="b" * 40)
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)

    assert result["publication_status"] == "blocked_stale"
    assert result["publication_allowed"] is False
    assert github.posts == []


@pytest.mark.asyncio
async def test_uncertain_post_is_not_retried(monkeypatch):
    database = FakeDatabase()
    github = FakeGitHub(fail_post=True)
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)
    assert result["publication_status"] == "outcome_unknown"
    assert len(github.posts) == 1

    repeated = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)
    assert repeated["publication_status"] == "outcome_unknown"
    assert len(github.posts) == 1


@pytest.mark.asyncio
async def test_existing_marker_is_reconciled_without_second_post(monkeypatch):
    marker = f"<!-- ai-pr-review-id:{REVIEW_ID} -->"
    database = FakeDatabase()
    github = FakeGitHub(reviews=[{"id": 321, "commit_id": HEAD_SHA, "body": marker}])
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)

    assert result["publication_status"] == "published"
    assert result["already_published"] is True
    assert result["github_review_id"] == 321
    assert github.posts == []


@pytest.mark.asyncio
async def test_empty_findings_are_never_published(monkeypatch):
    database = FakeDatabase(findings=[])
    github = FakeGitHub()
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.publish_approved_review(database, hitl_review_id=HITL_ID)

    assert result["publication_status"] == "blocked_no_findings"
    assert github.posts == []


@pytest.mark.asyncio
async def test_reconciliation_marks_existing_review_published(monkeypatch):
    marker = f"<!-- ai-pr-review-id:{REVIEW_ID} -->"
    database = FakeDatabase(publication_status="outcome_unknown")
    github = FakeGitHub(
        reviews=[{"id": 321, "commit_id": HEAD_SHA, "body": marker}]
    )
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.reconcile_approved_review(
        database, hitl_review_id=HITL_ID
    )

    assert result["publication_status"] == "published"
    assert result["github_review_id"] == 321
    assert database.row["publication_status"] == "published"
    assert database.row["review_status"] == "published"
    assert github.posts == []


@pytest.mark.asyncio
async def test_reconciliation_without_matching_review_never_posts(monkeypatch):
    database = FakeDatabase(publication_status="outcome_unknown")
    github = FakeGitHub(reviews=[])
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.reconcile_approved_review(
        database, hitl_review_id=HITL_ID
    )

    assert result["publication_status"] == "outcome_unknown"
    assert github.posts == []
    assert database.row["publication_status"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_reconciliation_blocks_stale_head_without_posting(monkeypatch):
    database = FakeDatabase(publication_status="publishing")
    github = FakeGitHub(live_sha="b" * 40)
    monkeypatch.setattr(publication, "GitHubClient", lambda: github)

    result = await publication.reconcile_approved_review(
        database, hitl_review_id=HITL_ID
    )

    assert result["publication_status"] == "blocked_stale"
    assert github.posts == []
    assert database.row["publication_status"] == "publishing"
