"""PerfPilot AI Streamlit entrypoint. Run with ``streamlit run frontend/app.py``.

Pages are registered in ``PAGES``; the landing page is Repository Audit.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from frontend import routes  # noqa: E402
from frontend.theme import apply_theme  # noqa: E402
from frontend.views.sidebar import render_sidebar  # noqa: E402

st.set_page_config(page_title="PerfPilot AI", page_icon="⚡", layout="wide")
apply_theme()

PAGES = {
    "Repository audit": [
        st.Page(routes.REPOSITORY_AUDIT, title="Repository Audit", icon="🔎", default=True),
        st.Page(routes.AUDIT_RESULTS, title="Audit Results", icon="📊"),
    ],
    "Deep dive": [
        st.Page(routes.WORKLOAD_LAB, title="Workload Lab", icon="🧪"),
    ],
    "Workspace": [
        st.Page(routes.REPORTS_HISTORY, title="Reports & History", icon="🗂️"),
        st.Page(routes.SYSTEM_STATUS, title="System Status", icon="🩺"),
    ],
}

page = st.navigation(PAGES)
render_sidebar()
page.run()
