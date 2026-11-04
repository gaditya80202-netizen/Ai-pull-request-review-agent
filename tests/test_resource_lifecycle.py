import pytest

import backend.job_queue.arq_worker as worker_module
import backend.main as main_module


class FakeDatabaseClient:
    instances = []

    def __init__(self):
        self.connected = False
        self.closed = False
        self.pool = object()
        self.instances.append(self)

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True


class FakeRedisPool:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fake_database():
    FakeDatabaseClient.instances.clear()


@pytest.mark.asyncio
async def test_api_lifespan_opens_and_closes_resources(monkeypatch):
    redis = FakeRedisPool()

    async def fake_create_pool(*args, **kwargs):
        return redis

    monkeypatch.setattr(
        main_module, "TimescaleClient", FakeDatabaseClient
    )
    monkeypatch.setattr(
        main_module, "create_pool", fake_create_pool
    )

    async with main_module.lifespan(main_module.app):
        database = FakeDatabaseClient.instances[0]

        assert database.connected is True
        assert main_module.app.state.db_pool is database.pool
        assert main_module.app.state.arq_pool is redis
        assert redis.closed is False

    assert database.closed is True
    assert redis.closed is True
    assert main_module.app.state.db_pool is None
    assert main_module.app.state.arq_pool is None


@pytest.mark.asyncio
async def test_api_closes_database_if_redis_startup_fails(monkeypatch):
    async def failing_create_pool(*args, **kwargs):
        raise RuntimeError("simulated Redis startup failure")

    monkeypatch.setattr(
        main_module, "TimescaleClient", FakeDatabaseClient
    )
    monkeypatch.setattr(
        main_module, "create_pool", failing_create_pool
    )

    with pytest.raises(RuntimeError, match="Redis startup failure"):
        async with main_module.lifespan(main_module.app):
            pass

    assert FakeDatabaseClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_worker_startup_and_shutdown_manage_database(monkeypatch):
    monkeypatch.setattr(
        worker_module, "TimescaleClient", FakeDatabaseClient
    )
    ctx = {}

    await worker_module.startup(ctx)

    database = FakeDatabaseClient.instances[0]
    assert database.connected is True
    assert ctx["tiger_client"] is database
    assert ctx["db_pool"] is database.pool

    await worker_module.shutdown(ctx)

    assert database.closed is True
    assert "tiger_client" not in ctx
    assert "db_pool" not in ctx


@pytest.mark.asyncio
async def test_worker_startup_failure_does_not_keep_resources(monkeypatch):
    class FailingDatabaseClient(FakeDatabaseClient):
        async def connect(self):
            raise RuntimeError("simulated database failure")

    monkeypatch.setattr(
        worker_module, "TimescaleClient", FailingDatabaseClient
    )
    ctx = {}

    with pytest.raises(RuntimeError, match="database failure"):
        await worker_module.startup(ctx)

    assert FailingDatabaseClient.instances[0].closed is True
    assert "tiger_client" not in ctx
    assert "db_pool" not in ctx
