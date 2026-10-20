from __future__ import annotations

import cProfile
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from backend.models.workloads import HotspotLocation, HotspotSort
from backend.profiling.hotspots import classify_location, parse_profile, rank_hotspots
from backend.profiling.runner import HARNESS_PATH

SOURCE = """
import json


def fib(n):
    return n if n < 2 else fib(n - 1) + fib(n - 2)


def leaf(values):
    return sorted(values)


def main():
    total = 0
    for i in range(200):
        total += len(leaf(list(range(i, 0, -1))))
    json.dumps({"x": list(range(50))})
    return total + fib(15)
"""


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("hotspot_sample_module", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def profiled(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mod.py").write_text(SOURCE)
    module = _load(workspace / "mod.py")
    profiler = cProfile.Profile()
    profiler.enable()
    module.main()
    profiler.disable()
    prof = tmp_path / "out.prof"
    profiler.dump_stats(str(prof))
    return prof, workspace


def test_parse_real_profile(profiled: tuple[Path, Path]) -> None:
    prof, workspace = profiled
    parsed = parse_profile(prof, workspace)
    by_name = {h.function_name: h for h in parsed.hotspots if h.is_user_code}
    assert set(by_name) == {"main", "leaf", "fib"}
    main = by_name["main"]
    assert main.file_path == "mod.py"
    assert main.line_number == 13
    assert main.source_available is True
    assert main.location_kind == HotspotLocation.WORKSPACE
    assert by_name["leaf"].total_calls == 200
    assert by_name["leaf"].primitive_calls == 200
    fib = by_name["fib"]
    assert fib.primitive_calls == 1
    assert fib.total_calls > 1000  # recursive calls are counted in total, not primitive
    assert 80.0 < main.cumulative_percentage <= 100.0 + 1e-6
    assert main.cumulative_time_ms >= fib.cumulative_time_ms


def test_exclusive_percentages_sum_to_100_and_cumulative_overlap(
    profiled: tuple[Path, Path],
) -> None:
    prof, workspace = profiled
    parsed = parse_profile(prof, workspace)
    exclusive = sum(h.percentage_of_profiled_time for h in parsed.hotspots)
    assert exclusive == pytest.approx(100.0)
    assert sum(h.internal_time_ms for h in parsed.hotspots) == pytest.approx(
        parsed.total_profiled_time_ms
    )
    assert sum(h.cumulative_percentage for h in parsed.hotspots) > 100.0
    for record in parsed.hotspots:
        assert record.cumulative_time_ms >= record.internal_time_ms - 1e-9


def test_builtins_and_stdlib_are_classified_and_filtered(profiled: tuple[Path, Path]) -> None:
    prof, workspace = profiled
    parsed = parse_profile(prof, workspace)
    kinds = {h.location_kind for h in parsed.hotspots}
    assert HotspotLocation.BUILTIN in kinds
    assert HotspotLocation.STDLIB in kinds  # json.dumps lives in the stdlib
    sorted_builtin = [h for h in parsed.hotspots if "sorted" in h.function_name]
    assert sorted_builtin
    assert sorted_builtin[0].file_path == "<built-in>"
    assert sorted_builtin[0].source_available is False
    user = rank_hotspots(parsed.hotspots, HotspotSort.EXCLUSIVE, user_only=True)
    assert {h.function_name for h in user} == {"main", "leaf", "fib"}
    assert all(h.is_user_code for h in user)
    assert len(parsed.hotspots) > len(user)
    stdlib = [h for h in parsed.hotspots if h.location_kind == HotspotLocation.STDLIB]
    assert all(h.file_path.startswith("<stdlib>/") for h in stdlib)
    assert not any(str(Path.home()) in h.file_path for h in parsed.hotspots)


def test_ranking_orders(profiled: tuple[Path, Path]) -> None:
    prof, workspace = profiled
    parsed = parse_profile(prof, workspace)
    assert [h.rank for h in parsed.hotspots] == list(range(1, len(parsed.hotspots) + 1))
    exclusive = rank_hotspots(parsed.hotspots, HotspotSort.EXCLUSIVE)
    assert [h.internal_time_ms for h in exclusive] == sorted(
        (h.internal_time_ms for h in exclusive), reverse=True
    )
    cumulative = rank_hotspots(parsed.hotspots, HotspotSort.CUMULATIVE, limit=3)
    assert len(cumulative) == 3
    assert cumulative[0].cumulative_time_ms == max(h.cumulative_time_ms for h in parsed.hotspots)
    calls = rank_hotspots(parsed.hotspots, HotspotSort.CALLS)
    assert calls[0].total_calls == max(h.total_calls for h in parsed.hotspots)
    assert json.loads(parsed.model_dump_json())["function_count"] == len(parsed.hotspots)


def test_classify_location_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    assert classify_location(str(tmp_path / "a.py"), tmp_path) == (
        HotspotLocation.WORKSPACE,
        "a.py",
        True,
    )
    assert classify_location("~", tmp_path)[0] == HotspotLocation.BUILTIN
    assert classify_location("<frozen importlib._bootstrap>", tmp_path)[0] == (
        HotspotLocation.EXTERNAL
    )
    assert classify_location(str(HARNESS_PATH), tmp_path)[0] == HotspotLocation.HARNESS
    kind, display, available = classify_location("/opt/elsewhere/secret_dir/thing.py", tmp_path)
    assert kind == HotspotLocation.EXTERNAL
    assert display == "<external>/thing.py"
    assert available is False
    assert classify_location(json.__file__, tmp_path)[1] == "<stdlib>/json/__init__.py"
