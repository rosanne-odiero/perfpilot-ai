"""Reports & History: audits and their reports, workload experiments, profiles and benchmarks."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import data, routes
from frontend.api_client import ApiError, Unsupported
from frontend.components.notices import empty_state, show_api_error
from frontend.components.provenance import provenance_html
from frontend.components.report_export import render_report_list, report_row
from frontend.formatting import SOURCE_LABELS, fmt_dt, fmt_ms, fmt_ratio
from frontend.views.catalog import AuditRow, audit_rows
from frontend.views.run_history import runs


def _audits(rows: list[AuditRow]) -> str | None:
    frame = pd.DataFrame(
        [
            {
                "Repository": r.repo_name,
                "Source": r.source_label,
                "Commit": (r.commit_sha or "")[:8],
                "Mode": r.mode,
                "Status": r.status,
                "Findings": r.findings_total,
                "Started": fmt_dt(r.created_at),
            }
            for r in rows
        ]
    )
    state = st.dataframe(
        frame,
        key="history_audits",
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
    )
    selected = state.selection.rows if state is not None else []
    return rows[selected[0]].id if selected and selected[0] < len(rows) else None


def _audit_section() -> None:
    st.markdown("##### Audits and reports")
    rows = audit_rows(200)
    if not rows:
        empty_state("No audits yet.", "Start one on the Repository Audit page.")
        return
    audit_id = _audits(rows)
    if audit_id is None:
        st.caption("Select an audit to open it or download its reports.")
        return
    row = next(r for r in rows if r.id == audit_id)
    st.page_link(
        routes.AUDIT_RESULTS,
        label=f"Open {row.repo_name} in Audit Results",
        icon="📊",
        query_params={routes.AUDIT_PARAM: audit_id},
    )
    reports = data.reports(audit_id)
    if not reports:
        st.caption("No reports generated for this audit yet (use its Export tab).")
        return
    with st.container(border=True):
        for report in sorted(reports, key=lambda r: (r["format"], r["created_at"])):
            report_row(report)


def experiments_frame(experiments: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Workload": e.get("workload_name") or e.get("workload_slug") or e["workload_id"],
                "Experiment": e["title"],
                "Kind": "optimization" if e["kind"] == "optimization" else "reference benchmark",
                "State": e["state"],
                "Validation": e.get("validation_status") or "",
                "Verdict": e.get("classification") or "",
                "Speedup": fmt_ratio(e.get("speedup")),
                "95% CI": f"{fmt_ratio(e['speedup_ci_low'])} to {fmt_ratio(e['speedup_ci_high'])}"
                if e.get("speedup_ci_low") is not None
                else "",
                "Reports": e.get("report_count", 0),
                "Created": fmt_dt(e["created_at"]),
            }
            for e in experiments
        ],
        columns=[
            "Workload",
            "Experiment",
            "Kind",
            "State",
            "Validation",
            "Verdict",
            "Speedup",
            "95% CI",
            "Reports",
            "Created",
        ],
    )


def _experiments_section() -> None:
    payload = data.experiments(100)
    if isinstance(payload, Unsupported):
        return
    st.markdown("##### Workload experiments " + provenance_html("MEASURED"), unsafe_allow_html=True)
    experiments = list(payload)
    if not experiments:
        st.caption(
            "No experiments yet. Run an optimization deep dive or a reference benchmark in the "
            "Workload Lab."
        )
        return
    state = st.dataframe(
        experiments_frame(experiments),
        key="history_experiments",
        hide_index=True,
        width="stretch",
        on_select="rerun",
        selection_mode="single-row",
        column_config={"Experiment": st.column_config.TextColumn(width="large")},
    )
    rows = state.selection.rows if state is not None else []
    if not rows or rows[0] >= len(experiments):
        st.caption("Select an experiment to open it or download its reports.")
        return
    chosen = experiments[rows[0]]
    st.page_link(
        routes.WORKLOAD_LAB,
        label=f"Open {chosen.get('workload_name') or 'the workload'} in the Workload Lab",
        icon="🧪",
        query_params={routes.WORKLOAD_PARAM: chosen["workload_id"]},
    )
    base = "/optimizations" if chosen["subject_type"] == "optimization" else "/benchmarks"
    reports = data.subject_reports(f"{base}/{chosen['id']}/reports")
    if reports:
        render_report_list(reports)
    else:
        st.caption(
            "No reports for this experiment yet; generate them in the Workload Lab "
            "(Optimize tab for candidates, Benchmark tab for reference benchmarks)."
        )


def _workload_names() -> dict[str, str]:
    return {w["id"]: w["name"] for w in data.all_workloads()}


def _runs_section() -> None:
    names = _workload_names()
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("##### Profiles")
        profiles = runs("profile", limit=50)
        if not profiles:
            st.caption("No profiles yet. Run one in the Workload Lab.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Workload": names.get(p.workload_id, p.workload_id),
                            "Size": p.run.get("size"),
                            "Status": p.run.get("status"),
                            "Profiled time": fmt_ms(p.run.get("total_profiled_time_ms")),
                            "Started": fmt_dt(p.created_at),
                        }
                        for p in profiles
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
    with right:
        st.markdown("##### Benchmarks")
        benchmarks = runs("benchmark", limit=50)
        if not benchmarks:
            st.caption("No benchmarks yet. Run one in the Workload Lab.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Workload": names.get(b.workload_id, b.workload_id),
                            "Size": b.run.get("size"),
                            "Verdict": b.run.get("classification") or b.run.get("status"),
                            "Speedup": fmt_ratio(b.run.get("speedup")),
                            "Candidate": (b.run.get("candidate") or {}).get("label"),
                            "Started": fmt_dt(b.created_at),
                        }
                        for b in benchmarks
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
    st.page_link(routes.WORKLOAD_LAB, label="Open the Workload Lab", icon="🧪")


def _repositories() -> None:
    repos = data.repositories()
    if not repos:
        return
    with st.expander(f"Imported repositories ({len(repos)})"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Name": r["name"],
                        "Source": SOURCE_LABELS.get(r["source_type"], r["source_type"]),
                        "Location": r["source_locator"],
                        "Ref": r.get("requested_ref") or "",
                        "Latest snapshot": r.get("latest_snapshot_status") or "",
                        "Imported": fmt_dt(r["created_at"]),
                    }
                    for r in repos
                ]
            ),
            hide_index=True,
            width="stretch",
        )


def render_page() -> None:
    st.title("Reports & history")
    try:
        _audit_section()
        _repositories()
        st.divider()
        _experiments_section()
        st.divider()
        _runs_section()
    except ApiError as exc:
        show_api_error(exc)
