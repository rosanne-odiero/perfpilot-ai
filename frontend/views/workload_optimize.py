"""Optimize tab: analysis, recommendation, candidate patch, approval, validation, benchmark."""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError, Unsupported
from frontend.components.benchmark_charts import CLASSIFICATION_COLORS
from frontend.components.job_progress import render_job_error, stepper_html, track_job
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, not_available
from frontend.components.optimization import (
    VALIDATION_COLORS,
    benchmark_block_reason,
    checks_frame,
    history_frame,
    lifecycle_steps,
)
from frontend.components.provenance import provenance_html
from frontend.components.report_export import ExportTarget, render_export
from frontend.formatting import fmt_dt, fmt_ms, fmt_pct, fmt_ratio
from frontend.theme import badge, esc, status_badge
from frontend.views.candidate_review import (
    ACTION_ERROR_KEY,
    CandidateApi,
    candidate_header,
    patch_validator_badges,
    post_action,
    rejection_text,
    render_checks,
    render_diff,
    render_review,
    show_action_error,
    track_candidate_job,
)
from frontend.views.run_history import runs
from frontend.views.workload_benchmark import show_run

ANALYSIS_KEY = "opt_analysis_{wid}"
ANALYSIS_JOB_KEY = "opt_analysis_job_{wid}"
CANDIDATE_KEY = "opt_candidate_{wid}"
CREATED_KEY = "opt_created_{wid}"
CANDIDATES = CandidateApi(
    base="/optimizations",
    load_diff=data.candidate_diff,
    clear=data.clear_optimization_caches,
)
IMPACT_COLORS = {"high": "#16a34a", "moderate": "#0891b2", "low": "#64748b"}


# --- analysis -------------------------------------------------------------------------------


def start_analysis(workload_id: str, profile_id: str | None, size: str | None) -> None:
    """Widget callback (also used by the Profile tab's "Optimize this hotspot")."""
    body: dict[str, Any] = {"workload_id": workload_id}
    if profile_id:
        body["profile_id"] = profile_id
    elif size:
        body["size"] = size
    try:
        accepted = api.post("/analyses", json=body)
    except ApiError as exc:
        st.session_state[ACTION_ERROR_KEY] = exc
        return
    st.session_state[ANALYSIS_KEY.format(wid=workload_id)] = accepted["subject_id"]
    st.session_state[ANALYSIS_JOB_KEY.format(wid=workload_id)] = accepted["job_id"]
    st.session_state.pop(CANDIDATE_KEY.format(wid=workload_id), None)
    data.jobs.clear()


def _analysis_form(detail: dict[str, Any]) -> None:
    wid = detail["id"]
    profiles = [p for p in runs("profile", wid) if p.run.get("status") == "SUCCEEDED"]
    options: dict[str, str] = {"": "Profile as part of the analysis"}
    options |= {
        p.id: f"Reuse profile {fmt_dt(p.created_at)} · {p.run.get('size')} · "
        f"{fmt_ms(p.run.get('total_profiled_time_ms'))}"
        for p in profiles
    }
    with st.form(f"analysis_form_{wid}", border=True):
        cols = st.columns([2.4, 1, 1])
        profile_id = cols[0].selectbox(
            "Measured evidence", list(options), format_func=options.__getitem__
        )
        sizes = detail["sizes"]
        size = cols[1].selectbox("Profile size", sizes, index=sizes.index(detail["default_size"]))
        submitted = cols[2].form_submit_button("Run analysis", type="primary", icon="🔬")
    if submitted:
        start_analysis(wid, profile_id or None, size)
        st.rerun()


def _select_analysis(detail: dict[str, Any]) -> dict[str, Any] | None:
    wid = detail["id"]
    history = runs("analysis", wid)
    if not history:
        return None
    labels = {
        a.id: f"{fmt_dt(a.created_at)} · {str(a.run.get('state', '')).lower()} · "
        f"{len(a.run.get('recommendation_ids') or [])} recommendation(s)"
        for a in history
    }
    key = ANALYSIS_KEY.format(wid=wid)
    if st.session_state.get(key) not in labels:
        st.session_state[key] = history[0].id
    chosen = st.selectbox("Analysis", list(labels), format_func=labels.__getitem__, key=key)
    return data.analysis(chosen)


