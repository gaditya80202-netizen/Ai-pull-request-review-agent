import pytest

from backend.job_queue import webhook_dispatcher as dispatcher


DELIVERY_ID = "11111111-1111-4111-8111-111111111111"


def pr_payload():
    return {
        "installation": {"id": 123},
        "repository": {"full_name": "owner/repository"},
        "pull_request": {
            "number": 7,
            "head": {"sha": "a" * 40},
        },
    }


class FakeDatabase:
    def __init__(self, delivery):
        self.delivery = delivery
        self.marks = []

    async def mark(self, *args, **kwargs):
        self.marks.append((args, kwargs))


class FakeQueue:
    def __init__(self, return_job=True):
        self.return_job = return_job
        self.jobs = []

    async def enqueue_job(self, function, *args, _job_id=None):
        self.jobs.append((function, args, _job_id))
        return object() if self.return_job else None


@pytest.mark.asyncio
async def test_pending_pr_delivery_is_queued(monkeypatch):
    payload = pr_payload()
    delivery = {
        "delivery_id": DELIVERY_ID,
        "event_name": "pull_request",
        "action": "opened",
        "payload": payload,
    }

    async def fake_list(db, limit):
        return [{
            "delivery_id": DELIVERY_ID,
            "status": "pending",
            "attempts": 0,
        }]

    async def fake_get(db, delivery_id):
        return delivery

    async def fake_mark(db, delivery_id, *, status, last_error=None):
        raise AssertionError("Valid PR delivery should not be marked here.")

    monkeypatch.setattr(dispatcher, "list_retryable_deliveries", fake_list)
    monkeypatch.setattr(dispatcher, "get_delivery", fake_get)
    monkeypatch.setattr(dispatcher, "mark_delivery", fake_mark)

    db = FakeDatabase(delivery)
    queue = FakeQueue()

    result = await dispatcher.dispatch_pending_webhooks({
        "tiger_client": db,
        "redis": queue,
    })

    assert result["queued"] == 1
    function, args, job_id = queue.jobs[0]
    assert function == "run_review"
    assert job_id == f"github-{DELIVERY_ID}-a1"
    assert args[0]["inbox_managed"] is True
    assert args[0]["head_sha"] == "a" * 40


@pytest.mark.asyncio
async def test_existing_arq_job_is_deferred(monkeypatch):
    delivery = {
        "delivery_id": DELIVERY_ID,
        "event_name": "pull_request",
        "action": "opened",
        "payload": pr_payload(),
    }

    async def fake_list(db, limit):
        return [{
            "delivery_id": DELIVERY_ID,
            "attempts": 0,
            "status": "pending",
        }]

    async def fake_get(db, delivery_id):
        return delivery

    async def fake_mark(*args, **kwargs):
        return None

    monkeypatch.setattr(dispatcher, "list_retryable_deliveries", fake_list)
    monkeypatch.setattr(dispatcher, "get_delivery", fake_get)
    monkeypatch.setattr(dispatcher, "mark_delivery", fake_mark)

    result = await dispatcher.dispatch_pending_webhooks({
        "tiger_client": FakeDatabase(delivery),
        "redis": FakeQueue(return_job=False),
    })

    assert result["queued"] == 0
    assert result["deferred"] == 1


@pytest.mark.asyncio
async def test_unavailable_resources_do_not_enqueue():
    result = await dispatcher.dispatch_pending_webhooks({})

    assert result["scanned"] == 0
    assert result["queued"] == 0


@pytest.mark.asyncio
async def test_non_pr_delivery_is_deferred(monkeypatch):
    delivery = {
        "delivery_id": DELIVERY_ID,
        "event_name": "push",
        "action": None,
        "payload": {},
    }

    async def fake_list(db, limit):
        return [{
            "delivery_id": DELIVERY_ID,
            "attempts": 0,
            "status": "pending",
        }]

    async def fake_get(db, delivery_id):
        return delivery

    monkeypatch.setattr(dispatcher, "list_retryable_deliveries", fake_list)
    monkeypatch.setattr(dispatcher, "get_delivery", fake_get)

    result = await dispatcher.dispatch_pending_webhooks({
        "tiger_client": FakeDatabase(delivery),
        "redis": FakeQueue(),
    })

    assert result["deferred"] == 1
    assert result["queued"] == 0


@pytest.mark.asyncio
async def test_default_branch_push_is_redispatched_as_reindex_job(
    monkeypatch,
):
    payload = {
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "deleted": False,
        "installation": {"id": 123},
        "repository": {
            "full_name": "owner/repository",
            "default_branch": "main",
        },
    }

    delivery = {
        "delivery_id": DELIVERY_ID,
        "event_name": "push",
        "action": None,
        "payload": payload,
    }

    async def fake_list(db, limit):
        return [{
            "delivery_id": DELIVERY_ID,
            "status": "pending",
            "attempts": 0,
        }]

    async def fake_get(db, delivery_id):
        return delivery

    async def fake_mark(*args, **kwargs):
        raise AssertionError("Valid push must not be marked ignored.")

    monkeypatch.setattr(
        dispatcher, "list_retryable_deliveries", fake_list
    )
    monkeypatch.setattr(
        dispatcher, "get_delivery", fake_get
    )
    monkeypatch.setattr(
        dispatcher, "mark_delivery", fake_mark
    )

    queue = FakeQueue()

    result = await dispatcher.dispatch_pending_webhooks({
        "tiger_client": FakeDatabase(delivery),
        "redis": queue,
    })

    assert result["queued"] == 1

    function, args, job_id = queue.jobs[0]
    assert function == "reindex_repo"
    assert job_id == f"github-{DELIVERY_ID}-a1"
    assert args[0]["repo"] == "owner/repository"
    assert args[0]["ref"] == "refs/heads/main"
    assert args[0]["head_sha"] == "a" * 40
    assert args[0]["inbox_managed"] is True
