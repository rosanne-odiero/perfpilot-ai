"""A fake OpenAI-compatible endpoint (httpx.MockTransport) for LLM tests. No network."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from backend.core.config import Settings

STEP_KEYS = {
    "1": "unit_summaries",
    "2": "dependency_context",
    "3": "hot_paths",
    "4": "correlation",
    "5": "recommendations",
}
EMPTY_OUTPUT: dict[str, dict[str, Any]] = {
    "unit_summaries": {"summaries": []},
    "dependency_context": {"modules": []},
    "hot_paths": {"paths": []},
    "correlation": {"insights": []},
    "recommendations": {"recommendations": []},
}
FAKE_KEY = "sk-test-0123456789abcdefghijKLMNOP"

Responder = Callable[[str, str], Any]


def llm_settings(settings: Settings, **overrides: Any) -> Settings:
    from pydantic import SecretStr

    values: dict[str, Any] = {
        "llm_base_url": "https://llm.example.test/v1",
        "llm_model": "fake-model",
        "llm_api_key": SecretStr(FAKE_KEY),
        "llm_max_retries": 1,
    }
    values.update(overrides)
    return settings.model_copy(update=values)


def step_of(body: dict[str, Any]) -> str:
    user = body["messages"][-1]["content"]
    match = re.search(r"Step (\d) of 5", user)
    return STEP_KEYS[match.group(1)] if match else "unknown"


class FakeLLM:
    """Records every request; answers with ``responder(step, user_content)`` (a dict is sent as
    the JSON message content, a str verbatim, an ``httpx.Response`` as is)."""

    def __init__(
        self, responder: Responder | None = None, usage: tuple[int, int] = (100, 50)
    ) -> None:
        self.responder = responder or (lambda step, _user: EMPTY_OUTPUT.get(step, {}))
        self.usage = usage
        self.bodies: list[dict[str, Any]] = []
        self.raw: list[bytes] = []
        self.headers: list[httpx.Headers] = []
        self.steps: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.raw.append(request.content)
        self.headers.append(request.headers)
        body = json.loads(request.content)
        self.bodies.append(body)
        step = step_of(body)
        self.steps.append(step)
        answer = self.responder(step, body["messages"][-1]["content"])
        if isinstance(answer, httpx.Response):
            return answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return httpx.Response(
            200,
            json={
                "model": "fake-model",
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": self.usage[0], "completion_tokens": self.usage[1]},
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    @property
    def calls(self) -> int:
        return len(self.bodies)

    def sent_text(self) -> str:
        return b"\n".join(self.raw).decode("utf-8", errors="replace")


def client_factory(fake: FakeLLM) -> Callable[[Settings], Any]:
    from backend.ai.llm_client import LLMClient

    return lambda cfg: LLMClient(cfg, transport=fake.transport, sleep=lambda _seconds: None)


def units_in(user: str) -> list[str]:
    return re.findall(r"^UNIT (\S+)", user, flags=re.MULTILINE)


def modules_in(user: str) -> list[str]:
    return re.findall(r"^module (\S+) \(", user, flags=re.MULTILINE)


def finding_ids_in(user: str, file_path: str) -> list[str]:
    return re.findall(rf"id (\w+) at {re.escape(file_path)}:", user)