def _uncovered_label(item: Any) -> str:
    return f"`{item.get('function_name', '?')}`" if isinstance(item, dict) else f"`{item}`"


def _analysis_summary(analysis: dict[str, Any]) -> None:
    summary = analysis.get("summary") or {}
    size = analysis.get("profile_size") or summary.get("profile_size") or "—"
    st.markdown(
        f"{status_badge(analysis['state'])} <span class='pp-muted'>profile "
        f"<code>{esc(str(analysis.get('profile_run_id')))}</code> · size {esc(str(size))} · "
        f"{summary.get('files_analyzed', 0)} file(s) analyzed · "
        f"{summary.get('static_findings', 0)} static finding(s)</span>",
        unsafe_allow_html=True,
    )
    if analysis.get("error"):
        st.error(analysis["error"])
    for warning in analysis.get("warnings") or []:
        st.warning(str(warning), icon="⚠️")
    uncovered = summary.get("hotspots_without_rule") or []
    if uncovered:
        st.caption(
            "Measured hotspots that no rule explains: "
            + ", ".join(_uncovered_label(h) for h in uncovered)
        )


# --- recommendations ------------------------------------------------------------------------


def _evidence(evidence: dict[str, Any]) -> None:
    target = evidence.get("target_function") or {}
    st.markdown(provenance_html("MEASURED") + " **Profiler evidence**", unsafe_allow_html=True)
    if not target:
        st.caption("No profiler evidence for the flagged function.")
        return
    render_cards(
        [
            Card(
                "Cumulative share",
                fmt_pct(target.get("cumulative_percentage")),
                f"{fmt_ms(target.get('cumulative_time_ms'))} cumulative",
                "MEASURED",
            ),
            Card(
                "Exclusive",
                fmt_ms(target.get("internal_time_ms")),
                f"{fmt_pct(target.get('percentage_of_profiled_time'))} · "
                f"{target.get('total_calls', '—')} call(s)",
                "MEASURED",
            ),
        ]
    )
    st.caption(
        f"`{target.get('function_name')}` at {target.get('file_path')}:{target.get('line_number')}"
        f" · profile {evidence.get('profile_id')} ({evidence.get('size')}). "
        + (evidence.get("note") or "")
    )
    outside = evidence.get("largest_non_workspace_functions") or []
    if outside:
        top = outside[0]
        st.caption(
            f"Largest outside the workspace: `{top.get('function_name')}` "
            f"{fmt_pct(top.get('percentage_of_profiled_time'))} exclusive "
            f"({top.get('total_calls')} calls), not attributed to a caller."
        )


def _impact(rec: dict[str, Any]) -> None:
    impact = rec.get("estimated_impact") or {}
    provenance = str(impact.get("provenance") or "AI_HYPOTHESIS")
    level = str(impact.get("level") or "unknown")
    st.markdown(
        provenance_html(provenance) + " **Qualitative impact** (never a number)",
        unsafe_allow_html=True,
    )
    st.markdown(
        badge(f"impact {level}", IMPACT_COLORS.get(level, "#ca8a04"))
        + f" <span class='pp-muted'>{esc(impact.get('rationale', ''))}</span>",
        unsafe_allow_html=True,
    )
    st.info(rec.get("expected_effect") or "—", icon="🔮")
    if impact.get("note"):
        st.caption(impact["note"])


def _create_candidate(rec_id: str, wid: str, use_llm: bool) -> None:
    body: dict[str, Any] = {"recommendation_id": rec_id}
    if use_llm:
        body["use_llm"] = True
    try:
        candidate = api.post("/optimizations", json=body)
    except ApiError as exc:
        st.session_state[ACTION_ERROR_KEY] = exc
        return
    st.session_state[CANDIDATE_KEY.format(wid=wid)] = candidate["id"]
    st.session_state[CREATED_KEY.format(wid=wid)] = candidate["id"]
    data.clear_optimization_caches()


