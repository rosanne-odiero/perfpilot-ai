"""LLMClient: OpenAI-compatible calls, retries, adaptation, usage accounting (mocked HTTP)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from llm_support import FAKE_KEY, llm_settings

from backend.ai.llm_client import LLMClient, LLMError, endpoint_host
from backend.ai.outbound import ChatMessage
from backend.core.config import Settings
from backend.core.exceptions import NotConfiguredError

MESSAGES = [ChatMessage("system", "rules"), ChatMessage("user", "hello")]


def _ok(content: str = '{"ok": true}', usage: bool = True) -> httpx.Response:
    body: dict[str, object] = {
        "model": "served-model",
        "choices": [{"message": {"content": content}}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
    return httpx.Response(200, json=body)


class Script:
    """Returns queued responses (or raises queued exceptions) in order."""

    def __init__(self, *items: httpx.Response | Exception) -> None:
        self.items = list(items)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(
    settings: Settings, script: Script, **overrides: object
) -> tuple[LLMClient, list[float]]:
    sleeps: list[float] = []
    client = LLMClient(
        llm_settings(settings, **overrides),
        transport=httpx.MockTransport(script),
        sleep=sleeps.append,
    )
    return client, sleeps


def test_success_payload_headers_and_usage(settings: Settings) -> None:
    script = Script(_ok())
    client, sleeps = _client(settings, script)
    completion = client.complete(MESSAGES, max_tokens=300)
    assert completion.content == '{"ok": true}'
    assert (completion.input_tokens, completion.output_tokens) == (11, 7)
    assert not completion.usage_estimated
    assert completion.model == "served-model"
    request = script.requests[0]
    assert str(request.url) == "https://llm.example.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(request.content)
    assert body["model"] == "fake-model"
    assert body["max_tokens"] == 300
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert sleeps == []


def test_azure_endpoint_uses_api_key_header(settings: Settings) -> None:
    script = Script(_ok())
    client, _ = _client(settings, script, llm_base_url="https://res.openai.azure.com/openai/v1")
    client.complete(MESSAGES, max_tokens=50)
    headers = script.requests[0].headers
    assert headers["api-key"] == FAKE_KEY
    assert "authorization" not in headers


def test_local_server_without_key(settings: Settings) -> None:
    script = Script(_ok())
    client, _ = _client(
        settings, script, llm_base_url="http://localhost:11434/v1", llm_api_key=None
    )
    client.complete(MESSAGES, max_tokens=50)
    assert "authorization" not in script.requests[0].headers
    assert str(script.requests[0].url) == "http://localhost:11434/v1/chat/completions"


def test_429_is_retried_with_retry_after(settings: Settings) -> None:
    script = Script(httpx.Response(429, headers={"retry-after": "2"}), _ok())
    client, sleeps = _client(settings, script)
    assert client.complete(MESSAGES, max_tokens=50).content == '{"ok": true}'
    assert sleeps == [2.0]
    assert len(script.requests) == 2


def test_5xx_exhausts_bounded_retries(settings: Settings) -> None:
    script = Script(*(httpx.Response(503) for _ in range(5)))
    client, sleeps = _client(settings, script, llm_max_retries=2)
    with pytest.raises(LLMError, match="after 3 attempts"):
        client.complete(MESSAGES, max_tokens=50)
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]


def test_timeout_is_retried_then_fails(settings: Settings) -> None:
    timeout = httpx.ReadTimeout("slow")
    script = Script(timeout, httpx.ReadTimeout("slow"))
    client, sleeps = _client(settings, script, llm_max_retries=1)
    with pytest.raises(LLMError, match="ReadTimeout"):
        client.complete(MESSAGES, max_tokens=50)
    assert len(sleeps) == 1


def test_non_retryable_status_fails_immediately(settings: Settings) -> None:
    script = Script(httpx.Response(401, json={"error": {"message": "bad key"}}))
    client, sleeps = _client(settings, script)
    with pytest.raises(LLMError, match="HTTP 401"):
        client.complete(MESSAGES, max_tokens=50)
    assert sleeps == []


def test_unsupported_json_mode_is_dropped_once(settings: Settings) -> None:
    rejected = httpx.Response(400, json={"error": {"message": "response_format not supported"}})
    script = Script(rejected, _ok(), _ok())
    client, _ = _client(settings, script)
    client.complete(MESSAGES, max_tokens=50)
    client.complete(MESSAGES, max_tokens=50)
    bodies = [json.loads(r.content) for r in script.requests]
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]
    assert "response_format" not in bodies[2]


def test_max_tokens_parameter_is_adapted(settings: Settings) -> None:
    rejected = httpx.Response(
        400, json={"error": {"message": "Unsupported parameter: 'max_tokens'"}}
    )
    script = Script(rejected, _ok())
    client, _ = _client(settings, script)
    client.complete(MESSAGES, max_tokens=64)
    body = json.loads(script.requests[1].content)
    assert body["max_completion_tokens"] == 64
    assert "max_tokens" not in body


def test_missing_usage_is_estimated(settings: Settings) -> None:
    script = Script(_ok(content="x" * 40, usage=False))
    client, _ = _client(settings, script)
    completion = client.complete(MESSAGES, max_tokens=50)
    assert completion.usage_estimated
    assert completion.output_tokens == 10
    assert completion.input_tokens > 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
def test_malformed_response_shape(settings: Settings, response: httpx.Response) -> None:
    client, _ = _client(settings, Script(response))
    with pytest.raises(LLMError):
        client.complete(MESSAGES, max_tokens=50)


def test_requires_configuration(settings: Settings) -> None:
    with pytest.raises(NotConfiguredError):
        LLMClient(settings)


def test_never_logs_prompts_or_keys(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    script = Script(httpx.Response(500), _ok())
    client, _ = _client(settings, script)
    secret_prompt = [ChatMessage("user", "very-specific-prompt-text-12345")]
    client.complete(secret_prompt, max_tokens=50)
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert FAKE_KEY not in logged
    assert "very-specific-prompt-text-12345" not in logged
    assert "llm call" in logged


def test_endpoint_host_strips_everything_but_host() -> None:
    assert endpoint_host("https://user:pw@api.example.com:8443/v1?key=x") == "api.example.com:8443"
    assert endpoint_host("http://localhost:11434/v1") == "localhost:11434"
    assert endpoint_host(None) is None
