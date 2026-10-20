"""Real child-process runs of the harness."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from backend.profiling.hotspots import parse_profile
from backend.profiling.runner import ResourceLimits, plan_limits, run_harness

LIMITS = ResourceLimits(timeout_seconds=30, memory_limit_mb=1024)
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _workspace(
    root: Path, workload: str, fixtures: str = "def make_input(n=3):\n    return list(range(n))\n"
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "workload.py").write_text(workload)
    (root / "fixtures.py").write_text(fixtures)
    return root


def _request(workspace: Path, mode: str = "time", **extra: Any) -> dict[str, Any]:
    return {
        "mode": mode,
        "workspace_root": str(workspace),
        "entrypoint": "workload:run",
        "fixture": "fixtures:make_input",
        "fixture_kwargs": {},
        "invocation_mode": "single",
        **extra,
    }


def test_time_mode_reports_separate_wall_and_cpu_samples(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run(xs):\n    return sum(x * x for x in xs)\n")
    run = run_harness(
        _request(ws, warmups=3, iterations=4, fixture_kwargs={"n": 1000}),
        run_dir=tmp_path / "run",
        limits=LIMITS,
        rss_sample_interval=None,
    )
    assert run.ok, run.failure_reason()
    timing = run.result["timing"]  # type: ignore[index]
    assert timing["warmups"] == 3
    assert len(timing["wall_ns"]) == 4
    assert len(timing["cpu_ns"]) == 4
    assert all(v > 0 for v in timing["wall_ns"])
    assert run.result["input_mutated"] is False  # type: ignore[index]
    assert len(run.result["input_sha256"]) == 64  # type: ignore[index]
    assert run.rss_peak_bytes is None


def test_profile_mode_produces_a_parsable_prof_and_memory(tmp_path: Path) -> None:
    ws = EXAMPLES / "duplicate_counter"
    run = run_harness(
        {
            **_request(ws, mode="profile"),
            "entrypoint": "workload:count_duplicates",
            "fixture_kwargs": {"n_items": 800, "seed": 1},
        },
        run_dir=tmp_path / "run",
        limits=LIMITS,
        rss_sample_interval=0.005,
    )
    assert run.ok, run.failure_reason()
    result = run.result or {}
    prof = tmp_path / "run" / "out" / result["profile"]["prof_file"]
    parsed = parse_profile(prof, ws)
    names = {h.function_name for h in parsed.hotspots if h.is_user_code}
    assert names == {"count_duplicates"}
    count_calls = [
        h for h in parsed.hotspots if "count" in h.function_name and h.location_kind == "builtin"
    ]
    assert count_calls[0].total_calls == 800
    assert result["memory"]["tracemalloc_peak_bytes"] > 0
    assert result["process"]["ru_maxrss_bytes"] > 0
    assert run.rss_peak_bytes is not None
    assert run.rss_peak_bytes > 0
    assert run.rss_samples >= 1


def test_memory_mode_excludes_input_from_tracemalloc(tmp_path: Path) -> None:
    ws = _workspace(
        tmp_path / "ws",
        "def run(xs):\n    return [x * 2 for x in xs]\n",
        "def make_input(n):\n    return list(range(n))\n",
    )
    small = run_harness(
        _request(ws, mode="memory", fixture_kwargs={"n": 10}), run_dir=tmp_path / "a", limits=LIMITS
    )
    large = run_harness(
        _request(ws, mode="memory", fixture_kwargs={"n": 200_000}),
        run_dir=tmp_path / "b",
        limits=LIMITS,
    )
    assert small.ok
    assert large.ok
    small_peak = small.result["memory"]["tracemalloc_peak_bytes"]  # type: ignore[index]
    large_peak = large.result["memory"]["tracemalloc_peak_bytes"]  # type: ignore[index]
    assert small_peak < 10_000
    assert large_peak > 1_000_000  # the output list (~200k ints), not the input


def test_workload_exception_is_reported(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run(xs):\n    raise KeyError('boom')\n")
    run = run_harness(_request(ws), run_dir=tmp_path / "run", limits=LIMITS)
    assert not run.ok
    assert run.result is not None
    assert run.result["error"]["type"] == "KeyError"
    assert "boom" in run.failure_reason()
    assert "Traceback" in run.result["error"]["traceback"]


def test_input_mutation_is_detected(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run(xs):\n    xs.append(1)\n    return len(xs)\n")
    run = run_harness(_request(ws, warmups=1), run_dir=tmp_path / "run", limits=LIMITS)
    assert run.ok
    assert run.result["input_mutated"] is True  # type: ignore[index]


def test_timeout_kills_the_process_group(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "import time\n\ndef run(xs):\n    time.sleep(30)\n")
    run = run_harness(
        _request(ws), run_dir=tmp_path / "run", limits=ResourceLimits(timeout_seconds=1)
    )
    assert run.timed_out
    assert not run.ok
    assert run.wall_seconds < 10
    assert run.failure_reason() == "timed out"


def test_rss_ceiling_kills_runaway_memory(tmp_path: Path) -> None:
    ws = _workspace(
        tmp_path / "ws",
        "import time\n\ndef run(xs):\n    block = bytearray(600 * 1024 * 1024)\n"
        "    time.sleep(5)\n    return len(block)\n",
    )
    run = run_harness(
        _request(ws),
        run_dir=tmp_path / "run",
        limits=ResourceLimits(timeout_seconds=20, memory_limit_mb=200),
        rss_sample_interval=0.01,
    )
    assert run.memory_limit_exceeded
    assert not run.ok


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rlimits")
def test_file_size_limit_is_enforced(tmp_path: Path) -> None:
    ws = _workspace(
        tmp_path / "ws",
        "def run(xs):\n    with open('big.bin', 'wb') as f:\n        f.write(b'x' * 4_000_000)\n"
        "    return 1\n",
    )
    run = run_harness(
        _request(ws),
        run_dir=tmp_path / "run",
        limits=ResourceLimits(timeout_seconds=20, max_file_bytes=1_000_000),
    )
    assert not run.ok


def test_limit_plan_reports_unsupported_limits() -> None:
    plan, report = plan_limits(ResourceLimits(timeout_seconds=10, memory_limit_mb=256))
    assert report.applied["RLIMIT_CPU"] >= 10
    assert "RLIMIT_FSIZE" in report.applied
    assert "wall_clock_timeout" in report.enforced_by_parent
    if sys.platform == "darwin":
        assert "RLIMIT_AS" in report.unsupported
    else:
        assert report.applied["RLIMIT_AS"] == 256 * 1024 * 1024
    assert len(plan) == len(report.applied)


def test_stdout_is_captured_and_truncated(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run(xs):\n    print('y' * 200_000)\n    return 1\n")
    run = run_harness(_request(ws), run_dir=tmp_path / "run", limits=LIMITS)
    assert run.ok
    assert run.stdout_truncated
    assert len(run.stdout) == 64 * 1024


def test_command_mode_runs_script_module_and_pytest(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run(xs):\n    return 1\n")
    (ws / "script.py").write_text("import sys\nprint(sum(range(1000)), sys.argv[1:])\n")
    (ws / "tool.py").write_text("import sys\nsys.exit(3)\n")
    (ws / "test_sample.py").write_text("def test_ok():\n    assert sum(range(10)) == 45\n")
    for index, (kind, target, args, exit_code) in enumerate(
        [
            ("script", "script.py", ["--flag"], 0),
            ("module", "tool", [], 3),
            ("pytest", "test_sample.py", ["-q"], 0),
        ]
    ):
        run = run_harness(
            {
                "mode": "command",
                "workspace_root": str(ws),
                "command": {"kind": kind, "target": target, "args": args},
            },
            run_dir=tmp_path / f"run{index}",
            limits=LIMITS,
        )
        assert run.ok, run.failure_reason()
        assert run.result["command"]["exit_code"] == exit_code  # type: ignore[index]
        prof = tmp_path / f"run{index}" / "out" / "profile.prof"
        assert parse_profile(prof, ws).function_count > 0
    assert (
        "499500"
        in run_harness(
            {
                "mode": "command",
                "workspace_root": str(ws),
                "command": {"kind": "script", "target": "script.py", "args": ["a"]},
            },
            run_dir=tmp_path / "again",
            limits=LIMITS,
        ).stdout
    )
