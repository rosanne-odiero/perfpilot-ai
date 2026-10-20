"""Harness ``command`` mode with ``profile: false`` (unprofiled timing used by A/B benchmarks)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.profiling.runner import ResourceLimits, run_harness

LIMITS = ResourceLimits(timeout_seconds=60, memory_limit_mb=1024)
SCRIPT = "import time\n\ntime.sleep(0.05)\nprint(sum(range(10_000)))\n"


def _request(workspace: Path, out: Path, **command: Any) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    return {
        "mode": "command",
        "workspace_root": str(workspace),
        "output_dir": str(out),
        "command": {"kind": "script", "target": "job.py", "args": [], **command},
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "job.py").write_text(SCRIPT)
    return root


def test_unprofiled_command_reports_only_the_command_timing(
    tmp_path: Path, workspace: Path
) -> None:
    out = tmp_path / "out"
    run = run_harness(
        _request(workspace, out, profile=False), run_dir=tmp_path / "run", limits=LIMITS
    )
    assert run.result is not None
    assert run.result["status"] == "ok", run.result
    timing = run.result["timing"]
    assert timing["profiled"] is False
    assert timing["wall_ns"] >= 50_000_000  # the script sleeps 50 ms
    assert 0 < timing["cpu_ns"] < timing["wall_ns"]
    assert run.result["command"]["exit_code"] == 0
    assert "profile" not in run.result
    assert not (out / "profile.prof").exists()
    assert run.result["process"]["ru_maxrss_bytes"] > 0


def test_profiled_command_is_unchanged_by_default(tmp_path: Path, workspace: Path) -> None:
    out = tmp_path / "out"
    run = run_harness(_request(workspace, out), run_dir=tmp_path / "run", limits=LIMITS)
    assert run.result is not None
    assert run.result["status"] == "ok"
    assert "timing" not in run.result
    assert run.result["profile"]["wall_ns_under_profiler"] > 0
    assert (out / "profile.prof").is_file()


@pytest.mark.parametrize(
    "flags",
    [{"profile": False, "stats_json": True}, {"profile": False, "trace_memory": True}],
)
def test_profiler_outputs_need_the_profiler(
    tmp_path: Path, workspace: Path, flags: dict[str, Any]
) -> None:
    run = run_harness(
        _request(workspace, tmp_path / "out", **flags), run_dir=tmp_path / "run", limits=LIMITS
    )
    assert run.result is not None
    assert run.result["error"]["code"] == "bad_request"


def test_profile_flag_must_be_boolean(tmp_path: Path, workspace: Path) -> None:
    run = run_harness(
        _request(workspace, tmp_path / "out", profile="no"),
        run_dir=tmp_path / "run",
        limits=LIMITS,
    )
    assert run.result is not None
    assert run.result["error"]["code"] == "bad_request"
