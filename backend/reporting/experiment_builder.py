"""Build an ``ExperimentReport`` from the database (one candidate, or one reference benchmark).

Only stored rows are read; nothing is re-run. Measured numbers are copied from the profile,
validation and benchmark rows; hypotheses keep their own labels. Sections that never ran stay
``None`` with the reason, so a failed or rejected attempt is reported as such.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import __version__
from backend.core.exceptions import NotFoundError
from backend.models.enums import Provenance
from backend.models.orm.optimization import (
    AnalysisRun,
    OptimizationCandidate,
    OptimizationRecommendation,
    ValidationRun,
)
from backend.models.orm.workloads import (
    BenchmarkRun,
    BenchmarkSample,
    ProfileRun,
    Project,
    Workload,
)
from backend.models.workloads import HotspotSort, WorkloadSpec
from backend.reporting.experiment_format import (
    fmt_bytes,
    fmt_ci,
    fmt_ms,
    fmt_percent,
    fmt_ratio,
)
from backend.reporting.experiment_model import (
    CUMULATIVE_DEFINITION,
    EXCLUSIVE_DEFINITION,
    PROFILER_OVERHEAD_NOTE,
    ApprovalSection,
    BenchmarkSection,
    BenchmarkSettings,
    EnvironmentSection,
    ExperimentKind,
    ExperimentMetadata,
    ExperimentReport,
    HotspotRow,
    LabeledText,
    MeasuredVsHypothesis,
    MemoryComparison,
    MemoryComparisonRow,
    MemoryMetric,
    Outcome,
    PatchSection,
    ProfilingSection,
    RecommendationSection,
    Reproducibility,
    SampleRow,
    StateEvent,
    SummaryItem,
    ValidationCheckRow,
    ValidationSection,
    VariantStats,
    WorkloadSection,
)
from backend.services.profiling import list_hotspots
from backend.services.workloads import build_spec

GENERATOR = f"PerfPilot AI {__version__}"
TOP_HOTSPOTS = 10
MAX_FAILURES_PER_CHECK = 10
DETERMINISTIC = "deterministic"
CHECK_STATUSES = {"PASSED", "FAILED", "INCONCLUSIVE", "NOT_RUN"}
DEFAULT_ORDER = "random"
DEFAULT_RESAMPLES = 10_000

_ENV_FIELDS = (
    ("captured_at", "Captured at"),
    ("perfpilot_version", "PerfPilot version"),
    ("python_implementation", "Python implementation"),
    ("python_version", "Python version"),
    ("platform", "Platform"),
    ("os", "OS"),
    ("os_release", "OS release"),
    ("architecture", "Architecture"),
    ("cpu_model", "CPU model"),
    ("logical_cpus", "Logical CPUs"),
    ("physical_cpus", "Physical CPUs"),
    ("cpu_frequency", "CPU frequency"),
    ("cpu_affinity", "CPU affinity"),
    ("total_memory_bytes", "Total memory (bytes)"),
    ("load_average_1_5_15", "Load average (1/5/15 min)"),
    ("uv_lock_sha256", "uv.lock SHA-256"),
)
_MEMORY_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("tracemalloc", "tracemalloc_peak_bytes", "tracemalloc peak (Python allocations, call only)"),
    ("rss", "rss_peak_bytes", "RSS peak (sampled, whole child process)"),
    ("ru_maxrss", "ru_maxrss_bytes", "ru_maxrss (kernel max RSS, whole child process)"),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        value = [round(v, 2) if isinstance(v, float) else v for v in value]
    return json.dumps(value, default=str)


# ------------------------------------------------------------------------------------------
# Workload, environment, profile
# ------------------------------------------------------------------------------------------


def _workload(session: Session, workload_id: str) -> tuple[Workload, WorkloadSpec, str]:
    workload = session.get(Workload, workload_id)
    if workload is None:
        raise NotFoundError(f"workload {workload_id} not found")
    project = session.get(Project, workload.project_id)
    return workload, build_spec(workload), project.name if project else ""


def _workload_section(
    workload: Workload,
    spec: WorkloadSpec,
    project: str,
    size: str | None,
    input_sha256: str | None,
    input_source: str | None,
) -> WorkloadSection:
    return WorkloadSection(
        workload_id=workload.id,
        slug=spec.slug,
        name=spec.name,
        description=spec.description,
        project=project,
        entrypoint=spec.entrypoint,
        invocation_mode=spec.invocation_mode.value,
        fixture=spec.fixture_id,
        fixture_description=spec.fixture_description,
        size=size,
        size_parameters=spec.sizes.get(size, {}) if size else {},
        input_sha256=input_sha256,
        input_sha256_source=input_source if input_sha256 else None,
        expected_output=spec.expected_output.model_dump(mode="json"),
        timeout_seconds=spec.timeout_seconds,
        memory_limit_mb=spec.memory_limit_mb,
        tree_sha256=workload.tree_sha256,
    )


def _environment(raw: dict[str, Any], source: str) -> EnvironmentSection | None:
    if not raw:
        return None
    fields = {
        label: _text(raw[key]) for key, label in _ENV_FIELDS if raw.get(key) not in (None, "")
    }
    packages = {str(k): str(v) for k, v in (raw.get("packages") or {}).items()}
    notes = [str(n) for n in raw.get("notes") or []]
    return EnvironmentSection(source=source, fields=fields, packages=packages, notes=notes)


def _profile_section(session: Session, profile: ProfileRun, relation: str) -> ProfilingSection:
    hotspots = list_hotspots(session, profile.id, sort=HotspotSort.EXCLUSIVE, limit=TOP_HOTSPOTS)
    memory = profile.memory or {}
    definitions = memory.get("definitions") or {}
    metrics = profile.metrics or {}
    return ProfilingSection(
        profile_id=profile.id,
        relation=relation,
        status=profile.status,
        size=profile.size,
        input_sha256=profile.input_sha256,
        total_profiled_time_ms=profile.total_profiled_time_ms,
        wall_time_under_profiler_ms=metrics.get("wall_time_under_profiler_ms"),
        hotspots=[
            HotspotRow(
                rank=i,
                function_name=h.function_name,
                file_path=h.file_path,
                line_number=h.line_number,
                location_kind=h.location_kind,
                is_user_code=h.is_user_code,
                total_calls=h.total_calls,
                primitive_calls=h.primitive_calls,
                exclusive_ms=h.internal_time_ms,
                cumulative_ms=h.cumulative_time_ms,
                exclusive_percent=h.percentage_of_profiled_time,
                cumulative_percent=h.cumulative_percentage,
            )
            for i, h in enumerate(hotspots, start=1)
        ],
        definitions={
            "exclusive": EXCLUSIVE_DEFINITION,
            "cumulative": CUMULATIVE_DEFINITION,
            "overhead": PROFILER_OVERHEAD_NOTE,
        },
        memory=[
            MemoryMetric(
                name=label,
                tool=tool,
                value_bytes=memory.get(key),
                definition=str(definitions.get(key, "")),
            )
            for tool, key, label in _MEMORY_TOOLS
        ],
        memory_unavailable=[str(u) for u in memory.get("unavailable") or []],
        warnings=[str(w) for w in profile.warnings or []],
    )


# ------------------------------------------------------------------------------------------
# Recommendation, patch, approval, validation
# ------------------------------------------------------------------------------------------


def _hypothesis_label(rec: OptimizationRecommendation, part: str) -> Provenance:
    """Deterministic rule templates are STATIC; anything a model wrote is AI_HYPOTHESIS."""
    if rec.generated_by == DETERMINISTIC:
        return Provenance.STATIC
    try:
        recorded = Provenance(str((rec.provenance or {}).get(part)))
    except ValueError:
        return Provenance.AI_HYPOTHESIS
    # A hypothesis can never be presented as a measurement.
    return Provenance.AI_HYPOTHESIS if recorded == Provenance.MEASURED else recorded


def _recommendation_section(rec: OptimizationRecommendation) -> RecommendationSection:
    impact = rec.estimated_impact or {}
    qualitative = "qualitative expectation, not a measurement"
    impact_text = str(impact.get("level", "unknown"))
    if impact.get("rationale"):
        impact_text += f": {impact['rationale']}"
    return RecommendationSection(
        recommendation_id=rec.id,
        analysis_id=rec.analysis_run_id,
        rank=rec.rank,
        rule_id=rec.rule_id,
        title=rec.title,
        target_file=rec.target_file,
        target_function=rec.target_function,
        source_lines=rec.source_lines or {},
        observed_bottleneck=LabeledText(
            text=rec.observed_bottleneck,
            provenance=Provenance.MEASURED,
            note="measured share of profiled time plus the static rule that flags the code",
        ),
        profiler_evidence=rec.profiler_evidence or {},
        suspected_root_cause=LabeledText(
            text=rec.suspected_root_cause, provenance=_hypothesis_label(rec, "suspected_root_cause")
        ),
        proposed_optimization=LabeledText(
            text=rec.proposed_optimization, provenance=_hypothesis_label(rec, "recommendation")
        ),
        expected_effect=LabeledText(
            text=rec.expected_effect,
            provenance=_hypothesis_label(rec, "expected_effect"),
            note=qualitative,
        ),
        estimated_impact=LabeledText(
            text=impact_text,
            provenance=_hypothesis_label(rec, "estimated_impact"),
            note=qualitative,
        ),
        correctness_risks=[str(r) for r in rec.correctness_risks or []],
        memory_tradeoffs=LabeledText(
            text=rec.memory_tradeoffs or "none stated",
            provenance=_hypothesis_label(rec, "memory_tradeoffs"),
        ),
        confidence=rec.confidence,
        validation_plan=[str(p) for p in rec.validation_plan or []],
        generated_by=rec.generated_by,
    )


def _patch_section(candidate: OptimizationCandidate) -> PatchSection:
    report = candidate.patch_validation or {}
    violations = [
        f"{v.get('code', 'violation')}: {v.get('message', '')}"
        + (f" ({v['path']})" if v.get("path") else "")
        for v in report.get("violations") or []
    ]
    return PatchSection(
        generated_by=candidate.generated_by,
        transform=candidate.transform,
        target_file=candidate.target_file,
        summary=candidate.summary,
        notes=[str(n) for n in candidate.notes or []],
        unified_diff=candidate.proposed_diff,
        diff_withheld_reason=None
        if candidate.proposed_diff
        else (candidate.error or "no diff was produced"),
        changed_lines=report.get("changed_lines"),
        original_file_sha256=candidate.original_file_sha256,
        patched_file_sha256=candidate.patched_file_sha256,
        original_tree_sha256=candidate.original_tree_sha256,
        patched_tree_sha256=candidate.patched_tree_sha256,
        patch_validator_valid=report.get("valid") if report else None,
        patch_validator_checks={str(k): str(v) for k, v in (report.get("checks") or {}).items()},
        patch_validator_violations=violations,
    )


def _approval_section(candidate: OptimizationCandidate) -> ApprovalSection:
    reasons = []
    for item in candidate.rejection_reasons or []:
        if isinstance(item, dict):
            code = f" [{item['code']}]" if item.get("code") else ""
            reasons.append(f"{item.get('by', 'unknown')}: {item.get('reason', '')}{code}")
        else:
            reasons.append(str(item))
    return ApprovalSection(
        status=candidate.approval_status,
        approved_by=candidate.approved_by,
        approved_at=candidate.approved_at,
        rejected_by=candidate.rejected_by,
        rejected_at=candidate.rejected_at,
        rejection_reasons=reasons,
        state_history=[
            StateEvent(
                from_state=str(h.get("from_state", "")),
                to_state=str(h.get("to_state", "")),
                at=str(h.get("at", "")),
                reason=str(h.get("reason", "")),
            )
            for h in candidate.state_history or []
        ],
    )


def _check_failures(check: dict[str, Any]) -> list[str]:
    data = check.get("data") or {}
    failures: list[str] = []
    for variant in ("baseline", "candidate"):
        side = data.get(variant)
        if isinstance(side, dict):
            failures += [f"{variant}: {f}" for f in side.get("failures") or []]
    cases = data.get("cases")
    if isinstance(cases, dict):
        for name, case in cases.items():
            if isinstance(case, dict) and case.get("status") not in (None, "PASSED"):
                failures.append(f"{name}: {case.get('status')} - {case.get('detail', '')}")
    return [f[:400] for f in failures[:MAX_FAILURES_PER_CHECK]]


def _validation_section(run: ValidationRun) -> ValidationSection:
    checks = [
        ValidationCheckRow(
            name=str(c.get("name", "")),
            required=bool(c.get("required", False)),
            status=c.get("status") if c.get("status") in CHECK_STATUSES else "NOT_RUN",
            detail=str(c.get("detail", "")),
            duration_seconds=c.get("duration_seconds"),
            failures=_check_failures(c),
        )
        for c in run.checks or []
    ]
    status = run.status if run.status in CHECK_STATUSES else "NOT_RUN"
    return ValidationSection(
        validation_id=run.id,
        status=status,
        summary=run.summary,
        required_checks=[c.name for c in checks if c.required],
        checks=checks,
        error=run.error,
        duration_seconds=run.duration_seconds,
        baseline_input_hashes={
            str(k): str(v) for k, v in (run.baseline_input_hashes or {}).items()
        },
        finished_at=run.finished_at,
    )


# ------------------------------------------------------------------------------------------
# Benchmark
# ------------------------------------------------------------------------------------------


def _variant_stats(summary: dict[str, Any]) -> list[VariantStats]:
    stats: list[VariantStats] = []
    for variant in ("baseline", "candidate"):
        block = summary.get(variant) or {}
        for metric in ("wall_ms", "cpu_ms"):
            values = block.get(metric)
            if not values:
                continue
            stats.append(
                VariantStats(
                    variant=variant,
                    metric=metric,
                    n=values["n"],
                    median=values["median"],
                    mean=values["mean"],
                    min=values["min"],
                    max=values["max"],
                    stdev=values.get("stdev"),
                    p95=values["p95"],
                    cv_percent=values.get("cv_percent"),
                    small_n_warning=values.get("p95_warning"),
                )
            )
    return stats


def _memory_comparison(memory: dict[str, Any]) -> MemoryComparison | None:
    base, cand = memory.get("baseline"), memory.get("candidate")
    if not isinstance(base, dict) or not isinstance(cand, dict):
        return None
    definitions = memory.get("definitions") or {}
    rows = []
    for tool, key, label in _MEMORY_TOOLS:
        b, c = base.get(f"{key}_median"), cand.get(f"{key}_median")
        rows.append(
            MemoryComparisonRow(
                tool=tool,
                label=label,
                baseline_bytes_median=b,
                candidate_bytes_median=c,
                ratio_candidate_to_baseline=c / b if b and c is not None else None,
                definition=str(definitions.get(key, "")),
            )
        )
    return MemoryComparison(
        runs_per_variant={
            "baseline": int(base.get("runs", 0)),
            "candidate": int(cand.get("runs", 0)),
        },
        rows=rows,
        unavailable=[str(u) for u in memory.get("unavailable") or []],
    )


def _samples(session: Session, run_id: str) -> list[SampleRow]:
    rows = session.scalars(
        select(BenchmarkSample)
        .where(BenchmarkSample.benchmark_run_id == run_id)
        .order_by(
            BenchmarkSample.kind, BenchmarkSample.pair_index, BenchmarkSample.position_in_pair
        )
    )
    return [
        SampleRow(
            variant=s.variant,
            kind=s.kind,
            pair_index=s.pair_index,
            position_in_pair=s.position_in_pair,
            wall_ms=s.wall_ns / 1e6 if s.wall_ns is not None else None,
            cpu_ms=s.cpu_ns / 1e6 if s.cpu_ns is not None else None,
            tracemalloc_peak_bytes=s.tracemalloc_peak_bytes,
            rss_peak_bytes=s.rss_peak_bytes,
            ru_maxrss_bytes=s.ru_maxrss_bytes,
            ok=s.ok,
            error=s.error,
        )
        for s in rows
    ]


def _cpu_unavailable_reason(run: BenchmarkRun, cpu_speedup: float | None) -> str | None:
    if cpu_speedup is not None:
        return None
    for warning in run.warnings or []:
        if "CPU-time speedup unavailable" in str(warning):
            return str(warning)
    if run.status != "SUCCEEDED":
        return f"the benchmark did not succeed (status {run.status})"
    return "CPU time was not recorded for this run"


def _benchmark_section(session: Session, run: BenchmarkRun) -> BenchmarkSection:
    summary = run.summary or {}
    ci = summary.get("ci") or {}
    thresholds = summary.get("thresholds") or {}
    env_settings = (run.environment or {}).get("settings") or {}
    stats = _variant_stats(summary)
    cpu_speedup = summary.get("cpu_speedup")
    return BenchmarkSection(
        benchmark_id=run.id,
        status=run.status,
        candidate_kind=run.candidate_kind,
        candidate_label=run.candidate_label,
        settings=BenchmarkSettings(
            size=run.size,
            warmups=run.warmups,
            runs=run.runs,
            memory_runs=run.memory_runs,
            seed=run.seed,
            order=run.order,
            threshold_percent=run.threshold_percent,
            bootstrap_resamples=run.bootstrap_resamples,
            timed_iterations_per_process=int(env_settings.get("timed_iterations_per_process", 1)),
        ),
        input_sha256=run.input_sha256,
        baseline_tree_sha256=(run.environment or {}).get("baseline_tree_sha256"),
        candidate_tree_sha256=(run.environment or {}).get("candidate_tree_sha256"),
        stats=stats,
        throughput_calls_per_s={
            v: (summary.get(v) or {}).get("throughput_calls_per_s")
            for v in ("baseline", "candidate")
            if summary.get(v)
        },
        speedup=run.speedup,
        ci_low=run.speedup_ci_low,
        ci_high=run.speedup_ci_high,
        ci_confidence=ci.get("confidence"),
        ci_method=ci.get("method"),
        latency_reduction_percent=run.latency_reduction_percent,
        throughput_speedup=summary.get("throughput_speedup"),
        cpu_speedup=cpu_speedup,
        cpu_speedup_unavailable_reason=_cpu_unavailable_reason(run, cpu_speedup),
        memory=_memory_comparison(run.memory or {}),
        classification=run.classification,
        classification_reason=run.classification_reason,
        improvement_speedup_threshold=thresholds.get("improvement_speedup"),
        regression_speedup_threshold=thresholds.get("regression_speedup"),
        correctness=run.correctness or {},
        formulas={str(k): str(v) for k, v in (summary.get("formulas") or {}).items()},
        warnings=[str(w) for w in run.warnings or []],
        error=run.error,
        samples=_samples(session, run.id),
    )


def _ci(bench: BenchmarkSection) -> str:
    return fmt_ci(bench.ci_low, bench.ci_high, bench.ci_confidence)


def _wall(bench: BenchmarkSection, variant: str) -> VariantStats | None:
    return next((s for s in bench.stats if s.variant == variant and s.metric == "wall_ms"), None)


def _measured_benchmark_items(bench: BenchmarkSection) -> list[SummaryItem]:
    m = Provenance.MEASURED
    base, cand = _wall(bench, "baseline"), _wall(bench, "candidate")
    items: list[SummaryItem] = []
    if base and cand:
        items.append(
            SummaryItem(
                label="Median wall time",
                value=f"baseline {fmt_ms(base.median)} -> candidate {fmt_ms(cand.median)} "
                f"(n={base.n} / {cand.n} timed runs)",
                provenance=m,
            )
        )
    if bench.speedup is not None:
        items.append(
            SummaryItem(
                label="Speedup (median wall-time ratio)",
                value=f"{fmt_ratio(bench.speedup)} ({_ci(bench)})",
                provenance=m,
            )
        )
        items.append(
            SummaryItem(
                label="Latency reduction",
                value=fmt_percent(bench.latency_reduction_percent),
                provenance=m,
            )
        )
        items.append(
            SummaryItem(
                label="CPU-time speedup",
                value=fmt_ratio(bench.cpu_speedup)
                if bench.cpu_speedup is not None
                else f"unavailable: {bench.cpu_speedup_unavailable_reason}",
                provenance=m,
            )
        )
    if bench.classification:
        items.append(
            SummaryItem(
                label="Classification",
                value=f"{bench.classification} at the {bench.settings.threshold_percent:g}% "
                f"practical threshold",
                provenance=m,
            )
        )
    if bench.memory:
        for row in bench.memory.rows:
            if row.tool == "ru_maxrss":
                continue
            items.append(
                SummaryItem(
                    label=row.label,
                    value=f"baseline {fmt_bytes(row.baseline_bytes_median)} -> candidate "
                    f"{fmt_bytes(row.candidate_bytes_median)} (median)",
                    provenance=m,
                )
            )
    if bench.status != "SUCCEEDED":
        items.append(
            SummaryItem(
                label="Benchmark status",
                value=f"{bench.status}: {bench.error or 'no result'}",
                provenance=m,
            )
        )
    return items


def _verdict_for_benchmark(bench: BenchmarkSection, subject: str) -> str:
    ci = _ci(bench)
    threshold = f"{bench.settings.threshold_percent:g}%"
    match bench.classification:
        case "IMPROVED":
            return (
                f"Confirmed by measurement: {subject} is IMPROVED at the {threshold} practical "
                f"threshold (speedup {fmt_ratio(bench.speedup)}, {ci})."
            )
        case "REGRESSED":
            return (
                f"Contradicted by measurement: {subject} REGRESSED (speedup "
                f"{fmt_ratio(bench.speedup)}, {ci})."
            )
        case "NO_CLEAR_DIFFERENCE" | "INCONCLUSIVE":
            return (
                f"Not confirmed: the benchmark of {subject} is {bench.classification} "
                f"({bench.classification_reason or ci}). No performance claim is made."
            )
        case "INVALID":
            return (
                f"No performance claim: the benchmark of {subject} is INVALID "
                f"({bench.classification_reason or bench.error or 'see warnings'})."
            )
    return (
        f"No performance claim: the benchmark of {subject} did not produce a result "
        f"(status {bench.status}: {bench.error or 'unknown error'})."
    )


# ------------------------------------------------------------------------------------------
# Reproducibility
# ------------------------------------------------------------------------------------------


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _json(body: dict[str, Any]) -> str:
    return json.dumps(body, separators=(", ", ": "))


def _benchmark_body(spec_id: str, run: BenchmarkRun, candidate: Any) -> dict[str, Any]:
    return {
        "workload_id": spec_id,
        "candidate": candidate,
        "size": run.size,
        "warmups": run.warmups,
        "runs": run.runs,
        "memory_runs": run.memory_runs,
        "threshold_percent": run.threshold_percent,
        "seed": run.seed,
        "order": run.order,
        "bootstrap_resamples": run.bootstrap_resamples,
    }


def _bench_cli_args(run: BenchmarkRun) -> str:
    return (
        f"--size {_q(run.size)} --warmups {run.warmups} --runs {run.runs} "
        f"--memory-runs {run.memory_runs} --threshold {run.threshold_percent:g} --seed {run.seed}"
    )


def _cli_notes(run: BenchmarkRun | None) -> list[str]:
    if run is None:
        return []
    if run.order != DEFAULT_ORDER or run.bootstrap_resamples != DEFAULT_RESAMPLES:
        return [
            f"order={run.order} and bootstrap_resamples={run.bootstrap_resamples} are not CLI "
            "options; use the API call to reproduce them exactly."
        ]
    return []


def _optimization_repro(
    spec: WorkloadSpec,
    rec: OptimizationRecommendation,
    profile: ProfileRun | None,
    candidate: OptimizationCandidate,
    run: BenchmarkRun | None,
) -> Reproducibility:
    cli = f".venv/bin/python scripts/optimize_workload.py {_q(spec.slug)} --approve"
    if profile is not None:
        cli += f" --profile-size {_q(profile.size)}"
    cli += f" --recommendation {rec.rank}"
    if run is not None:
        cli += " " + _bench_cli_args(run)
    approver = candidate.approved_by or "local-user"
    profile_body = {"workload_id": spec.id, "size": profile.size if profile else spec.default_size}
    analysis_body = {"workload_id": spec.id, "profile_id": profile.id if profile else None}
    calls = [
        f"POST /api/profiles {_json(profile_body)}",
        f"POST /api/analyses {_json(analysis_body)}",
        f"GET /api/analyses/{rec.analysis_run_id}/recommendations",
        f"POST /api/optimizations {_json({'recommendation_id': rec.id})}",
        f"GET /api/optimizations/{candidate.id}/diff",
        f"POST /api/optimizations/{candidate.id}/approve {_json({'approved_by': approver})}",
        f"POST /api/optimizations/{candidate.id}/validate",
        f"GET /api/optimizations/{candidate.id}/validation",
    ]
    if run is not None:
        body = _benchmark_body(spec.id, run, {"candidate_id": candidate.id})
        calls += [
            f"POST /api/benchmarks {_json(body)}",
            f"GET /api/benchmarks/{run.id}/results",
        ]
    calls.append(
        f"POST /api/optimizations/{candidate.id}/reports "
        f"{_json({'formats': ['md', 'json', 'html', 'csv', 'pdf']})}"
    )
    return Reproducibility(
        cli=[cli],
        api_calls=calls,
        notes=[
            "The IDs are those of this experiment; re-running the steps creates new records "
            "(the bundled workload source is unchanged, see the tree hashes).",
            "Every POST that starts work returns 202 and a job id; poll GET /api/jobs/{job_id}.",
            *_cli_notes(run),
        ],
    )


def _reference_repro(spec: WorkloadSpec, run: BenchmarkRun) -> Reproducibility:
    return Reproducibility(
        cli=[
            f".venv/bin/python scripts/run_workload.py benchmark {_q(spec.slug)} "
            + _bench_cli_args(run)
        ],
        api_calls=[
            f"POST /api/benchmarks {_json(_benchmark_body(spec.id, run, 'reference'))}",
            f"GET /api/benchmarks/{run.id}/results",
            f"POST /api/benchmarks/{run.id}/reports "
            f"{_json({'formats': ['md', 'json', 'html', 'csv', 'pdf']})}",
        ],
        notes=[
            "Every POST that starts work returns 202 and a job id; poll GET /api/jobs/{job_id}.",
            *_cli_notes(run),
        ],
    )


def _limitations(bench: BenchmarkSection | None) -> list[str]:
    items = [
        "Bundled workloads run on the host in constrained child processes (fresh interpreter, "
        "scrubbed environment, rlimits, timeouts), not on dedicated benchmark hardware; read the "
        "warnings and the environment fingerprint before trusting small differences.",
        "Correctness validation covers only the tests and inputs it exercised. It is evidence of "
        "equivalence, not a proof.",
        "tracemalloc (Python-traced allocations during the call) and RSS (sampled process "
        "memory) measure different things and are never compared with each other.",
        "cProfile times are inflated by instrumentation: the profile shows where time goes, the "
        "benchmark shows how fast the code runs.",
    ]
    if bench is not None and bench.settings.runs < 20:
        items.append(
            f"Only {bench.settings.runs} timed runs per variant: p95 and the bootstrap CI are "
            "unstable at this sample size; re-run with more runs on a quiet machine to confirm."
        )
    return items


# ------------------------------------------------------------------------------------------
# Public builders
# ------------------------------------------------------------------------------------------


def _latest_candidate_benchmark(
    session: Session, candidate: OptimizationCandidate
) -> BenchmarkRun | None:
    if candidate.benchmark_run_id:
        return session.get(BenchmarkRun, candidate.benchmark_run_id)
    return session.scalar(
        select(BenchmarkRun)
        .where(BenchmarkRun.candidate_id == candidate.id)
        .order_by(BenchmarkRun.created_at.desc())
        .limit(1)
    )


def _failed_checks(validation: ValidationSection) -> list[str]:
    return [
        f"{c.name} {c.status}: {c.detail}"
        for c in validation.checks
        if c.required and c.status != "PASSED"
    ]


def _no_benchmark_reason(
    candidate: OptimizationCandidate, validation: ValidationSection | None
) -> str:
    state = candidate.state
    if state == "REJECTED":
        by = candidate.rejected_by or "a reviewer"
        return (
            f"No benchmark ran: the candidate was REJECTED by {by} before validation, so no "
            "performance numbers exist for it."
        )
    if validation is not None and validation.status == "FAILED":
        return (
            "No benchmark ran: correctness validation FAILED, and a candidate is benchmarked "
            "only after every required check PASSED. This report therefore contains no "
            "performance numbers for the patch."
        )
    if validation is not None and validation.status == "INCONCLUSIVE":
        return (
            "No benchmark ran: correctness validation was INCONCLUSIVE; the candidate must be "
            "re-validated (and PASS) before it can be benchmarked."
        )
    if state == "FAILED":
        return (
            f"No benchmark ran: the candidate FAILED before benchmarking "
            f"({candidate.error or 'see the state history'})."
        )
    if state in ("VALIDATED",):
        return "Not benchmarked yet: the candidate is VALIDATED and can now be benchmarked."
    return (
        f"No benchmark ran: the candidate is {state}; it is benchmarked only after explicit "
        "approval and a PASSED validation."
    )


def _no_validation_reason(candidate: OptimizationCandidate) -> str:
    if candidate.state == "REJECTED":
        return (
            f"Validation did not run: the candidate was REJECTED by "
            f"{candidate.rejected_by or 'a reviewer'}."
        )
    if candidate.state == "FAILED":
        return f"Validation did not run: {candidate.error or 'the candidate failed earlier'}."
    return (
        f"Validation has not run: the candidate is {candidate.state} (validation needs an "
        "explicit approval first)."
    )


def _optimization_outcome(
    candidate: OptimizationCandidate,
    validation: ValidationSection | None,
    bench: BenchmarkSection | None,
) -> Outcome:
    state = candidate.state
    if state == "COMPLETED" and bench is not None:
        headline = (
            f"Validated and benchmarked: {bench.classification}, median speedup "
            f"{fmt_ratio(bench.speedup)} ({_ci(bench)})."
        )
        return Outcome(
            state=state,
            headline=headline,
            succeeded=bench.classification == "IMPROVED",
            reasons=[bench.classification_reason] if bench.classification_reason else [],
        )
    if state == "REJECTED":
        reasons = [
            str(r.get("reason", r)) if isinstance(r, dict) else str(r)
            for r in candidate.rejection_reasons or []
        ]
        return Outcome(
            state=state,
            headline=f"Rejected by {candidate.rejected_by or 'a reviewer'}; never validated or "
            "benchmarked.",
            succeeded=False,
            reasons=reasons,
        )
    if validation is not None and validation.status == "FAILED":
        return Outcome(
            state=state,
            headline="Correctness validation FAILED; no benchmark ran.",
            succeeded=False,
            reasons=_failed_checks(validation) or [validation.error or validation.summary],
        )
    if state == "FAILED":
        return Outcome(
            state=state,
            headline="The candidate FAILED.",
            succeeded=False,
            reasons=[candidate.error or "see the state history"],
        )
    if bench is not None:
        return Outcome(
            state=state,
            headline=f"Benchmark {bench.status}: "
            f"{bench.classification or bench.error or 'no result'}",
            succeeded=False,
            reasons=[bench.error] if bench.error else [],
        )
    return Outcome(
        state=state,
        headline=f"In progress: the candidate is {state}.",
        succeeded=False,
        reasons=[],
    )


def build_optimization_report(session: Session, candidate_id: str) -> ExperimentReport:
    candidate = session.get(OptimizationCandidate, candidate_id)
    if candidate is None:
        raise NotFoundError(f"optimization candidate {candidate_id} not found")
    rec = session.get(OptimizationRecommendation, candidate.recommendation_id)
    if rec is None:
        raise NotFoundError(f"recommendation {candidate.recommendation_id} not found")
    workload, spec, project = _workload(session, candidate.workload_id)
    analysis = session.get(AnalysisRun, rec.analysis_run_id)
    profile = (
        session.get(ProfileRun, analysis.profile_run_id)
        if analysis is not None and analysis.profile_run_id
        else None
    )
    run = _latest_candidate_benchmark(session, candidate)
    bench = _benchmark_section(session, run) if run is not None else None
    validation_row = (
        session.get(ValidationRun, candidate.validation_run_id)
        if candidate.validation_run_id
        else None
    )
    validation = _validation_section(validation_row) if validation_row else None
    recommendation = _recommendation_section(rec)

    size: str | None
    input_sha: str | None
    if run is not None:
        environment = _environment(run.environment or {}, f"benchmark run {run.id}")
        size, input_sha = run.size, run.input_sha256
        input_source = "benchmark preflight (identical for both variants)"
    else:
        environment = (
            _environment(profile.environment or {}, f"profile run {profile.id}")
            if profile
            else None
        )
        size = profile.size if profile else None
        input_sha = profile.input_sha256 if profile else None
        input_source = "profile run"

    measured: list[SummaryItem] = []
    target = (rec.profiler_evidence or {}).get("target_function")
    if isinstance(target, dict) and target:
        measured.append(
            SummaryItem(
                label="Profiled bottleneck",
                value=f"{target.get('function_name')} ({target.get('file_path')}:"
                f"{target.get('line_number')}): {fmt_percent(target.get('cumulative_percentage'))} "
                f"cumulative / {fmt_percent(target.get('percentage_of_profiled_time'))} exclusive "
                f"of profiled time, {target.get('total_calls')} call(s) "
                f"(cProfile, size {(rec.profiler_evidence or {}).get('size')})",
                provenance=Provenance.MEASURED,
            )
        )
    if validation is not None:
        passed = sum(1 for c in validation.checks if c.required and c.status == "PASSED")
        required = len(validation.required_checks)
        value = f"{validation.status}: {passed}/{required} required checks passed"
        failed = _failed_checks(validation)
        if failed:
            value += "; " + "; ".join(failed)
        measured.append(
            SummaryItem(label="Correctness validation", value=value, provenance=Provenance.MEASURED)
        )
    if bench is not None:
        measured += _measured_benchmark_items(bench)

    hypotheses = [
        SummaryItem(
            label="Suspected root cause",
            value=recommendation.suspected_root_cause.text,
            provenance=recommendation.suspected_root_cause.provenance,
        ),
        SummaryItem(
            label="Expected effect (qualitative)",
            value=recommendation.expected_effect.text,
            provenance=recommendation.expected_effect.provenance,
        ),
        SummaryItem(
            label="Estimated impact (qualitative)",
            value=recommendation.estimated_impact.text,
            provenance=recommendation.estimated_impact.provenance,
        ),
        SummaryItem(
            label="Memory trade-off",
            value=recommendation.memory_tradeoffs.text,
            provenance=recommendation.memory_tradeoffs.provenance,
        ),
    ]
    benchmark_absent = None if bench is not None else _no_benchmark_reason(candidate, validation)
    if bench is not None:
        verdict = _verdict_for_benchmark(bench, "the validated patch")
    elif validation is not None and validation.status == "FAILED":
        verdict = (
            "Not confirmed: the candidate FAILED correctness validation and was never "
            "benchmarked, so no performance measurement of the patch exists."
        )
    else:
        verdict = f"Untested: {benchmark_absent}"

    return ExperimentReport(
        kind=ExperimentKind.OPTIMIZATION,
        metadata=ExperimentMetadata(
            title=f"Optimization experiment: {spec.name} - {rec.title}",
            subject_type="optimization",
            subject_id=candidate.id,
            generated_at=_now(),
            generator=GENERATOR,
        ),
        outcome=_optimization_outcome(candidate, validation, bench),
        summary_box=MeasuredVsHypothesis(measured=measured, hypotheses=hypotheses, verdict=verdict),
        workload=_workload_section(workload, spec, project, size, input_sha, input_source),
        environment=environment,
        profiling=_profile_section(
            session, profile, "profile used by the analysis that produced this recommendation"
        )
        if profile is not None
        else None,
        profiling_absent_reason=None
        if profile is not None
        else "the analysis did not record a profile run",
        recommendation=recommendation,
        patch=_patch_section(candidate),
        approval=_approval_section(candidate),
        validation=validation,
        validation_absent_reason=None if validation else _no_validation_reason(candidate),
        benchmark=bench,
        benchmark_absent_reason=benchmark_absent,
        reproducibility=_optimization_repro(spec, rec, profile, candidate, run),
        limitations=_limitations(bench),
    )


def build_benchmark_report(session: Session, benchmark_id: str) -> ExperimentReport:
    """Report for a benchmark of the workload's bundled reference implementation."""
    run = session.get(BenchmarkRun, benchmark_id)
    if run is None:
        raise NotFoundError(f"benchmark {benchmark_id} not found")
    workload, spec, project = _workload(session, run.workload_id)
    bench = _benchmark_section(session, run)
    profile = session.scalar(
        select(ProfileRun)
        .where(ProfileRun.workload_id == run.workload_id, ProfileRun.status == "SUCCEEDED")
        .order_by(ProfileRun.created_at.desc())
        .limit(1)
    )
    subject = "the bundled reference implementation"
    succeeded = run.status == "SUCCEEDED" and run.classification == "IMPROVED"
    return ExperimentReport(
        kind=ExperimentKind.REFERENCE_BENCHMARK,
        metadata=ExperimentMetadata(
            title=f"Reference benchmark: {spec.name} - original vs {run.candidate_label}",
            subject_type="benchmark",
            subject_id=run.id,
            generated_at=_now(),
            generator=GENERATOR,
        ),
        outcome=Outcome(
            state=run.status,
            headline=(
                f"Reference benchmark {run.status}: {run.classification or 'no classification'}"
                + (f", median speedup {fmt_ratio(run.speedup)}" if run.speedup is not None else "")
                + "."
            ),
            succeeded=succeeded,
            reasons=[r for r in (run.classification_reason, run.error) if r],
        ),
        summary_box=MeasuredVsHypothesis(
            measured=_measured_benchmark_items(bench),
            hypotheses=[],
            verdict=_verdict_for_benchmark(bench, subject)
            + " The reference is hand-written for tests (not platform output); no "
            "recommendation, patch or approval is involved.",
        ),
        workload=_workload_section(
            workload,
            spec,
            project,
            run.size,
            run.input_sha256,
            "benchmark preflight (identical for both variants)",
        ),
        environment=_environment(run.environment or {}, f"benchmark run {run.id}"),
        profiling=_profile_section(
            session,
            profile,
            "most recent successful profile of this workload's original code; it is not part "
            "of the benchmark run",
        )
        if profile is not None
        else None,
        profiling_absent_reason=None
        if profile is not None
        else "no successful profile of this workload is stored",
        validation_absent_reason=(
            "Reference implementations are hand-written for tests and are not validated by the "
            "optimization workflow; the benchmark preflight still compares their outputs "
            "(see correctness)."
        ),
        benchmark=bench,
        reproducibility=_reference_repro(spec, run),
        limitations=_limitations(bench),
    )
