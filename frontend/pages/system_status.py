"""System status: API health and recent jobs (real data only)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from frontend.api_client import ApiError, get

st.title("System status")

try:
    health = get("/health")
except (ApiError, Exception) as exc:
    st.error(f"API unreachable: {exc}. Start it with `make api`.")
    st.stop()

cols = st.columns(4)
cols[0].metric("API", health["status"].upper())
cols[1].metric("Database", health["database"])
cols[2].metric("LLM", health["llm_model"] or "not configured (deterministic mode)")
cols[3].metric("Sandbox backend", health["sandbox_backend"])

st.subheader("Recent jobs")
jobs = get("/jobs", params={"limit": 50})
if not jobs:
    st.info("No jobs yet.")
else:
    frame = pd.DataFrame(jobs)[
        ["id", "job_type", "status", "stage", "progress", "error", "created_at", "finished_at"]
    ]
    st.dataframe(frame, use_container_width=True, hide_index=True)
