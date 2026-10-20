"""Normalize cProfile output (``.prof`` files) into hotspot records.

Definitions (see ``docs/methodology.md``):

* ``internal_time_ms``  - pstats ``tottime``: time spent in the function itself (exclusive).
* ``cumulative_time_ms`` - pstats ``cumtime``: time in the function and everything it called.
* ``percentage_of_profiled_time`` = ``tottime / sum(tottime over all functions) * 100``.
  Exclusive percentages add up to 100 across all functions.
* ``cumulative_percentage`` = ``cumtime / sum(tottime over all functions) * 100``. Cumulative
  times overlap (a caller includes its callees), so these are shown separately and never summed.
"""

from __future__ import annotations

import pstats
import sysconfig
from collections.abc import Callable, Iterable
from pathlib import Path

from pydantic import BaseModel

from backend.models.workloads import HotspotLocation, HotspotSort
from backend.profiling.runner import HARNESS_PATH

MAX_STORED_HOTSPOTS = 2000


class HotspotRecord(BaseModel):
    rank: int = 0
    function_name: str
    file_path: str
    location_kind: HotspotLocation
    line_number: int
    primitive_calls: int
    total_calls: int
    internal_time_ms: float
    cumulative_time_ms: float
    percentage_of_profiled_time: float
    cumulative_percentage: float
    source_available: bool
    is_user_code: bool


class ParsedProfile(BaseModel):
    total_profiled_time_ms: float
    function_count: int
    hotspots: list[HotspotRecord]


def _library_roots() -> list[tuple[Path, HotspotLocation, str]]:
    paths = sysconfig.get_paths()
    roots: list[tuple[Path, HotspotLocation, str]] = []
    for key in ("purelib", "platlib"):
        roots.append((Path(paths[key]).resolve(), HotspotLocation.SITE_PACKAGES, "<site-packages>"))
    for key in ("stdlib", "platstdlib"):
        roots.append((Path(paths[key]).resolve(), HotspotLocation.STDLIB, "<stdlib>"))
    return roots


def classify_location(filename: str, workspace_root: Path) -> tuple[HotspotLocation, str, bool]:
    """Return ``(kind, display_path, source_available)`` for a pstats filename.

    Workspace files are shown relative to the workspace. Library files are shown relative to
    their library root with a ``<stdlib>``/``<site-packages>`` marker; other absolute host
    paths are reduced to their file name so host layout is not leaked.
    """
    if filename == "~":
        return HotspotLocation.BUILTIN, "<built-in>", False
    if filename.startswith("<"):
        return HotspotLocation.EXTERNAL, filename, False
    path = Path(filename)
    if not path.is_absolute():
        return HotspotLocation.EXTERNAL, f"<external>/{path.name}", False
    resolved = path.resolve()
    if resolved == HARNESS_PATH:
        return HotspotLocation.HARNESS, "<harness>/harness_entry.py", False
    root = workspace_root.resolve()
    if resolved.is_relative_to(root):
        return HotspotLocation.WORKSPACE, resolved.relative_to(root).as_posix(), resolved.is_file()
    for library_root, kind, marker in _library_roots():
        if resolved.is_relative_to(library_root):
            return kind, f"{marker}/{resolved.relative_to(library_root).as_posix()}", False
    return HotspotLocation.EXTERNAL, f"<external>/{path.name}", False


def _is_profiler_frame(function_name: str) -> bool:
    return "_lsprof.Profiler" in function_name


StatsRow = tuple[str, int, str, int, int, float, float]
"""``(filename, line, function, cc, nc, tottime_s, cumtime_s)`` — one pstats entry."""

LocationClassifier = Callable[[str], tuple[HotspotLocation, str, bool]]


def parse_profile(prof_path: Path, workspace_root: Path) -> ParsedProfile:
    """Parse a cProfile dump. All functions are kept; ``rank`` orders them by exclusive time.

    Only for ``.prof`` files PerfPilot produced itself: pstats unmarshals the file, which is not
    safe for untrusted input. Sandbox runs use :func:`build_profile` on the harness's JSON table.
    """
    stats = pstats.Stats(str(prof_path))
    raw = stats.stats  # type: ignore[attr-defined]
    rows = [
        (filename, int(line), func, int(cc), int(nc), float(tt), float(ct))
        for (filename, line, func), (cc, nc, tt, ct, _callers) in raw.items()
    ]
    return build_profile(rows, lambda filename: classify_location(filename, workspace_root))


def build_profile(rows: Iterable[StatsRow], classify: LocationClassifier) -> ParsedProfile:
    """Turn pstats rows into ranked hotspot records using ``classify`` for file locations."""
    materialized = list(rows)
    total_tt = sum(row[5] for row in materialized)
    records: list[HotspotRecord] = []
    for filename, line, func, cc, nc, tt, ct in materialized:
        kind, display, available = classify(filename)
        if _is_profiler_frame(func):
            kind = HotspotLocation.HARNESS
        records.append(
            HotspotRecord(
                function_name=func,
                file_path=display,
                location_kind=kind,
                line_number=int(line),
                primitive_calls=int(cc),
                total_calls=int(nc),
                internal_time_ms=tt * 1000.0,
                cumulative_time_ms=ct * 1000.0,
                percentage_of_profiled_time=(tt / total_tt * 100.0) if total_tt > 0 else 0.0,
                cumulative_percentage=(ct / total_tt * 100.0) if total_tt > 0 else 0.0,
                source_available=available,
                is_user_code=kind == HotspotLocation.WORKSPACE,
            )
        )
    ranked = rank_hotspots(records, HotspotSort.EXCLUSIVE)
    for index, record in enumerate(ranked, start=1):
        record.rank = index
    return ParsedProfile(
        total_profiled_time_ms=total_tt * 1000.0,
        function_count=len(records),
        hotspots=ranked,
    )


def rank_hotspots(
    records: list[HotspotRecord],
    sort: HotspotSort,
    *,
    user_only: bool = False,
    limit: int | None = None,
) -> list[HotspotRecord]:
    """Sort hotspots; ``user_only`` drops builtin, stdlib, site-packages and harness frames."""
    selected = [r for r in records if r.is_user_code] if user_only else list(records)

    def key(r: HotspotRecord) -> tuple[float, float, str, int, str]:
        if sort == HotspotSort.CUMULATIVE:
            primary, secondary = r.cumulative_time_ms, r.internal_time_ms
        elif sort == HotspotSort.CALLS:
            primary, secondary = float(r.total_calls), r.internal_time_ms
        else:
            primary, secondary = r.internal_time_ms, float(r.total_calls)
        return -primary, -secondary, r.file_path, r.line_number, r.function_name

    selected.sort(key=key)
    return selected if limit is None else selected[:limit]
