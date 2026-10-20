"""Profile or benchmark a bundled workload from the command line and print JSON.

Uses exactly the same services as the API (``create_*_run`` -> ``execute_*_run`` -> ``*_view``),
so the printed JSON has the same shape and the runs are persisted in the same database::

    .venv/bin/python scripts/run_workload.py list
    .venv/bin/python scripts/run_workload.py profile duplicate_counter --size medium
    .venv/bin/python scripts/run_workload.py benchmark string_builder --runs 20 --warmups 5
    .venv/bin/python scripts/run_workload.py benchmark-all --summary
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


def _workload_id(slug: str) -> str:
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
            raise SystemExit(f"unknown bundled workload {slug!r}; try the 'list' command")
        return workload.id


def _list(_: argparse.Namespace) -> Any:
    from backend.models.database import session_scope
    from backend.services import workload_views

    with session_scope() as session:
        return [
            {
                "project": project.model_dump(mode="json"),
                "workloads": [
                    w.model_dump(mode="json")
                    for w in workload_views.workloads_view(session, project.id)
                ],
            }
            for project in workload_views.projects_view(session)
        ]


def _profile(args: argparse.Namespace) -> Any:
    from backend.core.config import get_settings
    from backend.models.database import session_scope
    from backend.models.workloads import HotspotSort, ProfileRequest
    from backend.services import profiling

    request = ProfileRequest(
        workload_id=_workload_id(args.workload), size=args.size, enable_sampling=args.sampling
    )
    with session_scope() as session:
        run_id = profiling.create_profile_run(session, request).id
    profiling.execute_profile_run(run_id, get_settings(), _progress)
    with session_scope() as session:
        view = profiling.profile_view(session, run_id).model_dump(mode="json")
        view["hotspots"] = [
            h.model_dump(mode="json")
            for h in profiling.list_hotspots(
                session, run_id, sort=HotspotSort(args.sort), limit=args.top
            )
        ]
    return view


def _benchmark_one(args: argparse.Namespace, slug: str) -> dict[str, Any]:
    from backend.core.config import get_settings
    from backend.models.database import session_scope
    from backend.models.workloads import BenchmarkRequest
    from backend.services import benchmarks

    request = BenchmarkRequest(
        workload_id=_workload_id(slug),
        size=args.size,
        warmups=args.warmups,
        runs=args.runs,
        memory_runs=args.memory_runs,
        threshold_percent=args.threshold,
        seed=args.seed,
    )
    with session_scope() as session:
        run_id = benchmarks.create_benchmark_run(session, request).id
    benchmarks.execute_benchmark_run(run_id, get_settings(), _progress)
    with session_scope() as session:
        view = benchmarks.benchmark_view(session, run_id).model_dump(mode="json")
    return _summary(slug, view) if args.summary else view


def _summary(slug: str, view: dict[str, Any]) -> dict[str, Any]:
    summary = view["summary"]
    memory = view["memory"]
    return {
        "workload": slug,
        "benchmark_id": view["id"],
        "candidate": view["candidate"]["label"],
        "size": view["size"],
        "settings": view["settings"],
        "median_baseline_ms": (summary.get("baseline") or {}).get("wall_ms", {}).get("median"),
        "median_candidate_ms": (summary.get("candidate") or {}).get("wall_ms", {}).get("median"),
        "speedup": view["speedup"],
        "ci95": [view["speedup_ci_low"], view["speedup_ci_high"]],
        "latency_reduction_percent": view["latency_reduction_percent"],
        "cpu_speedup": summary.get("cpu_speedup"),
        "classification": view["classification"],
        "reason": view["classification_reason"],
        "memory": {
            variant: memory.get(variant)
            for variant in ("baseline", "candidate")
            if memory.get(variant)
        },
        "warnings": view["warnings"],
    }


def _benchmark(args: argparse.Namespace) -> Any:
    return _benchmark_one(args, args.workload)


def _benchmark_all(args: argparse.Namespace) -> Any:
    from backend.services.workloads import discover_bundled

    return [_benchmark_one(args, slug) for slug, _path, _manifest in discover_bundled()]


def _add_benchmark_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--size", default=None, help="fixture size (default: workload default)")
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--memory-runs", type=int, default=3)
    parser.add_argument("--threshold", type=float, default=5.0, help="practical threshold, %%")
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--summary", action="store_true", help="print a compact summary")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_workload", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list registered workloads").set_defaults(fn=_list)
    profile = sub.add_parser("profile", help="profile a bundled workload")
    profile.add_argument("workload")
    profile.add_argument("--size", default=None)
    profile.add_argument("--sampling", action="store_true", help="also run py-spy if available")
    profile.add_argument(
        "--sort", choices=["exclusive", "cumulative", "calls"], default="exclusive"
    )
    profile.add_argument("--top", type=int, default=15)
    profile.set_defaults(fn=_profile)
    bench = sub.add_parser("benchmark", help="benchmark original vs bundled reference")
    bench.add_argument("workload")
    _add_benchmark_options(bench)
    bench.set_defaults(fn=_benchmark)
    bench_all = sub.add_parser("benchmark-all", help="benchmark every bundled workload")
    _add_benchmark_options(bench_all)
    bench_all.set_defaults(fn=_benchmark_all)
    args = parser.parse_args(argv)

    from backend.core.config import get_settings
    from backend.core.logging import configure_logging
    from backend.models.database import init_engine, session_scope
    from backend.services.workloads import seed_bundled_examples

    settings = get_settings()
    configure_logging("WARNING")
    init_engine(settings)
    with session_scope() as session:
        seed_bundled_examples(session)
    print(json.dumps(args.fn(args), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
