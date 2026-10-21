"""A selectbox whose selection is shareable through a URL query parameter holding an id.

Streamlit's ``bind="query-params"`` stores the *formatted label* in the URL and ignores values set
from outside (``st.switch_page(..., query_params=...)``) that are not labels, so a link carrying
an id could silently keep showing the previously selected item. Here the URL holds the id, an id
arriving in the URL wins over the remembered widget state, and user changes are written back.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import streamlit as st


def resolve_selection(
    state: MutableMapping[str, Any],
    *,
    key: str,
    options: Sequence[str],
    requested: str | None,
) -> None:
    """Seed ``state[key]`` before the widget renders.

    A requested id that differs from the last id this helper applied (i.e. a new link) replaces
    the widget's remembered value; a remembered value that is no longer an option is dropped.
    """
    applied_key = f"_{key}__url_applied"
    if requested and requested in options and state.get(applied_key) != requested:
        state[key] = requested
        state[applied_key] = requested
    if state.get(key) is not None and state.get(key) not in options:
        del state[key]


def url_selectbox(
    label: str,
    options: Sequence[str],
    *,
    key: str,
    param: str,
    format_func: Callable[[str], str] = str,
    container: Any = st,
    **kwargs: Any,
) -> str | None:
    """Render a selectbox over ids, synchronised with ``?param=<id>`` in the URL."""
    resolve_selection(
        st.session_state, key=key, options=options, requested=st.query_params.get(param)
    )
    choice: str | None = container.selectbox(
        label, list(options), format_func=format_func, key=key, **kwargs
    )
    if choice is not None:
        st.session_state[f"_{key}__url_applied"] = choice
        if st.query_params.get(param) != choice:
            st.query_params[param] = choice
    return choice
