"""GitHub-flavored Markdown renderer for the experiment report (paste into a PR or issue).

Stored text is escaped with the audit renderer's helpers: inline syntax is neutralized, code and
diffs go in fenced blocks whose fence is longer than any backtick run inside them.
"""

from __future__ import annotations

from backend.reporting.experiment_format import (
    fmt_bytes,
    fmt_ci,
    fmt_ms,
    fmt_percent,
    fmt_ratio,
)
from backend.reporting.experiment_model import (
    EXPERIMENT_SECTION_TITLES,
    BenchmarkSection,
    ExperimentReport,
    LabeledText,
)
from backend.reporting.model import PROVENANCE_LEGEND
from backend.reporting.renderers.common import format_datetime, humanize
from backend.reporting.renderers.markdown import code_span, fenced, md, prov, table

MAX_SAMPLE_ROWS = 200


def _heading(n: int) -> str:
    return f"## {n}. {EXPERIMENT_SECTION_TITLES[n]}"


def _absent(title: str, text: str) -> list[str]:
    return [f"> **{md(title)}**", ">", f"> {md(text)}"]


def _labeled(label: str, item: LabeledText) -> list[str]:
    note = f" _({md(item.note)})_" if item.note else ""
    return [f"- **{label}** {prov(item.provenance)}: {md(item.text)}{note}"]


def _summary(r: ExperimentReport) -> list[str]:
    box = r.summary_box
    out = [
        _heading(1),
        "",
        f"**{md(r.outcome.headline)}** (state `{md(r.outcome.state)}`)",
        "",
    ]
    out += [f"- {md(reason)}" for reason in r.outcome.reasons]
    if r.outcome.reasons:
        out.append("")
    out += ["### Measured vs hypothesis", ""]
    if box.measured:
        out += table(
            ["Measured", "Value", "Provenance"],
            ([md(i.label), md(i.value), prov(i.provenance)] for i in box.measured),
        )
    else:
        out.append("_Nothing was measured for this experiment._")
    out.append("")
    if box.hypotheses:
        out += table(
            ["Hypothesis (not measured)", "Statement", "Provenance"],
            ([md(i.label), md(i.value), prov(i.provenance)] for i in box.hypotheses),
        )
        out.append("")
    out += [f"> **Verdict:** {md(box.verdict)}", ""]
    return out


def _workload(r: ExperimentReport) -> list[str]:
    w = r.workload
    params = ", ".join(f"{k}={v}" for k, v in w.size_parameters.items())
    rows = [
        ["Workload", f"{md(w.name)} ({code_span(w.slug)}), project {md(w.project)}"],
        ["Description", md(w.description)],
        ["Entrypoint", f"{code_span(w.entrypoint)} ({md(w.invocation_mode)})"],
        ["Fixture", f"{code_span(w.fixture)} {md(w.fixture_description)}"],
        ["Input size", f"{md(w.size or 'n/a')} {code_span(params) if params else ''}"],
        [
            "Input SHA-256",
            f"{code_span(w.input_sha256 or 'not recorded')} {md(w.input_sha256_source or '')}",
        ],
        [
            "Expected output",
            md("; ".join(f"{k}: {v}" for k, v in w.expected_output.items())),
        ],
        ["Limits", f"timeout {w.timeout_seconds} s, memory {w.memory_limit_mb} MB"],
        ["Registered tree SHA-256", code_span(w.tree_sha256 or "unknown")],
    ]
    return [_heading(2), "", *table(["Field", "Value"], rows), ""]


def _environment(r: ExperimentReport) -> list[str]:
    out = [_heading(3), ""]
    e = r.environment
    if e is None:
        return [*out, *_absent("No environment fingerprint", "No run recorded one."), ""]
    out += [f"{prov(e.provenance)} Captured by {md(e.source)}.", ""]
    rows = [[md(k), code_span(v)] for k, v in e.fields.items()]
    if e.packages:
        rows.append(["Packages", code_span(", ".join(f"{k}=={v}" for k, v in e.packages.items()))])
    out += [*table(["Field", "Value"], rows), ""]
    out += [f"- ⚠️ {md(n)}" for n in e.notes]
    return [*out, ""]


