"""Metric cards in the dashboard style (label, value, sub-line, provenance)."""

from __future__ import annotations

from dataclasses import dataclass

import streamlit as st

from frontend.components.provenance import provenance_html
from frontend.theme import esc


@dataclass(frozen=True)
class Card:
    label: str
    value: str
    sub: str | None = None
    provenance: str | None = None
    color: str | None = None
    help: str | None = None


def card_html(card: Card) -> str:
    badge = provenance_html(card.provenance) if card.provenance else ""
    style = f' style="color:{card.color}"' if card.color else ""
    sub = f'<div class="pp-metric-sub">{esc(card.sub)}</div>' if card.sub else ""
    title = f' title="{esc(card.help)}"' if card.help else ""
    return (
        f'<div class="pp-metric"{title}><div class="pp-metric-label"><span>{esc(card.label)}'
        f"</span>{badge}</div>"
        f'<div class="pp-metric-value"{style}>{esc(card.value)}</div>{sub}</div>'
    )


def cards_html(cards: list[Card]) -> str:
    return '<div class="pp-metric-grid">' + "".join(card_html(c) for c in cards) + "</div>"


def render_cards(cards: list[Card]) -> None:
    if cards:
        st.markdown(cards_html(cards), unsafe_allow_html=True)