def _llm_model() -> str | None:
    """The configured model id, if the server has an LLM (the LLM proposer needs one)."""
    try:
        status = data.ai_status()
    except ApiError:
        return None
    if isinstance(status, dict) and status.get("configured"):
        return str(status.get("model"))
    return None


def _recommendation(rec: dict[str, Any], wid: str, existing: list[dict[str, Any]]) -> None:
    with st.container(border=True):
        st.markdown(
            f"**#{rec['rank']} · {esc(rec['title'])}** "
            + badge(f"confidence {rec.get('confidence', '?')}", "#334155")
            + " "
            + badge(f"rule {rec.get('rule_id') or '—'}", "#1e293b")
            + " "
            + badge(str(rec.get("generated_by") or "deterministic"), "#1e293b"),
            unsafe_allow_html=True,
        )
        lines = rec.get("source_lines") or {}
        st.caption(
            f"Target `{rec['target_file']}` · `{rec.get('target_function') or 'module'}` lines "
            f"{lines.get('start', '?')}-{lines.get('end', '?')}"
        )
        st.markdown(
            provenance_html("MEASURED")
            + " "
            + provenance_html("STATIC")
            + " "
            + esc(rec.get("observed_bottleneck", "")),
            unsafe_allow_html=True,
        )
        left, right = st.columns(2, gap="medium")
        with left:
            _evidence(rec.get("profiler_evidence") or {})
        with right:
            _impact(rec)
        st.markdown(
            provenance_html("STATIC")
            + f" **Root cause.** {esc(rec.get('suspected_root_cause', ''))}",
            unsafe_allow_html=True,
        )
        st.markdown(f"**Proposed optimization.** {rec.get('proposed_optimization', '')}")
        risks = rec.get("correctness_risks") or []
        if risks:
            st.warning("\n".join(f"- {r}" for r in risks), icon="⚖️")
        if rec.get("memory_tradeoffs"):
            st.caption(f"Memory trade-off: {rec['memory_tradeoffs']}")
        with st.expander("Validation plan"):
            for step in rec.get("validation_plan") or []:
                st.markdown(f"- {step}")
        strategy = rec.get("patch_strategy") or {}
        available = bool(strategy.get("deterministic_patch_available"))
        if existing:
            st.caption(
                "Candidates: "
                + ", ".join(f"{c['id'][:8]} ({c['state'].lower()})" for c in existing)
            )
        use_llm = False
        model = _llm_model()
        if not available:
            st.caption(f"No safe deterministic patch: {strategy.get('reason') or 'unknown'}")
            if model:
                use_llm = st.checkbox(
                    f"Ask `{model}` for a patch instead. I consent to sending this function's "
                    "source (redacted) and its evidence; the result is an AI_HYPOTHESIS that "
                    "still goes through the patch validator, approval and validation.",
                    key=f"llm_{rec['id']}",
                )
        st.button(
            "Generate candidate patch",
            key=f"gen_{rec['id']}",
            type="secondary" if existing else "primary",
            icon="🩹",
            disabled=not (available or use_llm),
            on_click=_create_candidate,
            args=(rec["id"], wid, use_llm),
        )


# --- candidate ------------------------------------------------------------------------------


def _validation(candidate: dict[str, Any]) -> None:
    validation = candidate.get("validation")
    if not validation:
        return
    status = str(validation.get("status"))
    duration = validation.get("duration_seconds")
    st.markdown(
        "##### Validation "
        + badge(status, VALIDATION_COLORS.get(status, "#64748b"))
        + f" <span class='pp-muted'>{esc(validation.get('summary', ''))}"
        + (f" · {duration:.1f} s" if duration is not None else "")
        + "</span>",
        unsafe_allow_html=True,
    )
    checks = validation.get("checks") or []
    if checks:
        render_checks(checks_frame(checks), checks)
    if validation.get("error"):
        st.error(validation["error"])
    st.caption(
        f"Original tree {str(validation.get('original_tree_sha256'))[:12]} · patched tree "
        f"{str(validation.get('patched_tree_sha256'))[:12]} · "
        f"{len(validation.get('baseline_input_hashes') or {})} baseline input hash(es) recorded"
    )


