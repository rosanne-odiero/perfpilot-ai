"""Secret redaction applied before any text leaves the machine (LLM calls) or is exported (reports).

Detection is pattern-based plus a high-entropy check on values assigned to secret-looking names.
It is deliberately conservative: a redacted false positive is cheap, a leaked key is not.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

REDACTED = "[REDACTED]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("azure_conn", re.compile(r"(?i)\b(?:AccountKey|SharedAccessKey|Password)=[^;\s'\"]{8,}")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@'\"]+:[^/\s@'\"]+@")),
]

# The lookbehind anchors keys at token starts; without it the leading ``[...]*`` makes the
# search quadratic on long tokens (e.g. 10 KB of sandbox output took seconds).
_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]*(?:secret|token|passw(?:or)?d|api[_-]?key|access[_-]?key|
        private[_-]?key|client[_-]?secret|auth|credential|dsn|conn(?:ection)?[_-]?string)
        [A-Za-z0-9_.-]*)
    (?P<sep>\s*[:=]\s*|\s*=>\s*)
    (?P<quote>['"]?)
    (?P<value>[^\s'",;#]{8,})
    (?P=quote)
    """
)

_PLACEHOLDER = re.compile(
    r"(?i)^(?:x+|\*+|changeme|change_me|your[_-].*|<.*>|\$\{.*\}|\{\{.*\}\}|example.*|dummy.*|"
    r"test.*|none|null|true|false|os\.environ.*|getenv.*|settings\..*|config\..*|env\..*)$"
)

SENSITIVE_FILE_NAMES = re.compile(
    r"(?i)(^|/)(\.env(\..+)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|.*\.p12|.*\.pfx|"
    r"credentials(\.json)?|secrets?\.(json|ya?ml|toml)|\.netrc|\.pypirc|\.npmrc)$"
)


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _looks_secret(value: str) -> bool:
    if _PLACEHOLDER.match(value):
        return False
    if value.isidentifier() and value.lower() == value and "_" not in value:
        return False  # plain words / variable names
    return shannon_entropy(value) >= 3.0 or any(c.isdigit() for c in value)


@dataclass
class RedactionResult:
    text: str
    count: int = 0
    kinds: dict[str, int] = field(default_factory=dict)


def redact_text(text: str) -> RedactionResult:
    """Return ``text`` with secrets replaced by ``[REDACTED]`` plus counts by kind."""
    kinds: dict[str, int] = {}

    def _bump(kind: str) -> None:
        kinds[kind] = kinds.get(kind, 0) + 1

    for kind, pattern in _PATTERNS:

        def _sub(match: re.Match[str], kind: str = kind) -> str:
            _bump(kind)
            if kind == "url_credentials":
                return f"{match.group(1)}{REDACTED}@"
            if kind == "azure_conn":
                return match.group(0).split("=", 1)[0] + "=" + REDACTED
            return REDACTED

        text = pattern.sub(_sub, text)

    def _assign(match: re.Match[str]) -> str:
        value = match.group("value")
        if REDACTED in value or not _looks_secret(value):
            return match.group(0)
        _bump("assignment")
        quote = match.group("quote")
        return f"{match.group('key')}{match.group('sep')}{quote}{REDACTED}{quote}"

    text = _ASSIGNMENT.sub(_assign, text)
    return RedactionResult(text=text, count=sum(kinds.values()), kinds=kinds)


def is_sensitive_path(path: str) -> bool:
    """True for files whose *content* must never be shown or sent (e.g. ``.env``, keys)."""
    return bool(SENSITIVE_FILE_NAMES.search(path.replace("\\", "/")))