def _profiling(r: ExperimentReport) -> list[str]:
    out = [_heading(4), ""]
    p = r.profiling
    if p is None:
        reason = r.profiling_absent_reason or "No profile is linked to this experiment."
        return [*out, *_absent("No profile", reason), ""]
    out += [
        f"{prov(p.provenance)} Profile {code_span(p.profile_id)} ({md(p.status)}, size "
        f"{md(p.size)}): {md(p.relation)}. Total profiled time {fmt_ms(p.total_profiled_time_ms)}, "
        f"wall time under the profiler {fmt_ms(p.wall_time_under_profiler_ms)}, input SHA-256 "
        f"{code_span(p.input_sha256 or 'n/a')}.",
        "",
        *table(
            ["#", "Function", "Location", "Calls", "Exclusive", "Excl. %", "Cumulative", "Cum. %"],
            (
                [
                    str(h.rank),
                    code_span(h.function_name),
                    code_span(f"{h.file_path}:{h.line_number}"),
                    str(h.total_calls),
                    fmt_ms(h.exclusive_ms),
                    fmt_percent(h.exclusive_percent),
                    fmt_ms(h.cumulative_ms),
                    fmt_percent(h.cumulative_percent),
                ]
                for h in p.hotspots
            ),
        ),
        "",
        *[f"- {md(v)}" for v in p.definitions.values()],
        "",
        "Memory during the profile (three different quantities):",
        "",
        *table(
            ["Metric", "Value", "Definition"],
            ([md(m.name), fmt_bytes(m.value_bytes), md(m.definition)] for m in p.memory),
        ),
        "",
    ]
    out += [f"- ⚠️ unavailable: {md(u)}" for u in p.memory_unavailable]
    out += [f"- ⚠️ {md(w)}" for w in p.warnings]
    return [*out, ""]


def _recommendation(r: ExperimentReport) -> list[str]:
    out = [_heading(5), ""]
    rec = r.recommendation
    if rec is None:
        return [
            *out,
            "_Not applicable: this experiment benchmarks the bundled reference implementation._",
            "",
        ]
    lines = rec.source_lines
    location = rec.target_file + (f":{lines['start']}" if lines.get("start") else "")
    out += [
        f"**{md(rec.title)}**: rule {code_span(rec.rule_id or 'n/a')}, rank {rec.rank}, "
        f"confidence {md(rec.confidence)}, generated by {md(rec.generated_by)}, target "
        f"{code_span(location)}"
        + (f" {code_span(rec.target_function)}" if rec.target_function else ""),
        "",
        *_labeled("Observed bottleneck", rec.observed_bottleneck),
    ]
    target = rec.profiler_evidence.get("target_function")
    if isinstance(target, dict):
        out.append(
            f"- **Profiler evidence** {prov(rec.profiler_evidence_provenance)}: profile "
            f"{code_span(rec.profiler_evidence.get('profile_id', 'n/a'))}: "
            f"{code_span(target.get('function_name', ''))} exclusive "
            f"{fmt_ms(target.get('internal_time_ms'))} "
            f"({fmt_percent(target.get('percentage_of_profiled_time'))}), cumulative "
            f"{fmt_ms(target.get('cumulative_time_ms'))} "
            f"({fmt_percent(target.get('cumulative_percentage'))}), "
            f"{target.get('total_calls')} call(s)"
        )
    out += _labeled("Suspected root cause", rec.suspected_root_cause)
    out += _labeled("Proposed optimization", rec.proposed_optimization)
    out += _labeled("Expected effect", rec.expected_effect)
    out += _labeled("Estimated impact", rec.estimated_impact)
    out += _labeled("Memory trade-offs", rec.memory_tradeoffs)
    out.append("- **Correctness risks:**")
    out += [f"  - {md(risk)}" for risk in rec.correctness_risks] or ["  - none stated"]
    out.append("- **Validation plan:**")
    out += [f"  {i}. {md(step)}" for i, step in enumerate(rec.validation_plan, start=1)]
    return [*out, ""]


def _patch(r: ExperimentReport) -> list[str]:
    out = [_heading(6), ""]
    d = r.patch
    if d is None:
        return [*out, "_Not applicable: the reference implementation is a bundled file._", ""]
    validator = (
        "not run"
        if d.patch_validator_valid is None
        else ("PASSED" if d.patch_validator_valid else "FAILED")
    )
    rows = [
        [
            "Generated by",
            md(d.generated_by) + (f", transform {code_span(d.transform)}" if d.transform else ""),
        ],
        ["Target file", code_span(d.target_file)],
        ["Changed lines", str(d.changed_lines) if d.changed_lines is not None else "n/a"],
        ["Original file SHA-256", code_span(d.original_file_sha256)],
        ["Patched file SHA-256", code_span(d.patched_file_sha256 or "n/a")],
        ["Original tree SHA-256", code_span(d.original_tree_sha256)],
        ["Patched tree SHA-256", code_span(d.patched_tree_sha256 or "not built (never validated)")],
        [
            "Patch validator",
            f"**{validator}** "
            + md(", ".join(f"{k}: {v}" for k, v in d.patch_validator_checks.items())),
        ],
    ]
    out += [*table(["Field", "Value"], rows), ""]
    out += [f"- ❌ {md(v)}" for v in d.patch_validator_violations]
    if d.summary:
        out += ["", md(d.summary)]
    out += [f"- {md(n)}" for n in d.notes]
    out.append("")
    if d.unified_diff:
        out += [*fenced(d.unified_diff.rstrip("\n"), "diff"), ""]
    else:
        out += [*_absent("No diff", d.diff_withheld_reason or "No diff was produced."), ""]
    return out