def _benchmark_form(candidate: dict[str, Any], detail: dict[str, Any]) -> None:
    cid = candidate["id"]
    reason = benchmark_block_reason(candidate)
    st.markdown(
        "##### Benchmark the validated candidate " + provenance_html("MEASURED"),
        unsafe_allow_html=True,
    )
    if reason:
        st.caption(reason)
        if candidate["state"] in {"BENCHMARKING", "COMPLETED"}:
            return
    cols = st.columns(4)
    sizes = detail["sizes"]
    size = cols[0].selectbox(
        "Input size", sizes, index=sizes.index(detail["default_size"]), key=f"obs_{cid}"
    )
    runs_n = cols[1].number_input("Measured runs (pairs)", 3, 500, 20, key=f"obr_{cid}")
    warmups = cols[2].number_input("Warm-up runs", 0, 100, 5, key=f"obw_{cid}")
    memory = cols[3].number_input("Memory runs", 0, 50, 3, key=f"obm_{cid}")
    body = {
        "workload_id": detail["id"],
        "candidate": {"candidate_id": cid},
        "size": size,
        "runs": int(runs_n),
        "warmups": int(warmups),
        "memory_runs": int(memory),
    }
    st.button(
        "Benchmark candidate vs baseline",
        type="primary",
        icon="🏁",
        key=f"bench_{cid}",
        disabled=reason is not None,
        on_click=post_action,
        args=(CANDIDATES, "/benchmarks", body, cid, "benchmark"),
    )


def _benchmark_result(candidate: dict[str, Any]) -> None:
    summary = candidate.get("benchmark")
    if not summary:
        return
    classification = str(summary.get("classification") or "UNCLASSIFIED")
    low, high = (summary.get("speedup_ci") or [None, None])[:2]
    st.markdown(
        "##### Benchmark result "
        + badge(
            classification.replace("_", " "), CLASSIFICATION_COLORS.get(classification, "#64748b")
        )
        + " "
        + provenance_html("MEASURED")
        + f" <span class='pp-muted'>benchmark <code>{esc(summary['id'])}</code> linked to this "
        f"candidate · size {esc(str(summary.get('size')))}</span>",
        unsafe_allow_html=True,
    )
    render_cards(
        [
            Card("Baseline median", fmt_ms(summary.get("median_baseline_ms")), None, "MEASURED"),
            Card("Candidate median", fmt_ms(summary.get("median_candidate_ms")), None, "MEASURED"),
            Card(
                "Speedup",
                fmt_ratio(summary.get("speedup")),
                f"95% CI {fmt_ratio(low)} to {fmt_ratio(high)}" if low is not None else None,
                "MEASURED",
            ),
            Card(
                "Latency reduction",
                fmt_pct(summary.get("latency_reduction_percent")),
                None,
                "MEASURED",
            ),
        ]
    )
    with st.expander("Raw samples and the full benchmark (same view as the Benchmark tab)"):
        show_run(data.benchmark(summary["id"]))


def _running_job(candidate: dict[str, Any]) -> tuple[str, str] | None:
    """The job behind a VALIDATING/BENCHMARKING candidate (e.g. after a page reload)."""
    if candidate["state"] == "VALIDATING" and (candidate.get("validation") or {}).get("job_id"):
        return candidate["validation"]["job_id"], "validate"
    if candidate["state"] == "BENCHMARKING" and candidate.get("benchmark_run_id"):
        job_id = data.benchmark(candidate["benchmark_run_id"]).get("job_id")
        return (job_id, "benchmark") if job_id else None
    return None


def _experiment_reports(candidate: dict[str, Any]) -> None:
    with st.expander("Experiment report", icon="📄"):
        st.caption(
            "The engineering write-up of this experiment: recommendation, diff, validation "
            "checks, benchmark with raw samples, provenance and reproduction commands."
        )
        blocked = None
        if candidate["state"] in {"VALIDATING", "BENCHMARKING"}:
            blocked = f"Reports are available once the candidate is no longer {candidate['state']}."
        render_export(
            ExportTarget(
                key=f"optimization_{candidate['id']}",
                path=f"/optimizations/{candidate['id']}/reports",
                empty_message="No experiment reports yet. Pick formats and press Generate reports.",
            ),
            blocked=blocked,
        )


