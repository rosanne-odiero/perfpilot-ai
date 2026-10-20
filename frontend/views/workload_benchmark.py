"""Benchmark tab: paired A/B runs of the baseline against a candidate, from raw samples."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError
from frontend.components.benchmark_charts import (
    CLASSIFICATION_COLORS,
    distribution_figure,
    memory_figure,
    memory_frame,
    noise_warnings,
    paired_ratios,
    ratio_figure,
    timing_frame,
)
from frontend.components.job_progress import render_job_error, track_job
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import NOT_AVAILABLE, empty_state, show_api_error
from frontend.components.provenance import provenance_html
from frontend.formatting import fmt_dt, fmt_ms, fmt_pct, fmt_ratio, human_bytes
from frontend.theme import badge, esc
from frontend.views.run_history import runs

JOB_KEY = "benchmark_job_{workload_id}"
SELECT_KEY = "benchmark_run_{workload_id}"
REFERENCE_LABEL = "reference implementation (bundled, for testing)"
ENV_FIELDS = (
    "cpu_model",
    "logical_cpus",
    "os",
    "os_release",
    "architecture",
    "python_version",
    "python_implementation",
    "perfpilot_version",
    "uv_lock_sha256",
    "load_average_1_5_15",
    "captured_at",
)


def _candidate_choice(detail: dict[str, Any], optimizations: bool) -> tuple[Any, bool]:
    options = []
    if detail.get("has_reference_candidate"):
        options.append("reference")
    if optimizations:
        options.append("candidate")
    if not options:
        st.warning("This workload has no bundled reference implementation to compare against.")
        return None, False
    choice = st.radio(
        "Compare the baseline against",
        options,
        horizontal=True,
        format_func=lambda o: REFERENCE_LABEL if o == "reference" else "a validated candidate",
    )
    if not optimizations:
        st.caption(
            f"Validated optimization candidates: {NOT_AVAILABLE} (needs the optimization API)."
        )
    if choice == "reference":
        return "reference", True
    candidate_id = st.text_input("Validated candidate id")
    return {"candidate_id": candidate_id.strip()}, bool(candidate_id.strip())


def _start_form(detail: dict[str, Any]) -> None:
    workload_id = detail["id"]
    with st.container(border=True):
        candidate, ready = _candidate_choice(detail, data.optimization_api_available())
        cols = st.columns(4)
        sizes = detail["sizes"]
        size = cols[0].selectbox(
            "Input size", sizes, index=sizes.index(detail["default_size"]), key="bm_size"
        )
        measured_runs = cols[1].number_input("Measured runs (pairs)", 3, 500, 20, key="bm_runs")
        warmups = cols[2].number_input("Warm-up runs", 0, 100, 5, key="bm_warmups")
        memory_runs = cols[3].number_input("Memory runs", 0, 50, 3, key="bm_mem")
        with st.expander("Advanced settings"):
            adv = st.columns(3)
            threshold = adv[0].number_input(
                "Practical threshold (%)",
                0.5,
                100.0,
                5.0,
                0.5,
                key="bm_threshold",
                help="Smaller differences are classified as no clear difference.",
            )
            order = adv[1].selectbox("Run order", ["random", "alternate"], key="bm_order")
            seed = adv[2].number_input("Seed", 0, 2**32 - 1, 12345, key="bm_seed")
        if st.button("Run benchmark", type="primary", icon="🏁", disabled=not ready):
            body = {
                "workload_id": workload_id,
                "candidate": candidate,
                "size": size,
                "runs": int(measured_runs),
                "warmups": int(warmups),
                "memory_runs": int(memory_runs),
                "threshold_percent": float(threshold),
                "order": order,
                "seed": int(seed),
            }
            try:
                accepted = api.post("/benchmarks", json=body)
            except ApiError as exc:
                show_api_error(exc, "Benchmark request rejected")
                return
            st.session_state[JOB_KEY.format(workload_id=workload_id)] = accepted["job_id"]
            st.session_state[SELECT_KEY.format(workload_id=workload_id)] = accepted["subject_id"]
            data.jobs.clear()
            st.rerun()


def _labels(run: dict[str, Any]) -> dict[str, str]:
    candidate = run.get("candidate") or {}
    return {"baseline": "baseline", "candidate": str(candidate.get("label") or "candidate")}


def _headline(run: dict[str, Any]) -> None:
    classification = run.get("classification") or "UNCLASSIFIED"
    color = CLASSIFICATION_COLORS.get(classification, "#64748b")
    candidate = (run.get("candidate") or {}).get("label") or "candidate"
    st.markdown(
        f"{badge(classification.replace('_', ' '), color)} {provenance_html('MEASURED')} "
        f"<span class='pp-muted'>baseline vs <b>{esc(candidate)}</b> · size {esc(run['size'])}"
        "</span>",
        unsafe_allow_html=True,
    )
    if run.get("classification_reason"):
        st.caption(run["classification_reason"])
    summary = run.get("summary") or {}
    base = (summary.get("baseline") or {}).get("wall_ms") or {}
    cand = (summary.get("candidate") or {}).get("wall_ms") or {}
    ci = (
        f"95% CI {fmt_ratio(run['speedup_ci_low'])} to {fmt_ratio(run['speedup_ci_high'])}"
        if run.get("speedup_ci_low") is not None
        else "no CI"
    )
    throughput_b = (summary.get("baseline") or {}).get("throughput_calls_per_s")
    throughput_c = (summary.get("candidate") or {}).get("throughput_calls_per_s")
    render_cards(
        [
            Card(
                "Baseline median", fmt_ms(base.get("median")), f"n={base.get('n', 0)}", "MEASURED"
            ),
            Card(
                "Candidate median", fmt_ms(cand.get("median")), f"n={cand.get('n', 0)}", "MEASURED"
            ),
            Card("Speedup", fmt_ratio(run.get("speedup")), ci, "MEASURED", color=color),
            Card(
                "Latency reduction",
                fmt_pct(run.get("latency_reduction_percent")),
                "median wall time",
                "MEASURED",
            ),
            Card("CPU-time speedup", fmt_ratio(summary.get("cpu_speedup")), None, "MEASURED"),
            Card(
                "Throughput",
                f"{throughput_c:,.0f}/s" if throughput_c else "—",
                f"baseline {throughput_b:,.0f}/s" if throughput_b else None,
                "MEASURED",
            ),
        ]
    )


def _correctness(run: dict[str, Any]) -> None:
    correctness = run.get("correctness") or {}
    if not correctness:
        return
    if correctness.get("equivalent"):
        comparison, detail = correctness.get("comparison"), correctness.get("detail", "")
        st.success(f"Outputs equivalent ({comparison}): {detail}", icon="✅")
    else:
        st.error(f"Outputs differ: {correctness.get('detail', 'no detail')}", icon="❌")


def _charts(run: dict[str, Any]) -> None:
    samples = data.benchmark_samples(run["id"])
    frame = timing_frame(samples)
    if frame.empty:
        st.caption("No successful timing samples were recorded.")
        return
    controls = st.columns([1, 1, 3])
    metric = controls[0].radio(
        "Metric", ["wall_ms", "cpu_ms"], format_func=lambda m: m.split("_")[0].upper() + " time"
    )
    speedup = run.get("speedup") or 1.0
    log_y = controls[1].toggle("Log scale", value=not 0.1 <= speedup <= 10)
    labels = _labels(run)
    left, right = st.columns(2)
    left.plotly_chart(
        distribution_figure(frame, metric=metric, log_y=log_y, labels=labels), theme=None
    )
    right.plotly_chart(
        ratio_figure(
            paired_ratios(frame, metric),
            speedup=run.get("speedup") if metric == "wall_ms" else None,
            ci=(run.get("speedup_ci_low"), run.get("speedup_ci_high"))
            if metric == "wall_ms"
            else (None, None),
        ),
        theme=None,
    )
    memory = memory_frame(samples)
    if not memory.empty:
        st.markdown("##### Memory " + provenance_html("MEASURED"), unsafe_allow_html=True)
        st.plotly_chart(memory_figure(memory, labels), theme=None)
        mem = run.get("memory") or {}
        ratio = mem.get("tracemalloc_peak_ratio_candidate_to_baseline")
        rows = [
            {
                "Variant": labels[variant],
                "Runs": (mem.get(variant) or {}).get("runs"),
                "tracemalloc peak (median)": human_bytes(
                    (mem.get(variant) or {}).get("tracemalloc_peak_bytes_median")
                ),
                "RSS peak (median)": human_bytes(
                    (mem.get(variant) or {}).get("rss_peak_bytes_median")
                ),
            }
            for variant in ("baseline", "candidate")
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if ratio is not None:
            st.caption(f"tracemalloc peak ratio candidate/baseline: {ratio:.2f}")
        for name, text in (mem.get("definitions") or {}).items():
            st.caption(f"`{name}`: {text}")


def _environment(run: dict[str, Any]) -> None:
    env = run.get("environment") or {}
    with st.expander("Environment fingerprint, settings and formulas"):
        rows = [{"Field": k, "Value": str(env[k])} for k in ENV_FIELDS if k in env]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        for note in env.get("notes") or []:
            st.caption(f"• {note}")
        st.markdown("**Settings**")
        st.json(run.get("settings") or {})
        summary = run.get("summary") or {}
        if summary.get("formulas"):
            st.markdown("**Formulas**")
            st.json({**summary["formulas"], "ci": summary.get("ci")})


def _show(run: dict[str, Any]) -> None:
    if run["status"] in {"PENDING", "QUEUED", "RUNNING"}:
        st.info("This benchmark is still running; results appear when it finishes.")
        return
    if run["status"] != "SUCCEEDED":
        st.error(f"Benchmark {run['status'].lower()}: {run.get('error') or 'no details'}")
        if run.get("error_details"):
            st.json(run["error_details"])
        return
    _headline(run)
    _correctness(run)
    for warning in noise_warnings(run):
        st.warning(warning, icon="📉")
    _charts(run)
    _environment(run)


def _run_label(run: dict[str, Any], created: str) -> str:
    verdict = (run.get("classification") or run.get("status", "")).lower().replace("_", " ")
    return f"{fmt_dt(created)} · {run.get('size')} · {verdict} · {fmt_ratio(run.get('speedup'))}"


def render(detail: dict[str, Any]) -> None:
    workload_id = detail["id"]
    st.markdown(
        "Paired A/B benchmark in fresh processes, randomized order, bootstrap CI "
        + provenance_html("MEASURED"),
        unsafe_allow_html=True,
    )
    _start_form(detail)
    job_key = JOB_KEY.format(workload_id=workload_id)
    if job_key in st.session_state:
        job = track_job(st.session_state[job_key], "Benchmarking")
        if job is None:
            return
        st.session_state.pop(job_key)
        if not job.succeeded:
            render_job_error(job, "Benchmark job")
    history = runs("benchmark", workload_id)
    if not history:
        empty_state(
            "No benchmarks yet for this workload.", "Configure the run and press Run benchmark."
        )
        return
    labels = {ref.id: _run_label(ref.run, ref.created_at) for ref in history}
    select_key = SELECT_KEY.format(workload_id=workload_id)
    if st.session_state.get(select_key) not in labels:
        st.session_state[select_key] = history[0].id
    benchmark_id = st.selectbox(
        "Benchmark run", list(labels), format_func=labels.__getitem__, key=select_key
    )
    _show(next(ref.run for ref in history if ref.id == benchmark_id))
