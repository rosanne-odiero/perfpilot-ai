"""Turn the harness's JSON pstats table from a sandbox run into measured hotspot records.

The worker never unmarshals the ``.prof`` written inside the sandbox (``marshal`` is unsafe for
untrusted input). The harness also writes ``profile_stats.json`` (see
``backend/profiling/harness_entry.py``), which is validated here as untrusted data.

Definitions (in addition to ``backend/profiling/hotspots.py``):

* ``attributed_time_ms`` = ``tottime(F) + Σ cumtime(C ← F)`` over callees ``C`` of ``F`` that are
  *not* repository code (builtins, stdlib, site-packages), capped at ``cumtime(F)``. It is the time
  spent on ``F``'s own lines, including library calls made directly from them, e.g. a
  ``cursor.execute`` inside a loop or a ``list.count`` in a condition.
* ``attributed_percentage`` = ``attributed_time_ms / Σ tottime(all functions) * 100``. Unlike the
  exclusive percentages these overlap (a library call that calls back into repository code counts
  for both), so they are never summed.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel

from backend.core.exceptions import ValidationFailedError
from backend.models.workloads import HotspotLocation
from backend.profiling.hotspots import (
    HotspotRecord,
    LocationClassifier,
    StatsRow,
    build_profile,
    classify_location,
)
from backend.sandbox.backend import SANDBOX_DEPS, SANDBOX_EXTRA_ROOT, SANDBOX_WORKSPACE

STATS_FORMAT = "perfpilot-pstats"
MAX_ENTRIES = 200_000
MAX_CALLER_EDGES = 2_000_000
MAX_NAME_CHARS = 4096
MAX_STORED_LIBRARY_HOTSPOTS = 200
MAX_STORED_USER_HOTSPOTS = 2000

_CONTAINER_SITE = re.compile(r"^/usr/(?:local/)?lib/python3\.\d+/(?:site|dist)-packages/(.+)$")
_CONTAINER_STDLIB = re.compile(r"^/usr/(?:local/)?lib/python3\.\d+/(.+)$")

Key = tuple[str, int, str]


class MeasuredHotspot(HotspotRecord):
    attributed_time_ms: float
    attributed_percentage: float


class MeasuredProfile(BaseModel):
    total_profiled_time_ms: float
    function_count: int
    hotspots: list[MeasuredHotspot]


def sandbox_classifier(host_workspace: Path) -> LocationClassifier:
    """Classify pstats filenames recorded inside the container (``/workspace/...``) and, for
    runs whose paths are already host paths, fall back to the host classifier."""
    host_root = host_workspace.resolve()

    def classify(filename: str) -> tuple[HotspotLocation, str, bool]:
        path = PurePosixPath(filename)
        if path.is_absolute() and path.is_relative_to(SANDBOX_WORKSPACE):
            rel = path.relative_to(SANDBOX_WORKSPACE)
            if ".." in rel.parts or not rel.parts:
                return HotspotLocation.EXTERNAL, f"<external>/{path.name}", False
            host = (host_root / rel.as_posix()).resolve()
            available = host.is_relative_to(host_root) and host.is_file()
            return HotspotLocation.WORKSPACE, rel.as_posix(), available
        if path.is_absolute() and path.is_relative_to(SANDBOX_EXTRA_ROOT):
            return HotspotLocation.HARNESS, f"<harness>/{path.name}", False
        if path.is_absolute() and path.is_relative_to(SANDBOX_DEPS):
            return (
                HotspotLocation.SITE_PACKAGES,
                f"<site-packages>/{path.relative_to(SANDBOX_DEPS).as_posix()}",
                False,
            )
        if match := _CONTAINER_SITE.match(filename):
            return HotspotLocation.SITE_PACKAGES, f"<site-packages>/{match[1]}", False
        if match := _CONTAINER_STDLIB.match(filename):
            return HotspotLocation.STDLIB, f"<stdlib>/{match[1]}", False
        return classify_location(filename, host_root)

    return classify


def _name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_NAME_CHARS or "\x00" in value:
        raise ValidationFailedError("profile statistics contain an invalid name")
    return value


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailedError("profile statistics contain an invalid call count")
    return value


def _seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailedError("profile statistics contain an invalid time")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValidationFailedError("profile statistics contain an invalid time")
    return number


def _key(item: list[Any]) -> Key:
    line = item[1]
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        raise ValidationFailedError("profile statistics contain an invalid line number")
    return _name(item[0]), line, _name(item[2])


def load_stats_table(path: Path) -> tuple[list[StatsRow], dict[Key, dict[Key, float]]]:
    """Validate ``profile_stats.json``. Returns rows and ``callers[callee][caller] = cumtime``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationFailedError("profile statistics are not valid JSON") from exc
    if (
        not isinstance(data, dict)
        or data.get("format") != STATS_FORMAT
        or data.get("version") != 1
        or not isinstance(data.get("entries"), list)
    ):
        raise ValidationFailedError("profile statistics have an unexpected format")
    entries: list[Any] = data["entries"]
    if len(entries) > MAX_ENTRIES:
        raise ValidationFailedError(f"profile statistics exceed {MAX_ENTRIES} functions")
    rows: list[StatsRow] = []
    callers: dict[Key, dict[Key, float]] = {}
    edges = 0
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 8 or not isinstance(entry[7], list):
            raise ValidationFailedError("profile statistics contain a malformed entry")
        key = _key(entry)
        rows.append(
            (*key, _count(entry[3]), _count(entry[4]), _seconds(entry[5]), _seconds(entry[6]))
        )
        edges += len(entry[7])
        if edges > MAX_CALLER_EDGES:
            raise ValidationFailedError("profile statistics contain too many call edges")
        by_caller: dict[Key, float] = {}
        for caller in entry[7]:
            if not isinstance(caller, list) or len(caller) != 7:
                raise ValidationFailedError("profile statistics contain a malformed call edge")
            by_caller[_key(caller)] = _seconds(caller[6])
        callers[key] = by_caller
    return rows, callers