def _candidate(candidate: dict[str, Any], detail: dict[str, Any]) -> None:
    candidate_header(candidate)
    st.write(candidate.get("summary") or "")
    for note in candidate.get("notes") or []:
        st.caption(f"• {note}")
    patch_validator_badges(candidate)
    for reason in candidate.get("rejection_reasons") or []:
        st.error(f"Rejected: {rejection_text(reason)}", icon="⛔")
    if candidate.get("error"):
        st.error(candidate["error"])
    render_diff(CANDIDATES, candidate["id"])
    if track_candidate_job(CANDIDATES, candidate["id"], _running_job(candidate)):
        return
    render_review(CANDIDATES, candidate)
    _validation(candidate)
    _benchmark_form(candidate, detail)
    _benchmark_result(candidate)
    if not isinstance(data.experiments(1), Unsupported):
        _experiment_reports(candidate)
    with st.expander("Lifecycle history"):
        st.dataframe(history_frame(candidate.get("state_history") or []), hide_index=True)


def _known_candidates(wid: str, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidates of this analysis: listed by the API, or (older APIs) the one created here."""
    rec_ids = set(analysis.get("recommendation_ids") or [])
    payload = data.workload_candidates(wid)
    if isinstance(payload, list):
        return [c for c in payload if c["recommendation_id"] in rec_ids]
    remembered = st.session_state.get(CREATED_KEY.format(wid=wid))
    if not remembered:
        return []
    try:
        candidate = data.candidate(remembered)
    except ApiError:
        return []
    return [candidate] if candidate["recommendation_id"] in rec_ids else []


def _select_candidate(wid: str, mine: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not mine:
        return None
    labels = {
        c["id"]: f"{c['id'][:8]} · {c['state'].lower()} · {fmt_dt(c['created_at'])}" for c in mine
    }
    key = CANDIDATE_KEY.format(wid=wid)
    if st.session_state.get(key) not in labels:
        st.session_state[key] = mine[0]["id"]
    chosen = st.selectbox("Candidate", list(labels), format_func=labels.__getitem__, key=key)
    return data.candidate(chosen)


def render(detail: dict[str, Any]) -> None:
    if not data.optimization_api_available():
        not_available(
            "Optimization workflow",
            "Analysis, patch candidates, diff review, approval and validation appear here once "
            "the API serves /api/analyses and /api/optimizations.",
        )
        return
    wid = detail["id"]
    st.markdown(
        "Profile → analyze → recommend → patch → **you approve** → validate → benchmark. "
        "Nothing counts as an optimization until it is validated and benchmarked."
    )
    show_action_error()
    _analysis_form(detail)
    job_key = ANALYSIS_JOB_KEY.format(wid=wid)
    if job_key in st.session_state:
        job = track_job(st.session_state[job_key], "Analyzing")
        if job is None:
            return
        st.session_state.pop(job_key)
        if not job.succeeded:
            render_job_error(job, "Analysis job")
    analysis = _select_analysis(detail)
    if analysis is None:
        st.markdown(stepper_html(lifecycle_steps(None, None)), unsafe_allow_html=True)
        empty_state("No analysis yet for this workload.", "Press Run analysis above.")
        return
    candidates = _known_candidates(wid, analysis)
    candidate = _select_candidate(wid, candidates)
    st.markdown(stepper_html(lifecycle_steps(analysis, candidate)), unsafe_allow_html=True)
    _analysis_summary(analysis)
    if analysis["state"] != "RECOMMENDED":
        return
    recs = data.analysis_recommendations(analysis["id"])
    left, right = st.columns([1, 1.25], gap="large")
    with left:
        st.markdown("##### Recommendations")
        if not recs:
            empty_state("The analysis found nothing to recommend.", "Profile a larger input size.")
        for rec in recs:
            existing = [c for c in candidates if c["recommendation_id"] == rec["id"]]
            _recommendation(rec, wid, existing)
    with right:
        if candidate is None:
            empty_state(
                "No candidate patch yet.",
                "Generate one from a recommendation; you review the diff before anything runs.",
            )
        else:
            _candidate(candidate, detail)