def _approval(r: ExperimentReport) -> list[str]:
    out = [_heading(7), ""]
    a = r.approval
    if a is None:
        return [*out, "_Not applicable: no patch approval is involved._", ""]
    out += [
        f"- Approval: **{md(a.status)}**",
        f"- Approved by: {md(a.approved_by or 'n/a')} at {format_datetime(a.approved_at)}",
    ]
    if a.rejected_by:
        out.append(f"- Rejected by: {md(a.rejected_by)} at {format_datetime(a.rejected_at)}")
    out += [f"- ❌ Rejection: {md(reason)}" for reason in a.rejection_reasons]
    out += [
        "",
        *table(
            ["From", "To", "At", "Reason"],
            (
                [md(h.from_state), md(h.to_state), code_span(h.at), md(h.reason)]
                for h in a.state_history
            ),
        ),
        "",
    ]
    return out


def _validation(r: ExperimentReport) -> list[str]:
    out = [_heading(8), ""]
    v = r.validation
    if v is None:
        reason = r.validation_absent_reason or "No validation run is linked."
        return [*out, *_absent("Validation did not run", reason), ""]
    out += [
        f"{prov(v.provenance)} **{v.status}**: {md(v.summary)}",
        "",
        f"Required checks: {', '.join(code_span(c) for c in v.required_checks) or 'none'}",
        "",
    ]
    if v.error:
        out += [f"⚠️ {md(v.error)}", ""]
    rows = []
    for c in v.checks:
        detail = md(c.detail)
        if c.failures:
            detail += "<br>" + "<br>".join(code_span(f) for f in c.failures)
        rows.append([code_span(c.name), "yes" if c.required else "no", f"**{c.status}**", detail])
    out += [*table(["Check", "Required", "Status", "Detail"], rows), ""]
    return out


def _benchmark_stats(b: BenchmarkSection) -> list[str]:
    s = b.settings
    out = [
        f"Settings: size {md(s.size)}, {s.warmups} warmup(s), {s.runs} timed runs per variant "
        f"({s.timed_iterations_per_process} call per fresh process), {s.memory_runs} memory runs, "
        f"order {md(s.order)}, seed {s.seed}, {s.bootstrap_resamples} bootstrap resamples, "
        f"practical threshold {s.threshold_percent:g}%.",
        "",
        f"Input SHA-256 {code_span(b.input_sha256 or 'n/a')}; baseline tree "
        f"{code_span(b.baseline_tree_sha256 or 'n/a')}; candidate tree "
        f"{code_span(b.candidate_tree_sha256 or 'n/a')}.",
        "",
    ]
    if not b.stats:
        return out
    out += table(
        ["Metric", "Value"],
        [
            ["Median speedup", f"**{fmt_ratio(b.speedup)}**"],
            ["Bootstrap CI", fmt_ci(b.ci_low, b.ci_high, b.ci_confidence)],
            ["Latency reduction", fmt_percent(b.latency_reduction_percent)],
            ["Throughput speedup", fmt_ratio(b.throughput_speedup)],
            [
                "CPU-time speedup",
                fmt_ratio(b.cpu_speedup)
                if b.cpu_speedup is not None
                else f"unavailable: {md(b.cpu_speedup_unavailable_reason or '')}",
            ],
            ["Classification", f"**{md(b.classification or 'n/a')}**"],
            ["Classification rule", md(b.classification_reason or "n/a")],
            [
                "Thresholds",
                f"IMPROVED needs ≥ {fmt_ratio(b.improvement_speedup_threshold)} and a CI "
                f"excluding 1; REGRESSED ≤ {fmt_ratio(b.regression_speedup_threshold)}",
            ],
            ["CI method", md(b.ci_method or "n/a")],
        ],
    )
    out += [
        "",
        "Per-variant statistics:",
        "",
        *table(
            ["Variant", "Metric", "n", "Median", "Mean", "Min", "Max", "Stdev", "p95", "CV"],
            (
                [
                    st.variant,
                    "wall" if st.metric == "wall_ms" else "CPU",
                    str(st.n),
                    fmt_ms(st.median),
                    fmt_ms(st.mean),
                    fmt_ms(st.min),
                    fmt_ms(st.max),
                    fmt_ms(st.stdev),
                    fmt_ms(st.p95),
                    fmt_percent(st.cv_percent),
                ]
                for st in b.stats
            ),
        ),
        "",
    ]
    warnings = list(dict.fromkeys(st.small_n_warning for st in b.stats if st.small_n_warning))
    out += [f"- ⚠️ Small sample: {md(w)}" for w in warnings]
    return [*out, ""] if warnings else out


