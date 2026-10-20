"""OpenAI-compatible Chat Completions client (httpx) for the optional LLM advisor.

Works with any server that implements ``POST {base_url}/chat/completions``: OpenAI
(``https://api.openai.com/v1``), Azure OpenAI v1 endpoints
(``https://<resource>.openai.azure.com/openai/v1``, key sent as ``api-key``), and local servers
such as Ollama (``http://localhost:11434/v1``) or LM Studio (``http://localhost:1234/v1``).

* JSON mode (``response_format={"type": "json_object"}``) is requested; a server that rejects it
  gets one retry without it and the instruction stays in the prompt. ``max_tokens`` /
  ``temperature`` incompatibilities are adapted the same way.
* 429, 5xx, timeouts and transport errors are retried at most ``llm_max_retries`` times with
  exponential backoff (``Retry-After`` honored, capped).
* Token usage comes from the response's ``usage``; when absent it is estimated and flagged.
* Every message is redacted right before sending. Prompts, responses and keys are never logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from backend.ai.budget import estimate_messages, estimate_tokens
from backend.ai.outbound import ChatMessage, redact_messages
from backend.core.config import Settings
from backend.core.exceptions import ExternalServiceError, NotConfiguredError
from backend.core.logging import get_logger

log = get_logger(__name__)

MAX_BACKOFF_SECONDS = 30.0
BASE_BACKOFF_SECONDS = 0.5
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".cognitiveservices.azure.com", ".azure-api.net")


class LLMError(ExternalServiceError):
    """The model endpoint failed (after retries) or returned an unusable response."""

    code = "llm_error"


@dataclass(frozen=True)
class ChatCompletion:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    usage_estimated: bool


@dataclass
class _Adaptations:
    json_mode: bool = True
    length_param: str = "max_tokens"
    send_temperature: bool = True
    tried: set[str] = field(default_factory=set)


def endpoint_host(base_url: str | None) -> str | None:
    """Host (and port) only — never paths, query strings, or credentials."""
    if not base_url:
        return None
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if parts.port else host or None


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not settings.llm_configured or not settings.llm_base_url or not settings.llm_model:
            raise NotConfiguredError("no LLM endpoint configured (PERFPILOT_LLM_BASE_URL/MODEL)")
        self.model = settings.llm_model
        self.max_retries = max(0, settings.llm_max_retries)
        self.redactions = 0
        self._sleep = sleep
        self._adapt = _Adaptations()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
        host = endpoint_host(settings.llm_base_url) or ""
        if key:
            if host.split(":", 1)[0].endswith(_AZURE_HOST_SUFFIXES):
                headers["api-key"] = key
            else:
                headers["Authorization"] = f"Bearer {key}"
        self._http = httpx.Client(
            base_url=settings.llm_base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(settings.llm_timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _payload(self, messages: list[ChatMessage], max_tokens: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            self._adapt.length_param: max_tokens,
        }
        if self._adapt.send_temperature:
            payload["temperature"] = 0
        if self._adapt.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _adapt_to(self, response: httpx.Response) -> bool:
        """Adjust the request after a 400 that names an unsupported parameter (once each)."""
        text = response.text.lower()[:2000]
        for marker, flag in (
            ("response_format", "json_mode"),
            ("json", "json_mode"),
            ("max_tokens", "length_param"),
            ("temperature", "send_temperature"),
        ):
            if marker in text and flag not in self._adapt.tried:
                self._adapt.tried.add(flag)
                if flag == "json_mode":
                    self._adapt.json_mode = False
                elif flag == "length_param":
                    self._adapt.length_param = "max_completion_tokens"
                else:
                    self._adapt.send_temperature = False
                return True
        return False

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        delay = BASE_BACKOFF_SECONDS * (2**attempt)
        if response is not None:
            retry_after = str(response.headers.get("retry-after", ""))
            if retry_after.replace(".", "", 1).isdigit():
                delay = max(delay, float(retry_after))
        return float(min(delay, MAX_BACKOFF_SECONDS))

    def complete(self, messages: list[ChatMessage], *, max_tokens: int) -> ChatCompletion:
        """One chat completion. Messages are redacted here, immediately before sending."""
        safe, redactions = redact_messages(messages)
        self.redactions += redactions
        attempt = 0
        while True:
            response: httpx.Response | None = None
            started = time.perf_counter()
            try:
                response = self._http.post("chat/completions", json=self._payload(safe, max_tokens))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                failure = f"{type(exc).__name__}"
            else:
                if response.status_code == 400 and self._adapt_to(response):
                    continue
                if response.is_success:
                    return self._parse(response, safe)
                failure = f"HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    raise LLMError(f"model endpoint rejected the request ({failure})")
            log.warning(
                "llm call failed",
                extra={
                    "model": self.model,
                    "attempt": attempt + 1,
                    "failure": failure,
                    "duration_s": round(time.perf_counter() - started, 3),
                },
            )
            if attempt >= self.max_retries:
                raise LLMError(
                    f"model endpoint unavailable after {attempt + 1} attempts: {failure}"
                )
            self._sleep(self._backoff(attempt, response))
            attempt += 1

    def _parse(self, response: httpx.Response, sent: list[ChatMessage]) -> ChatCompletion:
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("model endpoint returned an unexpected response shape") from exc
        if not isinstance(content, str):
            raise LLMError("model endpoint returned no text content")
        usage = data.get("usage") if isinstance(data, dict) else None
        input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
        estimated = not (isinstance(input_tokens, int) and isinstance(output_tokens, int))
        if estimated:
            input_tokens = estimate_messages(m.content for m in sent)
            output_tokens = estimate_tokens(content)
        assert isinstance(input_tokens, int)
        assert isinstance(output_tokens, int)
        model = data.get("model") if isinstance(data.get("model"), str) else self.model
        log.info(
            "llm call",
            extra={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_estimated": estimated,
            },
        )
        return ChatCompletion(content, str(model), input_tokens, output_tokens, estimated)


def create_llm_client(settings: Settings) -> LLMClient:
    return LLMClient(settings)
