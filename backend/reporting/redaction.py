"""Secret redaction for reports.

Two passes are applied: every string in the ``AuditReport`` is redacted before rendering (so
escaping cannot hide a secret from the pattern matcher), and every rendered output is redacted
again as a safety net. The second pass is structure-aware so the output stays well-formed: per
value for JSON and CSV, per text node for HTML (and the HTML that becomes the PDF), and over the
whole text for Markdown.
"""

from __future__ import annotations

import html
import re
from typing import Any

from backend.core.redaction import RedactionResult, redact_text
from backend.reporting.model import AuditReport

_TAG = re.compile(r"(<[^>]*>)")
# ``path/to/file.ext:120-140`` references. The generic assignment rule would read
# ``auth.py:1012-1040`` as ``auth...: <secret>``; a trailing ``@`` (URL credentials) is excluded.
_LOCATION = re.compile(r"[\w./-]*\.[A-Za-z0-9]{1,10}:\d+(?:-\d+)?(?![\w@:])")
_PLACEHOLDER = re.compile("\x00(\\d+)\x00")


def redact_report_text(text: str) -> RedactionResult:
    """``redact_text`` that leaves ``file:line`` references intact."""
    if "\x00" in text:
        return redact_text(text)
    kept: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    result = redact_text(_LOCATION.sub(_stash, text))
    if not kept:
        return result
    restored = _PLACEHOLDER.sub(lambda m: kept[int(m.group(1))], result.text)
    return RedactionResult(text=restored, count=result.count, kinds=result.kinds)


def redact_value(value: Any) -> tuple[Any, int]:
    """Recursively redact strings (dict keys included) in JSON-like data."""
    if isinstance(value, str):
        result = redact_report_text(value)
        return result.text, result.count
    if isinstance(value, list):
        total = 0
        items: list[Any] = []
        for item in value:
            redacted, count = redact_value(item)
            items.append(redacted)
            total += count
        return items, total
    if isinstance(value, dict):
        total = 0
        mapping: dict[Any, Any] = {}
        for key, item in value.items():
            new_key, key_count = redact_value(key)
            redacted, count = redact_value(item)
            mapping[new_key] = redacted
            total += key_count + count
        return mapping, total
    return value, 0


def redact_html(document: str) -> RedactionResult:
    """Redact the text nodes of an HTML document without touching markup.

    Each text segment is unescaped before matching (so ``&#34;``-escaped quotes cannot hide an
    assignment) and re-escaped only when something was replaced. Running the matcher over raw
    markup instead would let a false positive swallow tags.
    """
    parts = _TAG.split(document)
    kinds: dict[str, int] = {}
    for i in range(0, len(parts), 2):
        segment = parts[i]
        if not segment.strip():
            continue
        result = redact_report_text(html.unescape(segment))
        if result.count:
            parts[i] = html.escape(result.text, quote=False)
            for kind, count in result.kinds.items():
                kinds[kind] = kinds.get(kind, 0) + count
    return RedactionResult(text="".join(parts), count=sum(kinds.values()), kinds=kinds)


def redact_report(report: AuditReport) -> tuple[AuditReport, int]:
    """Return a copy of ``report`` with secrets redacted, and the number of replacements."""
    data, count = redact_value(report.model_dump(mode="json"))
    redacted = AuditReport.model_validate(data)
    redacted.metadata.redactions_applied = report.metadata.redactions_applied + count
    return redacted, count
