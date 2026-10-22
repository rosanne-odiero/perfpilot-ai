"""Audit "Optimize" tab: patch a finding of the imported repository, review, validate with the
approved test command and benchmark with the approved workload command, all in the sandbox."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend import api_client as api
from frontend import data
from frontend.api_client import ApiError, Unsupported
from frontend.components.benchmark_charts import (
    CLASSIFICATION_COLORS,
    distribution_figure,
    paired_ratios,
    ratio_figure,
    repo_samples_as_timing,
    timing_frame,
)
from frontend.components.findings_table import measured_cards
from frontend.components.job_progress import stepper_html
from frontend.components.metric_cards import Card, render_cards
from frontend.components.notices import empty_state, not_available
from frontend.components.optimization import (
    VALIDATION_COLORS,
    benchmark_block_reason,
    checks_frame,
    history_frame,
    repo_lifecycle_steps,
)
from frontend.components.provenance import provenance_html
from frontend.formatting import fmt_dt, fmt_ms, fmt_pct, fmt_ratio, human_bytes, items_of
from frontend.theme import badge, esc
from frontend.views.audit_context import TAB_KEY, AuditContext, open_in_files
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

TAB_NAME = "Optimize"
SELECT_KEY = "repo_candidate_{audit_id}"
TEST_KINDS = ("pytest",)
WORKLOAD_KINDS = ("script", "module", "pytest_benchmark")
REPO = CandidateApi(
    base="/repo-optimizations",
    load_diff=data.repo_diff,
    clear=data.clear_repo_optimization_caches,
    validate_body={},
)


def start_repo_optimization(audit_id: str, finding_id: str) -> None:
    """Widget callback: create a candidate for ``finding_id`` and open it in the Optimize tab."""
    try:
        candidate = api.post(f"/audits/{audit_id}/optimizations", json={"finding_id": finding_id})
    except ApiError as exc:
        st.session_state[ACTION_ERROR_KEY] = exc
        st.session_state[TAB_KEY] = TAB_NAME
        return
    data.clear_repo_optimization_caches()
    st.session_state[SELECT_KEY.format(audit_id=audit_id)] = candidate["id"]
    st.session_state[TAB_KEY] = TAB_NAME
    st.toast(generation_message(candidate), icon=OUTCOME_ICONS.get(candidate.get("outcome"), "🛠️"))


OUTCOME_ICONS = {"proposed": "✅", "no_safe_patch": "⚠️", "rejected_by_validator": "⛔"}


def generation_message(candidate: dict[str, Any]) -> str:
    """What happened after Generate patch, pointing at where the result is shown."""
    short = str(candidate.get("id", ""))[:8]
    outcome = candidate.get("outcome")
    if outcome == "proposed":
        return (
            f"Patch {short} generated for {candidate.get('target_file')}. Review the diff under "
            "Candidates below, then approve or reject it."
        )
    if outcome == "no_safe_patch":
        return f"No safe patch for this finding: {candidate.get('error') or 'see the candidate'}"
    return (
        f"The proposed patch {short} was rejected by the patch validator; the reasons are shown "
        "under Candidates below."
    )


def open_candidate(audit_id: str, candidate_id: str) -> None:
    st.session_state[SELECT_KEY.format(audit_id=audit_id)] = candidate_id
    st.session_state[TAB_KEY] = TAB_NAME


def candidates_by_finding(audit_id: str) -> dict[str, list[dict[str, Any]]] | None:
    """Existing candidates per finding id, or None when this API has no repo optimization."""
    payload = data.repo_candidates(audit_id)
    if isinstance(payload, Unsupported):
        return None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in items_of(payload):
        grouped.setdefault(candidate["finding_id"], []).append(candidate)
    return grouped


def render_optimize_button(audit_id: str, finding: dict[str, Any], key: str) -> None:
    """'Optimize this finding' (or open the existing candidate) for the finding detail/runtime."""
    grouped = candidates_by_finding(audit_id)
    if grouped is None:
        return
    existing = grouped.get(finding["id"]) or []
    left, right = st.columns([1, 2])
    left.button(
        "Optimize this finding",
        key=f"optimize_{key}",
        icon="🛠️",
        help="Generate a patch for this finding; you review it before anything runs.",
        on_click=start_repo_optimization,
        args=(audit_id, finding["id"]),
    )
    if existing:
        latest = existing[-1]
        right.button(
            f"Open candidate {latest['id'][:8]} ({latest['state'].lower()})",
            key=f"open_{key}",
            type="tertiary",
            on_click=open_candidate,
            args=(audit_id, latest["id"]),
        )


def _approved_commands(context: AuditContext) -> tuple[list[str], list[str]]:
    """Commands already approved (and run) for this audit: (test commands, workload commands)."""
    overview = data.audit_optional(context.id, "runtime")
    runs = overview.get("runs") or [] if isinstance(overview, dict) else []
    tests = sorted({" ".join(r["argv"]) for r in runs if r.get("command_kind") in TEST_KINDS})
    workloads = sorted(
        {" ".join(r["argv"]) for r in runs if r.get("command_kind") in WORKLOAD_KINDS}
    )
    return tests, workloads


def go_to_runtime() -> None:
    st.session_state[TAB_KEY] = "Runtime"


def _prerequisites(context: AuditContext) -> tuple[bool, bool]:
    """Show what validation/benchmarking will reuse; return (has_tests, has_workload)."""
    tests, workloads = _approved_commands(context)
    with st.container(border=True):
        st.markdown("**Before validating and benchmarking a patch**")
        st.caption(
            "Generating and reviewing a patch needs nothing else. To *prove* it, PerfPilot reuses "
            "commands you approved for this audit in the Runtime tab (same argv, image and "
            "limits) and runs them in the Docker sandbox; nothing new is ever run."
        )
        cols = st.columns(2)
        with cols[0]:
            if tests:
                st.success("Validation (tests): " + ", ".join(f"`{t}`" for t in tests), icon="✅")
            else:
                st.warning(
                    "Step 1 — approve and run a **pytest** command in the Runtime tab "
                    "(tick *Install dependencies* if the tests need packages).",
                    icon="1️⃣",
                )
        with cols[1]:
            if workloads:
                st.success(
                    "Benchmark (workload): " + ", ".join(f"`{w}`" for w in workloads), icon="✅"
                )
            else:
                st.warning(
                    "Step 2 — approve and run a **workload** command (a script, module or "
                    "benchmark that exercises the code) in the Runtime tab.",
                    icon="2️⃣",
                )
        if not (tests and workloads):
            st.button(
                "Go to the Runtime tab",
                key=f"repo_go_runtime_{context.id}",
                icon="🧪",
                on_click=go_to_runtime,
            )
    return bool(tests), bool(workloads)


PATCH_LABELS = {"auto": "✅ automatic", "llm": "🤖 via LLM", "none": "— not available"}


def preview_frame(items: list[dict[str, Any]]) -> pd.DataFrame:
    def patch(item: dict[str, Any]) -> str:
        if item.get("automatic_patch"):
            return PATCH_LABELS["auto"]
        return PATCH_LABELS["llm"] if item.get("llm_fallback") else PATCH_LABELS["none"]

    return pd.DataFrame(
        [
            {
                "Patch": patch(item),
                "Priority": float(item.get("priority_score") or 0.0),
                "Severity": item["severity"],
                "Rule": item["rule_id"],
                "Finding": item["title"],
                "Location": f"{item['file_path']}:{item['line_start']}",
                "Measured": bool(item.get("measured")),
                "Why not automatic": item.get("reason") or "",
                "Candidates": len(item.get("candidate_ids") or []),
            }
            for item in items
        ]
    )


def _pick_finding(context: AuditContext) -> None:
    """Step 0: choose a finding (with its patch availability) and generate a candidate."""
    preview = data.repo_patch_preview(context.id)
    if isinstance(preview, Unsupported):
        st.info(
            "Open a finding (Findings tab) or a confirmed hotspot (Runtime tab) and press "
            "Optimize this finding.",
            icon="🧭",
        )
        return
    items: list[dict[str, Any]] = list(preview.get("items") or [])
    auto = int(preview.get("automatic_patches") or 0)
    st.markdown("#### Pick a finding to patch")
    st.caption(
        f"{auto} of {preview.get('previewed', len(items))} findings can be patched automatically "
        "(deterministic AST rewrites for rules "
        + ", ".join(f"`{r}`" for r in preview.get("patchable_rules") or [])
        + "). The others show why not. Nothing is created until you press Generate patch."
    )
    if not preview.get("llm_configured"):
        st.caption(
            "Findings without an automatic patch can get an LLM-proposed patch: configure "
            "`PERFPILOT_LLM_*` in `.env` and start an audit with *Use AI* and consent."
        )
    elif not preview.get("llm_consent"):
        st.caption(
            "An LLM is configured, but this audit was started without AI consent, so only "
            "automatic patches are offered. Re-run the audit with *Use AI* to enable LLM patches."
        )
    only_patchable = st.toggle(
        "Only findings that can be patched",
        value=auto > 0,
        key=f"repo_only_patchable_{context.id}",
    )
    shown = [
        i for i in items if not only_patchable or i.get("automatic_patch") or i.get("llm_fallback")
    ]
    if not shown:
        empty_state(
            "No finding in this audit can be patched automatically.",
            "Turn off the filter to see each finding's reason, or configure an LLM.",
        )
        return
    state = st.dataframe(
        preview_frame(shown),
        key=f"repo_preview_{context.id}",
        hide_index=True,
        width="stretch",
        height=min(420, 38 + 35 * len(shown)),
        on_select="rerun",
        selection_mode="single-row",
        column_config={
            "Priority": st.column_config.ProgressColumn(
                "Priority", format="%.1f", min_value=0.0, max_value=100.0
            ),
            "Finding": st.column_config.TextColumn(width="medium"),
            "Why not automatic": st.column_config.TextColumn(width="large"),
        },
    )
    rows = state.selection.rows if state is not None else []
    selected = shown[rows[0]] if rows and rows[0] < len(shown) else None
    if selected is None:
        st.caption("Select a row, then generate a patch for it.")
        return
    can_patch = bool(selected.get("automatic_patch") or selected.get("llm_fallback"))
    cols = st.columns([1, 2])
    cols[0].button(
        "Generate patch",
        key=f"repo_generate_{context.id}",
        type="primary",
        icon="🛠️",
        disabled=not can_patch,
        on_click=start_repo_optimization,
        args=(context.id, selected["finding_id"]),
    )
    if can_patch:
        how = selected.get("transform") or "LLM proposal (AI_HYPOTHESIS until validated)"
        cols[1].caption(
            f"`{selected['rule_id']}` at `{selected['file_path']}:{selected['line_start']}` · {how}"
        )
    else:
        cols[1].caption(f"Not patchable: {selected.get('reason') or 'unknown reason'}")


def _finding_block(context: AuditContext, candidate: dict[str, Any]) -> None:
    evidence = str(candidate.get("evidence") or "STATIC")
    st.markdown(
        f"{provenance_html('STATIC')} finding <code>{esc(candidate['rule_id'])}</code> "
        f"<b>{esc(candidate.get('finding_title') or '')}</b> · evidence "
        f"{provenance_html(evidence)}",
        unsafe_allow_html=True,
    )
    target = f"{candidate['target_file']}:{candidate.get('line_start')}-{candidate.get('line_end')}"
    cols = st.columns([3, 1])
    cols[0].caption(f"Target `{target}` in `{candidate.get('target_function') or 'module'}`")
    cols[1].button(
        "Open in file viewer",
        key=f"repo_open_file_{candidate['id']}",
        icon="📄",
        type="tertiary",
        on_click=open_in_files,
        args=(candidate["target_file"], candidate.get("line_start"), candidate.get("line_end")),
    )
    if candidate.get("measured_evidence"):
        render_cards(measured_cards(candidate["measured_evidence"]))


def _llm_usage(candidate: dict[str, Any]) -> None:
    usage = candidate.get("llm_usage")
    if not usage:
        return
    st.caption(
        f"LLM proposer {usage.get('model')} ({usage.get('prompt_version')}): "
        f"{usage.get('calls', 0)} call(s), {usage.get('input_tokens', 0):,} in / "
        f"{usage.get('output_tokens', 0):,} out tokens, {usage.get('redactions', 0)} redaction(s), "
        f"{usage.get('rejected_answers', 0)} rejected answer(s). The patch is an AI_HYPOTHESIS "
        "until validated and benchmarked."
    )


def _validation(candidate: dict[str, Any]) -> None:
    validation = candidate.get("validation")
    if not validation:
        return
    status = str(validation.get("status"))
    st.markdown(
        "##### Validation in the sandbox "
        + badge(status, VALIDATION_COLORS.get(status, "#64748b"))
        + " "
        + provenance_html("MEASURED")
        + f" <span class='pp-muted'><code>{esc(' '.join(validation.get('argv') or []))}</code>"
        f" · {esc(validation.get('image', ''))}</span>",
        unsafe_allow_html=True,
    )
    st.caption(validation.get("summary") or "")
    checks = validation.get("checks") or []
    if checks:
        render_checks(checks_frame(checks), checks)
    comparison = validation.get("test_comparison") or {}
    variants = validation.get("variants") or {}
    rows = []
    for name in ("original", "patched"):
        variant = variants.get(name) or {}
        counts = variant.get("counts") or {}
        rows.append(
            {
                "Variant": name,
                "Passed": counts.get("passed"),
                "Failed": counts.get("failed"),
                "Errors": counts.get("error"),
                "Skipped": counts.get("skipped"),
                "Exit (cmd)": variant.get("command_exit_code"),
                "Timed out": variant.get("timed_out"),
                "OOM": variant.get("oom_killed"),
                "Seconds": variant.get("duration_s"),
                "Failure": variant.get("failure") or "",
            }
        )
    if variants:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    regressions = comparison.get("regressions") or []
    if regressions:
        st.error(
            f"{len(regressions)} test(s) passed on the original but not on the patched copy: "
            + ", ".join(f"`{r.get('test')}` ({r.get('patched')})" for r in regressions[:10])
        )
    if validation.get("error"):
        st.error(validation["error"])


def _benchmark_form(candidate: dict[str, Any], has_workload: bool) -> None:
    cid = candidate["id"]
    reason = benchmark_block_reason(candidate)
    if reason is None and not has_workload:
        reason = "Approve a workload command (script/module/benchmark) in the Runtime tab first."
    st.markdown(
        "##### Benchmark in the sandbox " + provenance_html("MEASURED"), unsafe_allow_html=True
    )
    if reason:
        st.caption(reason)
        if candidate["state"] in {"BENCHMARKING", "COMPLETED"}:
            return
    cols = st.columns(4)
    runs = cols[0].number_input("Measured pairs", 2, 50, 10, key=f"rbr_{cid}")
    warmups = cols[1].number_input("Warm-up pairs", 0, 10, 1, key=f"rbw_{cid}")
    threshold = cols[2].number_input("Threshold (%)", 0.5, 99.0, 5.0, 0.5, key=f"rbt_{cid}")
    seed = cols[3].number_input("Seed", 0, 2**31 - 1, 0, key=f"rbs_{cid}")
    st.caption(
        "Each run is a fresh container; the order inside each pair is randomized. Only the "
        "approved command is timed (no profiler); container and interpreter start-up are excluded."
    )
    body = {
        "runs": int(runs),
        "warmups": int(warmups),
        "threshold_percent": float(threshold),
        "seed": int(seed),
    }
    st.button(
        "Benchmark candidate vs original",
        type="primary",
        icon="🏁",
        key=f"rbench_{cid}",
        disabled=reason is not None,
        on_click=post_action,
        args=(REPO, f"/repo-optimizations/{cid}/benchmark", body, cid, "benchmark"),
    )


def _benchmark_result(candidate: dict[str, Any]) -> None:
    run = candidate.get("benchmark")
    if not run or run.get("status") in {"QUEUED", "RUNNING"}:
        return
    classification = str(run.get("classification") or run.get("status"))
    low, high = (run.get("speedup_ci") or [None, None])[:2]
    summary = run.get("summary") or {}
    base = (summary.get("baseline") or {}).get("wall_ms") or {}
    cand = (summary.get("candidate") or {}).get("wall_ms") or {}
    st.markdown(
        "##### Benchmark result "
        + badge(
            classification.replace("_", " "), CLASSIFICATION_COLORS.get(classification, "#64748b")
        )
        + " "
        + provenance_html("MEASURED")
        + f" <span class='pp-muted'><code>{esc(' '.join(run.get('argv') or []))}</code> · "
        f"{summary.get('pairs', '—')} pairs</span>",
        unsafe_allow_html=True,
    )
    if run.get("classification_reason"):
        st.caption(run["classification_reason"])
    if run.get("error"):
        st.error(run["error"])
    rss_b = (summary.get("baseline") or {}).get("peak_rss_bytes") or {}
    rss_c = (summary.get("candidate") or {}).get("peak_rss_bytes") or {}
    render_cards(
        [
            Card(
                "Original median", fmt_ms(base.get("median")), f"n={base.get('n', 0)}", "MEASURED"
            ),
            Card(
                "Candidate median", fmt_ms(cand.get("median")), f"n={cand.get('n', 0)}", "MEASURED"
            ),
            Card(
                "Speedup",
                fmt_ratio(run.get("speedup")),
                f"95% CI {fmt_ratio(low)} to {fmt_ratio(high)}" if low is not None else "no CI",
                "MEASURED",
            ),
            Card(
                "Latency reduction", fmt_pct(run.get("latency_reduction_percent")), None, "MEASURED"
            ),
            Card(
                "Peak RSS (median)",
                human_bytes(rss_c.get("median")),
                f"original {human_bytes(rss_b.get('median'))} · ru_maxrss, interpreter included",
                "MEASURED",
            ),
        ]
    )
    st.caption(f"Timed: {run.get('timed', '')}")
    frame = timing_frame(repo_samples_as_timing(run.get("samples") or []))
    if not frame.empty:
        labels = {"baseline": "original", "candidate": "candidate"}
        left, right = st.columns(2)
        left.plotly_chart(distribution_figure(frame, labels=labels), theme=None)
        right.plotly_chart(
            ratio_figure(paired_ratios(frame), speedup=run.get("speedup"), ci=(low, high)),
            theme=None,
        )
    for warning in dict.fromkeys(str(w) for w in run.get("warnings") or []):
        st.warning(warning, icon="📉")
    with st.expander("Inputs and environment fingerprint"):
        st.json(
            {"inputs": run.get("inputs"), "environment": run.get("environment")}, expanded=False
        )


def _running_job(candidate: dict[str, Any]) -> tuple[str, str] | None:
    """The job behind a VALIDATING/BENCHMARKING candidate (e.g. after a page reload)."""
    if candidate["state"] == "VALIDATING" and (candidate.get("validation") or {}).get("job_id"):
        return candidate["validation"]["job_id"], "validate"
    if candidate["state"] == "BENCHMARKING" and (candidate.get("benchmark") or {}).get("job_id"):
        return candidate["benchmark"]["job_id"], "benchmark"
    return None


def _candidate(
    context: AuditContext, candidate: dict[str, Any], has_tests: bool, has_workload: bool
) -> None:
    st.markdown(stepper_html(repo_lifecycle_steps(candidate)), unsafe_allow_html=True)
    candidate_header(candidate, " " + provenance_html(str(candidate.get("provenance") or "STATIC")))
    _finding_block(context, candidate)
    if candidate.get("outcome") == "no_safe_patch":
        reason = candidate.get("error") or "no deterministic transformation applies"
        st.warning(
            f"No safe patch: {reason}. The finding stays a recommendation; nothing was changed.",
            icon="🚫",
        )
    if candidate.get("summary"):
        st.write(candidate["summary"])
    for note in candidate.get("notes") or []:
        st.caption(f"• {note}")
    _llm_usage(candidate)
    patch_validator_badges(candidate)
    for reason in candidate.get("rejection_reasons") or []:
        st.error(f"Rejected: {rejection_text(reason)}", icon="⛔")
    if candidate.get("error") and candidate.get("outcome") != "no_safe_patch":
        st.error(candidate["error"])
    if candidate.get("patched_file_sha256"):
        render_diff(REPO, candidate["id"])
    if track_candidate_job(REPO, candidate["id"], _running_job(candidate)):
        return
    note = "" if has_tests else "Approve a pytest command in the Runtime tab before validating."
    render_review(REPO, candidate, validate_note=note)
    _validation(candidate)
    if candidate["state"] not in {"FAILED", "REJECTED"} or candidate.get("benchmark"):
        _benchmark_form(candidate, has_workload)
    _benchmark_result(candidate)
    with st.expander("Lifecycle history"):
        st.dataframe(history_frame(candidate.get("state_history") or []), hide_index=True)


def render(context: AuditContext) -> None:
    show_action_error()
    payload = data.repo_candidates(context.id)
    if isinstance(payload, Unsupported):
        not_available(
            "Repository optimization",
            "Patching, validating and benchmarking findings of this repository appear here once "
            "the API serves /api/audits/{id}/optimizations.",
        )
        return
    candidates = list(reversed(items_of(payload)))
    with st.container(border=True):
        _pick_finding(context)
    has_tests, has_workload = _prerequisites(context)
    if not candidates:
        return
    st.markdown("#### Candidates")
    labels = {
        c["id"]: f"{c['id'][:8]} · {c['rule_id']} · {c['target_file']} · {c['state'].lower()} · "
        f"{fmt_dt(c['created_at'])}"
        for c in candidates
    }
    key = SELECT_KEY.format(audit_id=context.id)
    if st.session_state.get(key) not in labels:
        st.session_state[key] = candidates[0]["id"]
    chosen = st.selectbox("Candidate", list(labels), format_func=labels.__getitem__, key=key)
    _candidate(context, data.repo_candidate(chosen), has_tests, has_workload)
