from types import SimpleNamespace

import pytest

from backend.core.settings import settings
from backend.integrations.deepseek_client import DeepSeekClient
from backend.models.findings import LLMFindingList


class FakeUsage:
    prompt_tokens = 20
    completion_tokens = 10

    def model_dump(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


def fake_response(content, finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content),
            )
        ],
        usage=FakeUsage(),
    )


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeAPIClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(responses)
        )

    async def close(self):
        pass


@pytest.fixture
def model_settings(monkeypatch):
    monkeypatch.setattr(
        settings, "deepseek_model", "deepseek-flash"
    )
    monkeypatch.setattr(
        settings, "deepseek_thinking", "disabled"
    )
    monkeypatch.setattr(
        settings, "llm_max_output_tokens", 4096
    )


@pytest.mark.asyncio
async def test_valid_json_is_parsed_and_usage_returned(
    model_settings,
):
    api = FakeAPIClient([
        fake_response('{"findings": []}')
    ])
    client = DeepSeekClient(api_client=api)

    result = await client.call_llm(
        "Review the code.",
        "Example input.",
        LLMFindingList,
    )

    assert result.parsed.findings == []
    assert result.tokens_in == 20
    assert result.tokens_out == 10
    assert result.model == "deepseek-flash"
    assert len(api.chat.completions.calls) == 1

    call = api.chat.completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.asyncio
async def test_malformed_json_triggers_one_retry(model_settings):
    api = FakeAPIClient([
        fake_response("{broken json"),
        fake_response('{"findings": []}'),
    ])
    client = DeepSeekClient(api_client=api)

    result = await client.call_llm(
        "Review the code.",
        "Example input.",
        LLMFindingList,
    )

    assert result.parsed.findings == []
    assert len(api.chat.completions.calls) == 2
    assert api.chat.completions.calls[1]["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_truncated_response_triggers_retry(model_settings):
    api = FakeAPIClient([
        fake_response('{"findings":', finish_reason="length"),
        fake_response('{"findings": []}'),
    ])
    client = DeepSeekClient(api_client=api)

    result = await client.call_llm(
        "Review the code.",
        "Example input.",
        LLMFindingList,
    )

    assert result.parsed.findings == []
    assert len(api.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_invalid_schema_fails_after_two_attempts(
    model_settings,
):
    api = FakeAPIClient([
        fake_response('{"wrong_field": []}'),
        fake_response('{"still_wrong": []}'),
    ])
    client = DeepSeekClient(api_client=api)

    with pytest.raises(ValueError, match="after two attempts"):
        await client.call_llm(
            "Review the code.",
            "Example input.",
            LLMFindingList,
        )

    assert len(api.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_missing_model_is_rejected_before_api_call(
    monkeypatch,
):
    monkeypatch.setattr(settings, "deepseek_model", None)
    api = FakeAPIClient([])
    client = DeepSeekClient(api_client=api)

    with pytest.raises(RuntimeError, match="DEEPSEEK_MODEL"):
        await client.call_llm(
            "Review the code.",
            "Example input.",
            LLMFindingList,
        )

    assert len(api.chat.completions.calls) == 0
