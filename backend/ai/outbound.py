"""Outbound safety for LLM calls: every message is redacted, sensitive files are never sent."""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.redaction import is_sensitive_path, redact_text


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


def redact_messages(messages: list[ChatMessage]) -> tuple[list[ChatMessage], int]:
    """Return redacted copies of ``messages`` and the number of redactions applied."""
    redacted: list[ChatMessage] = []
    count = 0
    for message in messages:
        result = redact_text(message.content)
        count += result.count
        redacted.append(ChatMessage(message.role, result.text))
    return redacted, count


def may_send_file(path: str) -> bool:
    """Content of sensitive files (``.env``, keys, credential files) never leaves the machine."""
    return not is_sensitive_path(path)
