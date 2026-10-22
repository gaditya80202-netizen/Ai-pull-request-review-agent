import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from backend.core.settings import settings
from backend.main import app


TEST_SECRET = "webhook-test-secret"
DELIVERY_ID = "11111111-1111-4111-8111-111111111111"


class FakeJob:
    def __init__(self, job_id):
        self.job_id = job_id


class FakeARQPool:
    def __init__(self):
        self.values = {}
        self.job_ids = set()
        self.enqueued = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def enqueue_job(
        self, function, *args, _job_id=None, **kwargs
    ):
        if _job_id in self.job_ids:
            return None

        self.job_ids.add(_job_id)
        self.enqueued.append((function, args, _job_id))
        return FakeJob(_job_id)


@pytest.fixture
def webhook_client(monkeypatch):
    old_pool = getattr(app.state, "arq_pool", None)
    pool = FakeARQPool()

    monkeypatch.setattr(
        settings, "github_webhook_secret", TEST_SECRET
    )
    app.state.arq_pool = pool

    client = TestClient(app)
    yield client, pool

    client.close()

    if old_pool is None:
        if hasattr(app.state, "arq_pool"):
            delattr(app.state, "arq_pool")
    else:
        app.state.arq_pool = old_pool


def make_payload(action="opened"):
    return {
        "action": action,
        "installation": {"id": 123},
        "repository": {"full_name": "demo-owner/demo-repo"},
        "pull_request": {
            "number": 42,
            "head": {"sha": "abc123"},
        },
    }


def signed_headers(body, signature=None):
    digest = hmac.new(
        TEST_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return {
        "content-type": "application/json",
        "x-github-event": "pull_request",
        "x-github-delivery": DELIVERY_ID,
        "x-hub-signature-256": signature or f"sha256={digest}",
    }


def send_webhook(client, payload, signature=None):
    body = json.dumps(
        payload, separators=(",", ":")
    ).encode("utf-8")

    return client.post(
        "/webhook/github",
        content=body,
        headers=signed_headers(body, signature),
    )


def test_valid_pr_webhook_enqueues_review(webhook_client):
    client, pool = webhook_client

    response = send_webhook(client, make_payload())

    assert response.status_code == 202
    assert response.json()["queued"] is True
    assert len(pool.enqueued) == 1

    function, args, job_id = pool.enqueued[0]
    assert function == "run_review"
    assert job_id == f"github-{DELIVERY_ID}"

    job = args[0]
    assert job["repo"] == "demo-owner/demo-repo"
    assert job["pr_number"] == 42
    assert job["head_sha"] == "abc123"
    assert job["installation_id"] == 123


def test_duplicate_delivery_is_not_enqueued_twice(webhook_client):
    client, pool = webhook_client

    first = send_webhook(client, make_payload())
    second = send_webhook(client, make_payload())

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert len(pool.enqueued) == 1


def test_invalid_signature_is_rejected(webhook_client):
    client, pool = webhook_client

    response = send_webhook(
        client,
        make_payload(),
        signature="sha256=" + ("0" * 64),
    )

    assert response.status_code == 401
    assert len(pool.enqueued) == 0


def test_unsupported_pr_action_is_ignored(webhook_client):
    client, pool = webhook_client

    response = send_webhook(client, make_payload("closed"))

    assert response.status_code == 202
    assert response.json()["ignored"] is True
    assert len(pool.enqueued) == 0
