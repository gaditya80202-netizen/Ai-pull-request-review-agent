from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.core.settings import settings
from backend.integrations.deepseek_client import DeepSeekClient


class OutputSchema(BaseModel):
    ok: bool


class FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def model_dump(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def response(content, prompt_tokens=None, completion_tokens=None):
    usage = None
    if prompt_tokens is not None and completion_tokens is not None:
        usage = FakeUsage(prompt_tokens, completion_tokens)

    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )
        ],
        usage=usage,
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)

    async def create(self, **kwargs):
        return self.responses.pop(0)


class FakeAPIClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(responses)
        )

    async def close(self):
        pass


@pytest.fixture
def configured_settings(monkeypatch):
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-flash")
    monkeypatch.setattr(settings, "deepseek_thinking", "disabled")
    monkeypatch.setattr(settings, "llm_max_output_tokens", 128)
    monkeypatch.setattr(settings, "max_context_tokens", 10000)


@pytest.mark.asyncio
async def test_retry_usage_includes_both_model_attempts(configured_settings):
    api = FakeAPIClient([
        response("not valid JSON", 100, 20),
        response('{"ok": true}', 120, 30),
    ])
    client = DeepSeekClient(api_client=api)

    result = await client.call_llm(
        system_text="Return a JSON object.",
        user_text="Test input.",
        schema=OutputSchema,
    )

    assert result.parsed.ok is True
    assert result.tokens_in == 220
    assert result.tokens_out == 50


@pytest.mark.asyncio
async def test_missing_usage_does_not_report_partial_totals(
    configured_settings,
):
    api = FakeAPIClient([
        response("not valid JSON"),
        response('{"ok": true}', 120, 30),
    ])
    client = DeepSeekClient(api_client=api)

    result = await client.call_llm(
        system_text="Return a JSON object.",
        user_text="Test input.",
        schema=OutputSchema,
    )

    assert result.parsed.ok is True
    assert result.tokens_in is None
    assert result.tokens_out is None
