"""Selection follows ids in the URL (regression: a new audit link showed the previous audit)."""

from __future__ import annotations

from typing import Any

import pytest

from frontend.components.url_select import resolve_selection

KEY = "audit_select"
APPLIED = f"_{KEY}__url_applied"


def test_first_visit_uses_requested_id() -> None:
    state: dict[str, Any] = {}
    resolve_selection(state, key=KEY, options=["a", "b"], requested="b")
    assert state[KEY] == "b"


def test_new_link_overrides_remembered_selection() -> None:
    state: dict[str, Any] = {KEY: "old", APPLIED: "old"}
    resolve_selection(state, key=KEY, options=["new", "old"], requested="new")
    assert state[KEY] == "new"


def test_user_choice_is_kept_while_url_is_unchanged() -> None:
    # The user picked "b" in the widget; the helper then wrote it to the URL and APPLIED.
    state: dict[str, Any] = {KEY: "b", APPLIED: "b"}
    resolve_selection(state, key=KEY, options=["a", "b"], requested="b")
    assert state[KEY] == "b"


@pytest.mark.parametrize("requested", [None, "", "not-an-option"])
def test_missing_or_unknown_ids_do_not_change_the_selection(requested: str | None) -> None:
    state: dict[str, Any] = {KEY: "a", APPLIED: "a"}
    resolve_selection(state, key=KEY, options=["a", "b"], requested=requested)
    assert state[KEY] == "a"


def test_stale_remembered_value_is_dropped() -> None:
    state: dict[str, Any] = {KEY: "deleted"}
    resolve_selection(state, key=KEY, options=["a", "b"], requested=None)
    assert KEY not in state
