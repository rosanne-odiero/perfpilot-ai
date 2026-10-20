"""Consistent error, empty and "capability not available" states."""

from __future__ import annotations

import streamlit as st

from frontend.api_client import TIMEOUT, UNREACHABLE, ApiError
from frontend.theme import esc

NOT_AVAILABLE = "This capability is not available in this API version"


def show_api_error(exc: ApiError, context: str | None = None) -> None:
    prefix = f"{context}: " if context else ""
    if exc.code in {UNREACHABLE, TIMEOUT}:
        st.error(
            f"{prefix}{exc.message}. Start the API with `make api` (and the worker with "
            "`make worker`), or set `PERFPILOT_API_BASE_URL`.",
            icon="🔌",
        )
        return
    http = f" (HTTP {exc.status})" if exc.status else ""
    st.error(f"{prefix}**{exc.message or exc.code}** · `{exc.code}`{http}", icon="⚠️")
    if exc.details:
        with st.expander("Error details"):
            st.json(exc.details)


def not_available(capability: str, explanation: str | None = None) -> None:
    body = f"<b>{esc(capability)}</b> — {NOT_AVAILABLE}."
    if explanation:
        body += f"<br><span class='pp-muted'>{esc(explanation)}</span>"
    st.markdown(f'<div class="pp-notice">{body}</div>', unsafe_allow_html=True)


def empty_state(message: str, next_step: str | None = None) -> None:
    st.info(f"{message}" + (f"\n\n**Next:** {next_step}" if next_step else ""), icon="🧭")