def measure_profile(stats_file: Path, host_workspace: Path) -> MeasuredProfile:
    rows, callers = load_stats_table(stats_file)
    classify = sandbox_classifier(host_workspace)
    kinds = {row[:3]: classify(row[0]) for row in rows}
    cumtime = {row[:3]: row[6] for row in rows}
    attributed: dict[Key, float] = {row[:3]: row[5] for row in rows}
    library_time: dict[Key, float] = defaultdict(float)
    for callee, by_caller in callers.items():
        if kinds.get(callee, (HotspotLocation.EXTERNAL,))[0] == HotspotLocation.WORKSPACE:
            continue
        for caller, seconds in by_caller.items():
            library_time[caller] += seconds
    for key, seconds in library_time.items():
        if key in attributed:
            attributed[key] = min(cumtime[key], attributed[key] + seconds)

    parsed = build_profile(rows, lambda filename: classify(filename))
    total_s = parsed.total_profiled_time_ms / 1000.0
    by_display: dict[tuple[str, int, str], float] = {}
    for key, seconds in attributed.items():
        kind, display, _ = kinds[key]
        if kind == HotspotLocation.WORKSPACE:
            by_display[(display, key[1], key[2])] = seconds

    measured: list[MeasuredHotspot] = []
    libraries = 0
    users = 0
    for record in parsed.hotspots:
        if record.is_user_code:
            if users >= MAX_STORED_USER_HOTSPOTS:
                continue
            users += 1
            seconds = by_display.get(
                (record.file_path, record.line_number, record.function_name),
                record.internal_time_ms / 1000.0,
            )
        else:
            if libraries >= MAX_STORED_LIBRARY_HOTSPOTS:
                continue
            libraries += 1
            seconds = record.internal_time_ms / 1000.0
        measured.append(
            MeasuredHotspot(
                **record.model_dump(),
                attributed_time_ms=seconds * 1000.0,
                attributed_percentage=(seconds / total_s * 100.0) if total_s > 0 else 0.0,
            )
        )
    return MeasuredProfile(
        total_profiled_time_ms=parsed.total_profiled_time_ms,
        function_count=parsed.function_count,
        hotspots=measured,
    )
