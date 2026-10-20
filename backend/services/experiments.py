"""Experiment history: recent optimization candidates and reference benchmarks, newest first."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.orm.audit import ReportRecord
from backend.models.orm.optimization import OptimizationCandidate, OptimizationRecommendation
from backend.models.orm.workloads import BenchmarkRun, Workload
from backend.models.workloads import CandidateKind

ExperimentKindName = Literal["optimization", "reference_benchmark"]


class ExperimentOut(BaseModel):
    kind: ExperimentKindName
    id: str
    subject_type: Literal["optimization", "benchmark"]
    workload_id: str
    workload_slug: str | None
    workload_name: str | None
    title: str
    state: str
    validation_status: str | None
    benchmark_id: str | None
    benchmark_status: str | None
    size: str | None
    classification: str | None
    speedup: float | None
    speedup_ci_low: float | None
    speedup_ci_high: float | None
    latency_reduction_percent: float | None
    median_baseline_ms: float | None
    median_candidate_ms: float | None
    provenance: str | None
    generated_by: str | None
    report_count: int
    created_at: datetime
    updated_at: datetime


def _median(run: BenchmarkRun | None, variant: str) -> float | None:
    if run is None:
        return None
    summary: dict[str, Any] = run.summary or {}
    value = ((summary.get(variant) or {}).get("wall_ms") or {}).get("median")
    return float(value) if value is not None else None


def _report_counts(session: Session, subject_type: str, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    rows = session.execute(
        select(ReportRecord.subject_id, func.count())
        .where(ReportRecord.subject_type == subject_type, ReportRecord.subject_id.in_(ids))
        .group_by(ReportRecord.subject_id)
    ).all()
    return {str(subject_id): int(count) for subject_id, count in rows}


def _benchmark_fields(run: BenchmarkRun | None) -> dict[str, Any]:
    return {
        "benchmark_id": run.id if run else None,
        "benchmark_status": run.status if run else None,
        "size": run.size if run else None,
        "classification": run.classification if run else None,
        "speedup": run.speedup if run else None,
        "speedup_ci_low": run.speedup_ci_low if run else None,
        "speedup_ci_high": run.speedup_ci_high if run else None,
        "latency_reduction_percent": run.latency_reduction_percent if run else None,
        "median_baseline_ms": _median(run, "baseline"),
        "median_candidate_ms": _median(run, "candidate"),
        "provenance": run.provenance if run else None,
    }


def list_experiments(
    session: Session,
    *,
    limit: int = 50,
    workload_id: str | None = None,
    kind: ExperimentKindName | None = None,
) -> list[ExperimentOut]:
    workloads = {w.id: w for w in session.scalars(select(Workload))}
    items: list[ExperimentOut] = []
    if kind in (None, "optimization"):
        stmt = select(OptimizationCandidate).order_by(OptimizationCandidate.created_at.desc())
        if workload_id:
            stmt = stmt.where(OptimizationCandidate.workload_id == workload_id)
        candidates = list(session.scalars(stmt.limit(limit)))
        counts = _report_counts(session, "optimization", [c.id for c in candidates])
        for candidate in candidates:
            rec = session.get(OptimizationRecommendation, candidate.recommendation_id)
            run = (
                session.get(BenchmarkRun, candidate.benchmark_run_id)
                if candidate.benchmark_run_id
                else None
            )
            workload = workloads.get(candidate.workload_id)
            items.append(
                ExperimentOut(
                    kind="optimization",
                    id=candidate.id,
                    subject_type="optimization",
                    workload_id=candidate.workload_id,
                    workload_slug=workload.slug if workload else None,
                    workload_name=workload.name if workload else None,
                    title=rec.title if rec else candidate.summary,
                    state=candidate.state,
                    validation_status=candidate.validation_status,
                    generated_by=candidate.generated_by,
                    report_count=counts.get(candidate.id, 0),
                    created_at=candidate.created_at,
                    updated_at=candidate.updated_at,
                    **_benchmark_fields(run),
                )
            )
    if kind in (None, "reference_benchmark"):
        bench_stmt = (
            select(BenchmarkRun)
            .where(BenchmarkRun.candidate_kind == CandidateKind.REFERENCE)
            .order_by(BenchmarkRun.created_at.desc())
        )
        if workload_id:
            bench_stmt = bench_stmt.where(BenchmarkRun.workload_id == workload_id)
        runs = list(session.scalars(bench_stmt.limit(limit)))
        counts = _report_counts(session, "benchmark", [r.id for r in runs])
        for run in runs:
            workload = workloads.get(run.workload_id)
            items.append(
                ExperimentOut(
                    kind="reference_benchmark",
                    id=run.id,
                    subject_type="benchmark",
                    workload_id=run.workload_id,
                    workload_slug=workload.slug if workload else None,
                    workload_name=workload.name if workload else None,
                    title=f"original vs {run.candidate_label}",
                    state=run.status,
                    validation_status=None,
                    generated_by=None,
                    report_count=counts.get(run.id, 0),
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    **_benchmark_fields(run),
                )
            )
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items[:limit]
