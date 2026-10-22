import pytest
from fastapi.testclient import TestClient

from backend.core.settings import settings
from backend.main import app
from backend.hitl import router as hitl_router


TOKEN = "t" * 48
HITL_ID = "11111111-1111-4111-8111-111111111111"
FINDING_ID = "33333333-3333-4333-8333-333333333333"


class FakeDatabase:
    def __init__(self):
        self.fetch_calls = []

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return [{
            "hitl_review_id": HITL_ID,
            "review_id": "22222222-2222-4222-8222-222222222222",
            "reason": "Human approval required",
            "hitl_status": "pending",
            "repo": "owner/repository",
            "pr_number": 7,
            "head_sha": "a" * 40,
            "review_status": "human_review_required",
            "finding_count": 2,
        }]


@pytest.fixture
def api_client(monkeypatch):
    previous_db = getattr(app.state, "tiger_client", None)
    db = FakeDatabase()
    app.state.tiger_client = db

    monkeypatch.setattr(settings, "hitl_api_token", TOKEN)
    monkeypatch.setattr(settings, "hitl_reviewer_id", "operator-1")

    # Do not enter TestClient's context manager: that would run the
    # application lifespan and attempt real Timescale/Redis connections.
    client = TestClient(app)
    yield client, db
    client.close()

    if previous_db is None:
        if hasattr(app.state, "tiger_client"):
            delattr(app.state, "tiger_client")
    else:
        app.state.tiger_client = previous_db


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_pending_queue_requires_authentication(api_client):
    client, _ = api_client

    response = client.get("/api/hitl/reviews/pending")

    assert response.status_code == 401


def test_pending_queue_returns_items(api_client, auth_headers):
    client, db = api_client

    response = client.get(
        "/api/hitl/reviews/pending",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["hitl_review_id"] == HITL_ID
    assert response.json()["publication_allowed"] is False
    assert db.fetch_calls


def test_invalid_token_is_rejected(api_client):
    client, _ = api_client

    response = client.get(
        "/api/hitl/reviews/pending",
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_approval_calls_service_as_configured_reviewer(
    api_client, auth_headers, monkeypatch
):
    client, _ = api_client
    captured = {}

    async def fake_decide(database, **kwargs):
        captured.update(kwargs)
        return {
            "decision": kwargs["decision"],
            "reviewer": kwargs["reviewer"],
            "publication_allowed": False,
        }

    monkeypatch.setattr(hitl_router, "decide_review", fake_decide)

    response = client.post(
        f"/api/hitl/reviews/{HITL_ID}/decision",
        headers=auth_headers,
        json={"decision": "approved", "reviewer": "attacker-controlled"},
    )

    assert response.status_code == 200
    assert captured["reviewer"] == "operator-1"
    assert captured["decision"] == "approved"
    assert response.json()["publication_allowed"] is False


def test_invalid_decision_is_rejected(api_client, auth_headers):
    client, _ = api_client

    response = client.post(
        f"/api/hitl/reviews/{HITL_ID}/decision",
        headers=auth_headers,
        json={"decision": "publish"},
    )

    assert response.status_code == 422


def test_feedback_endpoint_authenticates(api_client):
    client, _ = api_client

    response = client.post(
        f"/api/hitl/findings/{FINDING_ID}/feedback",
        json={"verdict": "false_positive"},
    )

    assert response.status_code == 401


def test_missing_auth_configuration_fails_closed(
    api_client, monkeypatch
):
    client, _ = api_client
    monkeypatch.setattr(settings, "hitl_api_token", None)

    response = client.get(
        "/api/hitl/reviews/pending",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 503
