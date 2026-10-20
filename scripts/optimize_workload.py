"""Run the full deep-dive for a bundled workload and print JSON.

profile -> analysis (static rules + measured hotspots) -> recommendation -> patch candidate ->
explicit approval -> correctness validation -> A/B benchmark of the validated patch.

Uses the same services as the API, so every run is persisted in the same database::

    .venv/bin/python scripts/optimize_workload.py duplicate_counter            # stops at approval
    .venv/bin/python scripts/optimize_workload.py duplicate_counter --approve  # full deep dive
    .venv/bin/python scripts/optimize_workload.py nested_loop --approve --runs 20 --warmups 5

Without ``--approve`` the candidate is left in ``AWAITING_APPROVAL``: nothing is applied,
validated, or benchmarked without an explicit approval.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _progress(stage: str, fraction: float, message: str | None = None) -> None:
    print(f"[{fraction:5.1%}] {stage}: {message or ''}", file=sys.stderr)


def _step(name: str) -> None:
    print(f"==> {name}", file=sys.stderr)


def _benchmark_summary(view: dict[str, Any]) -> dict[str, Any]:
    summary = view["summary"] or {}
    return {
        "benchmark_id": view["id"],
        "status": view["status"],
        "candidate": view["candidate"]["label"],
        "size": view["size"],
        "settings": view["settings"],
        "median_baseline_ms": ((summary.get("baseline") or {}).get("wall_ms") or {}).get("median"),
        "median_candidate_ms": ((summary.get("candidate") or {}).get("wall_ms") or {}).get(
            "median"
        ),
        "speedup": view["speedup"],
        "ci95": [view["speedup_ci_low"], view["speedup_ci_high"]],
        "latency_reduction_percent": view["latency_reduction_percent"],
        "classification": view["classification"],
        "reason": view["classification_reason"],
        "memory": {k: view["memory"].get(k) for k in ("baseline", "candidate")},
        "warnings": view["warnings"],
        "error": view["error"],
        "provenance": view["provenance"],
    }


def _workload(slug: str) -> tuple[str, Path]:
    from sqlalchemy import select

    from backend.models.database import session_scope
    from backend.models.orm.workloads import Project, Workload
    from backend.services.workloads import BUNDLED_PROJECT_NAME

    with session_scope() as session:
        workload = session.scalar(
            select(Workload)
            .join(Project, Project.id == Workload.project_id)
            .where(Project.name == BUNDLED_PROJECT_NAME, Workload.slug == slug)
        )
        if workload is None:
            raise SystemExit(f"unknown bundled workload {slug!r}; try run_workload.py list")
        return workload.id, Path(workload.workspace_path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    from backend.core.config import get_settings
    from backend.models.database import session_scope
    from backend.models.optimization_api import AnalysisRequest, OptimizationRequest
    from backend.models.workloads import BenchmarkRequest, CandidateRef, ProfileRequest
    from backend.optimization import candidate_resolver
    from backend.optimization.state_machine import OptimizationState
    from backend.services import analyses, benchmarks, optimizations, profiling
    from backend.services.source_snapshots import hash_tree

    settings = get_settings()
    candidate_resolver.install()
    workload_id, workspace = _workload(args.workload)
    output: dict[str, Any] = {"workload": args.workload}

    _step("profile")
    with session_scope() as session:
        profile_id = profiling.create_profile_run(
            session, ProfileRequest(workload_id=workload_id, size=args.profile_size)
        ).id
    profiling.execute_profile_run(profile_id, settings, _progress)
    with session_scope() as session:
        profile = profiling.profile_view(session, profile_id)
        output["profile"] = {
            "id": profile.id,
            "size": profile.size,
            "total_profiled_time_ms": profile.total_profiled_time_ms,
            "top_user_hotspots": [h.model_dump() for h in profile.top_user_hotspots[:3]],
            "provenance": profile.provenance,
        }

    _step("analysis")
    with session_scope() as session:
        analysis_id = analyses.create_analysis(
            session, AnalysisRequest(workload_id=workload_id, profile_id=profile_id)
        ).id
    analyses.execute_analysis(analysis_id, settings, _progress)
    with session_scope() as session:
        analysis = analyses.analysis_view(session, analysis_id)
        recommendations = analyses.list_recommendations(session, analysis_id)
    output["analysis"] = {
        "id": analysis.id,
        "state": analysis.state,
        "summary": analysis.summary,
        "recommendations": [
            {"id": r.id, "rank": r.rank, "title": r.title, "rule_id": r.rule_id}
            for r in recommendations
        ],
    }
    if not recommendations:
        output["result"] = "no recommendations: nothing to optimize"
        return output
    chosen = next((r for r in recommendations if r.rank == args.recommendation), None)
    if chosen is None:
        raise SystemExit(f"no recommendation with rank {args.recommendation}")
    output["recommendation"] = chosen.model_dump(mode="json")

    _step("patch candidate")
    with session_scope() as session:
        candidate_id = optimizations.create_candidate(
            session, settings, OptimizationRequest(recommendation_id=chosen.id)
        ).id
    with session_scope() as session:
        candidate = optimizations.candidate_view(session, candidate_id)
        output["candidate"] = {
            "id": candidate.id,
            "state": candidate.state,
            "generated_by": candidate.generated_by,
            "transform": candidate.transform,
            "summary": candidate.summary,
            "notes": candidate.notes,
            "patch_validation": candidate.patch_validation,
            "error": candidate.error,
            "diff": optimizations.candidate_diff(session, candidate_id).unified_diff
            if candidate.state == OptimizationState.AWAITING_APPROVAL
            else None,
        }
    if candidate.state != OptimizationState.AWAITING_APPROVAL:
        output["result"] = f"candidate is {candidate.state}: {candidate.error}"
        return output
    if not args.approve:
        output["result"] = (
            "approval required: the patch was not applied, validated or benchmarked "
            "(re-run with --approve to continue)"
        )
        return output

    _step("approve + validate")
    with session_scope() as session:
        optimizations.approve_candidate(session, candidate_id, args.approved_by)
    with session_scope() as session:
        validation_id = optimizations.create_validation(session, candidate_id).id
    optimizations.execute_validation(validation_id, settings, _progress)
    with session_scope() as session:
        validation = optimizations.validation_view(session, validation_id)
        state = optimizations.get_candidate(session, candidate_id).state
    output["validation"] = {
        "id": validation.id,
        "status": validation.status,
        "summary": validation.summary,
        "checks": [
            {
                "name": c["name"],
                "required": c["required"],
                "status": c["status"],
                "detail": c["detail"],
            }
            for c in validation.checks
        ],
    }
    if state == OptimizationState.VALIDATED:
        _step("benchmark")
        request = BenchmarkRequest(
            workload_id=workload_id,
            candidate=CandidateRef(candidate_id=candidate_id),
            size=args.size,
            warmups=args.warmups,
            runs=args.runs,
            memory_runs=args.memory_runs,
            threshold_percent=args.threshold,
            seed=args.seed,
        )
        with session_scope() as session:
            benchmark_id = benchmarks.create_benchmark_run(session, request).id
        benchmarks.execute_benchmark_run(benchmark_id, settings, _progress)
        with session_scope() as session:
            output["benchmark"] = _benchmark_summary(
                benchmarks.benchmark_view(session, benchmark_id).model_dump(mode="json")
            )
    else:
        output["benchmark"] = None
    with session_scope() as session:
        final = optimizations.candidate_view(session, candidate_id)
    output["final_state"] = final.state
    output["state_history"] = [
        f"{h['from_state']} -> {h['to_state']}: {h['reason']}" for h in final.state_history
    ]
    output["original_tree_unchanged"] = hash_tree(workspace)[0] == final.original_tree_sha256
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optimize_workload", description=__doc__.splitlines()[0])
    parser.add_argument("workload", help="bundled workload slug (see run_workload.py list)")
    parser.add_argument(
        "--approve", action="store_true", help="explicitly approve the proposed patch"
    )
    parser.add_argument("--approved-by", default="cli-user")
    parser.add_argument("--profile-size", default=None, help="size used for profiling")
    parser.add_argument("--recommendation", type=int, default=1, help="rank to implement")
    parser.add_argument("--size", default=None, help="benchmark size (default: workload default)")
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--memory-runs", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=5.0, help="practical threshold, %%")
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args(argv)

    from backend.core.config import get_settings
    from backend.core.logging import configure_logging
    from backend.models.database import init_engine, session_scope
    from backend.services.workloads import seed_bundled_examples

    configure_logging("WARNING")
    init_engine(get_settings())
    with session_scope() as session:
        seed_bundled_examples(session)
    print(json.dumps(run(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