def _benchmark(r: ExperimentReport) -> list[str]:
    out = [_heading(9), ""]
    b = r.benchmark
    if b is None:
        reason = r.benchmark_absent_reason or "No benchmark is linked to this experiment."
        return [*out, *_absent("No benchmark ran", reason), ""]
    out += [
        f"{prov(b.provenance)} Benchmark {code_span(b.benchmark_id)}: **{md(b.status)}**, "
        f"baseline (original) vs {md(b.candidate_label)}",
        "",
    ]
    if b.error:
        out += [f"⚠️ {md(b.error)}", ""]
    out += _benchmark_stats(b)
    out += ["### Memory comparison", ""]
    if b.memory is None:
        out += ["_No memory runs were recorded._", ""]
    else:
        runs = b.memory.runs_per_variant
        out += [
            f"{runs.get('baseline', 0)} baseline / {runs.get('candidate', 0)} candidate memory "
            "run(s), separate from the timed runs. tracemalloc and RSS are different quantities "
            "and are not compared with each other.",
            "",
            *table(
                ["Metric", "Baseline (median)", "Candidate (median)", "Ratio", "Definition"],
                (
                    [
                        md(m.label),
                        fmt_bytes(m.baseline_bytes_median),
                        fmt_bytes(m.candidate_bytes_median),
                        fmt_ratio(m.ratio_candidate_to_baseline),
                        md(m.definition),
                    ]
                    for m in b.memory.rows
                ),
            ),
            "",
        ]
        out += [f"- ⚠️ unavailable: {md(u)}" for u in b.memory.unavailable]
    if b.correctness:
        details = ", ".join(f"{k}={v}" for k, v in b.correctness.items())
        out += [f"Output check during the benchmark: {md(details)}", ""]
    out += ["### Warnings and noise", ""]
    out += [f"- ⚠️ {md(w)}" for w in b.warnings] or ["_No warnings._"]
    timing = [s for s in b.samples if s.kind == "timing"]
    out += [
        "",
        "<details><summary>Raw timing samples "
        f"({len(timing)} timed of {len(b.samples)} rows; all rows in the CSV export)</summary>",
        "",
        *table(
            ["Pair", "Variant", "Position", "Wall", "CPU", "OK"],
            (
                [
                    str(s.pair_index),
                    s.variant,
                    str(s.position_in_pair),
                    fmt_ms(s.wall_ms),
                    fmt_ms(s.cpu_ms),
                    "yes" if s.ok else "no",
                ]
                for s in timing[:MAX_SAMPLE_ROWS]
            ),
        ),
        "",
        "</details>",
        "",
    ]
    return out


def render_experiment_markdown(report: ExperimentReport) -> str:
    r = report
    out = [
        f"# {md(r.metadata.title)}",
        "",
        f"{md(humanize(r.kind.value))} · {md(r.metadata.subject_type)} "
        f"{code_span(r.metadata.subject_id)} · workload {code_span(r.workload.slug)} · "
        f"generated {format_datetime(r.metadata.generated_at)} by {md(r.metadata.generator)}",
        "",
        "Provenance labels: "
        + "; ".join(f"{prov(e.label)} {md(e.meaning)}" for e in PROVENANCE_LEGEND),
        "",
    ]
    out += _summary(r)
    out += _workload(r)
    out += _environment(r)
    out += _profiling(r)
    out += _recommendation(r)
    out += _patch(r)
    out += _approval(r)
    out += _validation(r)
    out += _benchmark(r)
    out += [_heading(10), "", "Command line:", ""]
    out += [*fenced("\n".join(r.reproducibility.cli), "sh"), "", "HTTP API:", ""]
    out += [*fenced("\n".join(r.reproducibility.api_calls), "http"), ""]
    out += [f"- {md(n)}" for n in r.reproducibility.notes]
    out += ["", _heading(11), ""]
    out += [f"- {md(item)}" for item in r.limitations]
    out += [
        "",
        "---",
        f"_Generated by {md(r.metadata.generator)} · experiment schema {r.schema_version} · "
        f"{r.metadata.redactions_applied} secret-like values redacted._",
        "",
    ]
    return "\n".join(out)
