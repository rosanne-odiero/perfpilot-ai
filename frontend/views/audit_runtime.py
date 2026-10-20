"""Runtime tab: sandbox status, commands, explicit approval, runs and MEASURED results."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend import data, routes
from frontend.api_client import ApiError, Unsupported
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, not_available, show_api_error
from frontend.components.provenance import provenance_html
from frontend.components.runtime import (
    LIMIT_FIELDS,
    LooserLimitError,
    approval_body,
    commands_frame,
    hotspots_frame,
    runs_frame,
    runtime_only_frame,
)
from frontend.formatting import fmt_dt, fmt_ms, fmt_pct, human_bytes, items_of
from frontend.theme import badge, esc, severity_badge, status_badge
from frontend.views.audit_context import AuditContext, open_in_files

RUN_JOB_KEY = "runtime_job_{audit_id}"
CHECK_JOB_KEY = "runtime_check_job"
RUN_SELECT_KEY = "runtime_run_{audit_id}"
TOP_CORRELATIONS = 5


def sandbox_status_line(status: dict[str, Any]) -> str:
    available = bool(status.get("available"))
    checked = fmt_dt(status.get("checked_at")) if status.get("checked_at") else "never"
    details = status.get("details") or {}
    extra = " · ".join(
        f"{k.replace('_', ' ')} {details[k]}"
        for k in ("operating_system", "server_version", "cgroup_version")
        if k in details
    )
    return (
        badge(
            f"SANDBOX {'AVAILABLE' if available else 'UNAVAILABLE'}",
            "#16a34a" if available else "#dc2626",
        )
        + f" {badge(str(status.get('backend', '?')), '#334155')}"
        + f" {badge(str(status.get('image', '')), '#1e293b')}"
        + f"<br><span class='pp-muted'>Last checked by a worker: {checked}"
        + (f" · {esc(extra)}" if extra else "")
        + (f" · {esc(status['reason'])}" if status.get("reason") else "")
        + "</span>"
    )


def render_sandbox_status() -> dict[str, Any] | None:
    """Availability as last observed by a worker, with a button to re-check it."""
    try:
        status = data.runtime_status()
    except ApiError as exc:
        show_api_error(exc, "Runtime status")
        return None
    if isinstance(status, Unsupported):
        return None
    left, right = st.columns([5, 1.3])
    left.markdown(sandbox_status_line(status), unsafe_allow_html=True)
    if right.button("Re-check sandbox", icon="🔄", key="runtime_refresh"):
        try:
            accepted = api.post("/runtime/status/refresh")
        except ApiError as exc:
            show_api_error(exc, "Re-check")
        else:
            st.session_state[CHECK_JOB_KEY] = accepted["job_id"]
            st.rerun()
    job_id = st.session_state.get(CHECK_JOB_KEY)
    if job_id:
        job = track_job(job_id, "Checking the sandbox")
        if job is not None:
            st.session_state.pop(CHECK_JOB_KEY, None)
            data.runtime_status.clear()
            if not job.succeeded:
                render_job_error(job, "Sandbox check")
    return dict(status)


def _start_runtime_audit(context: AuditContext) -> None:
    try:
        accepted = api.post(
            "/audits", json={"snapshot_id": context.row.snapshot_id, "mode": "RUNTIME"}
        )
    except ApiError as exc:
        show_api_error(exc, "Runtime audit rejected")
        return
    data.clear_audit_caches()
    st.switch_page(routes.AUDIT_RESULTS, query_params={routes.AUDIT_PARAM: accepted["subject_id"]})


def _limits_form(status: dict[str, Any], key: str) -> dict[str, Any]:
    defaults = status.get("default_limits") or {}
    st.markdown("**Limits** (server defaults; you can only tighten them)")
    columns = st.columns(3)
    chosen: dict[str, Any] = {}
    for index, (name, label, step) in enumerate(LIMIT_FIELDS):
        if name not in defaults:
            continue
        default = defaults[name]
        is_float = isinstance(default, float)
        chosen[name] = columns[index % 3].number_input(
            label,
            min_value=0.1 if is_float else 1,
            max_value=default,
            value=default,
            step=step,
            key=f"{key}_{name}",
        )
    st.caption("Network: none (always). Read-only root, no capabilities, non-root user.")
    return chosen


def _approval(
    context: AuditContext, command: dict[str, Any], status: dict[str, Any] | None
) -> None:
    key = f"approve_{command['id']}"
    st.markdown(f"##### Approve `{' '.join(command['argv'])}`")
    st.caption(command.get("description") or "")
    if not command.get("runnable"):
        st.warning(command.get("reason_if_not_runnable") or "This command is not runnable.")
        return
    if status is None or not status.get("available"):
        st.warning(
            "The sandbox is unavailable, so the server refuses approvals (503). Re-check it above."
        )
        return
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        st.markdown("**Exact command (pinned by the approval)**")
        st.code(repr(list(command["argv"])), language="python")
        ttl_hours = int(status.get("approval_ttl_seconds", 0)) // 3600
        st.markdown(
            f"Working directory `/workspace/{command.get('workdir_rel') or '.'}` · image "
            f"`{status.get('image')}` · single use, expires after {ttl_hours} h"
        )
        if command.get("evidence"):
            st.caption("Discovered from: " + ", ".join(f"`{e}`" for e in command["evidence"]))
        limits = _limits_form(status, key)
    with right:
        trace_memory = st.checkbox(
            "Trace memory with tracemalloc",
            value=True,
            key=f"{key}_trace",
            help="Runs tracemalloc concurrently with cProfile; it adds overhead to timings.",
        )
        install = st.checkbox(
            "Allow the separate dependency-install step",
            key=f"{key}_deps",
            help="Installs requirements.txt or pyproject dependencies before the run.",
        )
        sdist = False
        if install:
            timeout = int(status.get("dependency_install_timeout_seconds", 0))
            st.warning(
                "The install step **uses the network** (it is the only networked step) and may "
                f"take up to {timeout} s. The profiled run itself stays offline.",
                icon="🌐",
            )
            sdist = st.checkbox(
                "Allow source distributions (runs package build scripts)", key=f"{key}_sdist"
            )
        elif command.get("requires_dependencies"):
            imports = ", ".join(command.get("third_party_imports") or []) or "unknown"
            st.info(
                f"This command imports third-party packages ({imports}); without the install "
                "step it may fail.",
                icon="📦",
            )
        approved_by = st.text_input("Approved by", value="local-user", key=f"{key}_by")
        confirmed = st.checkbox(
            "I approve running the exact command above once, in the image and with the limits "
            "shown.",
            key=f"{key}_confirm",
        )
        if st.button(
            "Approve and run",
            type="primary",
            icon="▶️",
            disabled=not confirmed or not approved_by.strip(),
            key=f"{key}_go",
        ):
            try:
                body = approval_body(
                    command,
                    approved_by=approved_by.strip(),
                    limits=limits,
                    defaults=status.get("default_limits") or {},
                    allow_dependency_install=install,
                    allow_sdist_builds=sdist,
                    trace_memory=trace_memory,
                )
            except LooserLimitError as exc:
                st.error(f"Limits can only be tightened: {exc}")
                return
            try:
                accepted = api.post(f"/audits/{context.id}/runtime/approvals", json=body)
            except ApiError as exc:
                show_api_error(exc, "Approval rejected")
                return
            st.session_state[RUN_JOB_KEY.format(audit_id=context.id)] = accepted["job_id"]
            st.session_state[RUN_SELECT_KEY.format(audit_id=context.id)] = accepted["subject_id"]
            data.clear_runtime_caches()
            st.rerun()


def _memory_cards(memory: dict[str, Any]) -> list[Card]:
    traced = memory.get("tracemalloc_peak_bytes") is not None
    return [
        Card(
            "Peak RSS (whole process)",
            human_bytes(memory.get("peak_rss_bytes")),
            f"at exit {human_bytes(memory.get('rss_at_exit_bytes'))} · getrusage in the sandbox",
            "MEASURED",
        ),
        Card(
            "tracemalloc peak (Python allocations)",
            human_bytes(memory.get("tracemalloc_peak_bytes")),
            f"retained {human_bytes(memory.get('tracemalloc_retained_bytes'))}"
            if traced
            else "not traced for this run",
            "MEASURED",
        ),
    ]


def _run_cards(run: dict[str, Any]) -> list[Card]:
    flags = [
        label
        for label, on in (
            ("timed out", run.get("timed_out")),
            ("OOM-killed", run.get("oom_killed")),
        )
        if on
    ]
    duration = run.get("duration_s")
    return [
        Card(
            "Status",
            run["status"],
            run.get("failure_reason") or ", ".join(flags) or "no timeout, no OOM",
            color="#22c55e" if run["status"] == "SUCCEEDED" else "#f87171",
        ),
        Card(
            "Exit codes",
            f"{run.get('command_exit_code', '—')} / {run.get('exit_code', '—')}",
            "command / container",
        ),
        Card(
            "Wall time (profiled)",
            fmt_ms(run.get("wall_time_ms")),
            f"CPU {fmt_ms(run.get('cpu_time_ms'))}",
            "MEASURED",
        ),
        Card(
            "Total run",
            f"{duration:.1f} s" if duration is not None else "—",
            "incl. container start/stop",
        ),
        Card(
            "Profiled functions",
            str(run.get("function_count") or "—"),
            f"{fmt_ms(run.get('total_profiled_time_ms'))} profiled",
            "MEASURED",
        ),
    ]


def _correlation(corr: dict[str, Any], finding: dict[str, Any] | None, index: int) -> None:
    loc = corr["location"]
    with st.container(border=True):
        left, right = st.columns(2, gap="medium")
        with left:
            st.markdown(provenance_html("STATIC") + " **Static claim**", unsafe_allow_html=True)
            if finding:
                st.markdown(
                    f"{severity_badge(finding['severity'])} **{esc(finding['title'])}**",
                    unsafe_allow_html=True,
                )
                st.caption(finding["description"][:300])
            span = f"{loc['file_path']}:{loc['line_start']}-{loc['line_end']}"
            symbol = f" in `{loc['symbol']}`" if loc.get("symbol") else ""
            st.markdown(f"`{corr['rule_id']}` · `{span}`{symbol}")
            st.caption(f"Static priority {corr['static_priority_score']:.1f}")
            st.button(
                "Open in file viewer",
                key=f"corr_open_{index}",
                icon="📄",
                type="tertiary",
                on_click=open_in_files,
                args=(loc["file_path"], loc["line_start"], loc["line_end"]),
            )
        with right:
            st.markdown(
                provenance_html("MEASURED") + " **Measured evidence**", unsafe_allow_html=True
            )
            render_cards(
                [
                    Card(
                        "Attributed time",
                        fmt_ms(corr["attributed_time_ms"]),
                        f"{fmt_pct(corr['attributed_share'] * 100)} of profiled time",
                        "MEASURED",
                    ),
                    Card(
                        "Priority",
                        f"{corr['measured_priority_score']:.1f}",
                        f"x{corr['measured_factor']:.2f} from {corr['static_priority_score']:.1f}",
                    ),
                ]
            )
            st.caption(f"Symbol match: {loc.get('match', '?')}")


def _hotspots(run_id: str, hotspots: list[dict[str, Any]]) -> None:
    st.markdown("##### Hotspots " + provenance_html("MEASURED"), unsafe_allow_html=True)
    only_repo = st.toggle("Repository code only", value=True, key=f"rt_repo_{run_id}")
    rows = [h for h in hotspots if h["is_user_code"] or not only_repo]
    st.caption(
        "Attributed time = own lines plus direct calls into non-repository code; shares overlap "
        "and are never summed. cProfile inflates absolute times."
    )
    st.dataframe(
        hotspots_frame(rows),
        hide_index=True,
        width="stretch",
        height=min(420, 38 + 35 * max(1, len(rows))),
        column_config={
            "Attributed %": st.column_config.ProgressColumn(
                format="%.1f%%", min_value=0, max_value=100
            ),
            "Function": st.column_config.TextColumn(width="medium"),
        },
    )


def _run_extras(run: dict[str, Any]) -> None:
    with st.expander("Environment, limits, output and artifacts"):
        env = run.get("environment") or {}
        st.dataframe(
            pd.DataFrame({"Field": list(env), "Value": [str(v) for v in env.values()]}),
            hide_index=True,
            width="stretch",
        )
        if run.get("dependency_install"):
            st.markdown("**Dependency install**")
            st.json(run["dependency_install"])
        for label, field in (("stdout (tail)", "stdout_tail"), ("stderr (tail)", "stderr_tail")):
            if run.get(field):
                st.markdown(f"**{label}**")
                st.code(run[field], language="text")
        artifacts = ", ".join(f"{k}={v}" for k, v in (run.get("artifacts") or {}).items())
        st.caption(f"Artifacts (written by the profiled process, untrusted): {artifacts}")


def _run_detail(context: AuditContext, run_id: str) -> None:
    run = data.runtime_run(context.id, run_id)
    st.markdown(
        f"{provenance_html('MEASURED')} {status_badge(run['status'])} "
        f"<code>{esc(' '.join(run['argv']))}</code> · approved by {esc(run['approved_by'])} · "
        f"{fmt_dt(run.get('created_at'))}",
        unsafe_allow_html=True,
    )
    render_cards(_run_cards(run))
    st.markdown("##### Memory " + provenance_html("MEASURED"), unsafe_allow_html=True)
    st.caption(
        "RSS is the whole process (interpreter, imports, data); tracemalloc counts only Python "
        "allocations. They measure different things and are not compared with each other."
    )
    render_cards(_memory_cards(run.get("memory") or {}))
    for warning in run.get("warnings") or []:
        st.warning(str(warning), icon="⚠️")
    if run["status"] != "SUCCEEDED":
        st.error(
            "Only successful runs produce hotspots and correlations. "
            + (run.get("failure_reason") or "")
        )
    hotspots = run.get("hotspots") or []
    if hotspots:
        _hotspots(run_id, hotspots)
    correlations = run.get("correlations") or []
    if correlations:
        st.markdown(f"##### Findings confirmed by measurements ({len(correlations)})")
        findings, _ = context.findings()
        by_id = {f["id"]: f for f in findings}
        ordered = sorted(correlations, key=lambda c: -c["measured_priority_score"])
        for index, corr in enumerate(ordered[:TOP_CORRELATIONS]):
            _correlation(corr, by_id.get(corr["finding_id"]), index)
        rest = ordered[TOP_CORRELATIONS:]
        if rest:
            with st.expander(f"{len(rest)} more confirmed finding(s)"):
                for index, corr in enumerate(rest, start=TOP_CORRELATIONS):
                    _correlation(corr, by_id.get(corr["finding_id"]), index)
    runtime_only = run.get("runtime_only_hotspots") or []
    if runtime_only:
        st.markdown(
            "##### Runtime-only hotspots " + provenance_html("MEASURED"), unsafe_allow_html=True
        )
        st.caption("Hot repository code that no static rule flagged.")
        st.dataframe(runtime_only_frame(runtime_only), hide_index=True, width="stretch")
    if hotspots or runtime_only:
        st.caption(
            "Patch, validate and benchmark deep dives run on registered workloads in the Workload "
            "Lab; repository hotspots are measured here but never patched automatically."
        )
    _run_extras(run)


def _runs(context: AuditContext, overview: dict[str, Any]) -> None:
    runs = overview.get("runs") or []
    if not runs:
        st.caption("No runs yet. Approve a discovered command above to measure it.")
        return
    st.markdown("##### Runs")
    st.dataframe(runs_frame(runs), hide_index=True, width="stretch")
    labels = {
        r["id"]: f"{fmt_dt(r['created_at'])} · {' '.join(r['argv'])} · {r['status']}" for r in runs
    }
    select_key = RUN_SELECT_KEY.format(audit_id=context.id)
    if st.session_state.get(select_key) not in labels:
        st.session_state[select_key] = overview.get("measured_run_id") or runs[0]["id"]
    run_id = st.selectbox("Run", list(labels), format_func=labels.__getitem__, key=select_key)
    if overview.get("measured_run_id") == run_id:
        st.caption("This run's evidence is attached to the findings and the report (section 7).")
    _run_detail(context, run_id)


def _track_run(context: AuditContext) -> bool:
    """Return True while an approved run is still executing (a live tracker is shown)."""
    key = RUN_JOB_KEY.format(audit_id=context.id)
    job_id = st.session_state.get(key)
    if not job_id:
        return False
    job = track_job(job_id, "Runtime run in the sandbox")
    if job is None:
        return True
    st.session_state.pop(key, None)
    data.clear_runtime_caches()
    if not job.succeeded:
        render_job_error(job, "Runtime run")
    return False


def render(context: AuditContext) -> None:
    commands_payload = data.audit_optional(context.id, "runtime/commands")
    if isinstance(commands_payload, Unsupported):
        not_available(
            "Runtime audit",
            "Discovered commands, the approval flow and MEASURED hotspots appear here once the "
            "API serves /api/audits/{id}/runtime/commands.",
        )
        return
    status = render_sandbox_status()
    if context.audit.get("mode") != "RUNTIME":
        st.info(
            "This is a static audit: nothing was executed. Runtime audits discover runnable "
            "commands; start one for this snapshot to approve and measure a run.",
            icon="🛡️",
        )
        if st.button("Start a runtime audit of this snapshot", icon="🚀", key="rt_start_audit"):
            _start_runtime_audit(context)
        return
    overview = data.audit_optional(context.id, "runtime")
    overview = overview if isinstance(overview, dict) else {}
    for warning in overview.get("warnings") or []:
        st.warning(str(warning), icon="⚠️")
    if _track_run(context):
        return
    commands = items_of(commands_payload)
    if not commands:
        empty_state(
            "No runnable commands were discovered (tests, benchmarks, scripts).",
            "Add a pytest suite or a benchmark script to the repository and re-run the audit.",
        )
    else:
        st.markdown(
            f"##### Discovered commands ({len(commands)}) " + provenance_html("STATIC"),
            unsafe_allow_html=True,
        )
        st.caption("Proposals found by reading the repository. Nothing runs until you approve one.")
        st.dataframe(
            commands_frame(commands),
            hide_index=True,
            width="stretch",
            column_config={"Evidence": st.column_config.TextColumn(width="medium")},
        )
        labels = {c["id"]: f"{c['kind']} · {' '.join(c['argv'])}" for c in commands}
        chosen = st.selectbox(
            "Review a command",
            [None, *labels],
            format_func=lambda cid: (
                "Select a command to review and approve" if cid is None else labels[cid]
            ),
            key=f"runtime_command_{context.id}",
        )
        if chosen is not None:
            with st.container(border=True):
                _approval(context, next(c for c in commands if c["id"] == chosen), status)
    _runs(context, overview)
