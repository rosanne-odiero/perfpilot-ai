"""PerfPilot AI Streamlit entrypoint.

Pages are registered in ``PAGES``. Run with ``streamlit run frontend/app.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend.theme import apply_theme  # noqa: E402

st.set_page_config(page_title="PerfPilot AI", page_icon="⚡", layout="wide")
apply_theme()

PAGES = {
    "PerfPilot": [
        st.Page("pages/system_status.py", title="System Status", icon="🩺"),
    ],
}

st.navigation(PAGES).run()
