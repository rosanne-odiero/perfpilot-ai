"""Repository-controlled text rendered by the UI through ``unsafe_allow_html`` must be escaped.

Finding titles, file paths, rule IDs and recommendation fields come from imported (untrusted)
repositories or the LLM. The Streamlit helpers below build raw HTML, so each must escape them.
"""

from __future__ import annotations

import pytest

from frontend.components.metric_cards import Card, cards_html
from frontend.components.provenance import provenance_html
from frontend.theme import badge, severity_badge

PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "<a href='javascript:alert(1)'>x</a>",
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_badges_escape_text_and_title(payload: str) -> None:
    html = badge(payload, "#000", title=payload)
    assert "<script" not in html
    assert "<img" not in html
    assert "<a " not in html
    assert severity_badge(payload).count("<span") == 1


@pytest.mark.parametrize("payload", PAYLOADS)
def test_metric_cards_escape_every_field(payload: str) -> None:
    html = cards_html([Card(label=payload, value=payload, sub=payload, help=payload)])
    for dangerous in ("<script", "<img", "<a "):
        assert dangerous not in html


@pytest.mark.parametrize("payload", PAYLOADS)
def test_unknown_provenance_values_are_escaped(payload: str) -> None:
    html = provenance_html(payload)
    assert "<script" not in html
    assert "<img" not in html
