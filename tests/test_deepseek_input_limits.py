from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.core.settings import settings
from backend.integrations.deepseek_client import (
    DeepSeekClient,
    validate_request_input_size,
)


class ExampleSchema(BaseModel):
    ok: bool


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("API must not be called for an oversized request")


def test_size_guard_accepts_small_request():
    size = validate_request_input_size(
        "System prompt",
        "Small request",
        2000,
    )
    assert size <= 2000


def test_size_guard_rejects_oversized_request():
    with pytest.raises(ValueError, match="exceeds the configured"):
        validate_request_input_size(
            "System prompt",
            "x" * 2000,
            1000,
        )


def test_size_guard_counts_utf8_bytes():
    # Each "é" uses two UTF-8 bytes.
    size = validate_request_input_size("s", "é" * 100, 2000)
    assert size >= 200


@pytest.mark.parametrize("limit", [0, -1, True, 512])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="MAX_CONTEXT_TOKENS"):
        validate_request_input_size("system", "user", limit)


@pytest.mark.asyncio
async def test_oversized_request_never_calls_api(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-flash")
    monkeypatch.setattr(settings, "max_context_tokens", 1000)
    monkeypatch.setattr(settings, "llm_max_output_tokens", 200)
    monkeypatch.setattr(settings, "deepseek_thinking", "disabled")

    completions = FakeCompletions()
    fake_api = SimpleNamespace(
        chat=SimpleNamespace(
            completions=completions,
        )
    )
    client = DeepSeekClient(api_client=fake_api)

    with pytest.raises(ValueError, match="exceeds the configured"):
        await client.call_llm(
            system_text="Review this code.",
            user_text="x" * 2000,
            schema=ExampleSchema,
        )

    assert completions.calls == []
