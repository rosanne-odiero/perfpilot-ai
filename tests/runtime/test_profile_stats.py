"""Parsing the harness's JSON pstats table (untrusted sandbox output) into measured hotspots."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from backend.core.exceptions import ValidationFailedError
from backend.models.workloads import HotspotLocation
from backend.profiling.runner import HARNESS_PATH
from backend.runtime import profile_stats
from backend.runtime.profile_stats import load_stats_table, measure_profile, sandbox_classifier


def _write(path: Path, entries: list[Any], **header: Any) -> Path:
    document = {"format": "perfpilot-pstats", "version": 1, "entries": entries, **header}
    path.write_text(json.dumps(document))
    return path


@pytest.mark.parametrize(
    ("filename", "kind", "display"),
    [
        ("/workspace/shopfast/orders.py", HotspotLocation.WORKSPACE, "shopfast/orders.py"),
        ("/workspace/../etc/passwd", HotspotLocation.EXTERNAL, "<external>/passwd"),
        ("/perfpilot/harness_entry.py", HotspotLocation.HARNESS, "<harness>/harness_entry.py"),
        ("/deps/requests/api.py", HotspotLocation.SITE_PACKAGES, "<site-packages>/requests/api.py"),
        (
            "/usr/local/lib/python3.12/site-packages/_pytest/main.py",
            HotspotLocation.SITE_PACKAGES,
            "<site-packages>/_pytest/main.py",
        ),
        (
            "/usr/local/lib/python3.12/json/decoder.py",
            HotspotLocation.STDLIB,
            "<stdlib>/json/decoder.py",
        ),
        ("~", HotspotLocation.BUILTIN, "<built-in>"),
        (
            "<frozen importlib._bootstrap>",
            HotspotLocation.EXTERNAL,
            "<frozen importlib._bootstrap>",
        ),
        ("/home/someone/private/thing.py", HotspotLocation.EXTERNAL, "<external>/thing.py"),
    ],
)
def test_sandbox_paths_are_classified_without_leaking_host_paths(
    tmp_path: Path, filename: str, kind: HotspotLocation, display: str
) -> None:
    (tmp_path / "shopfast").mkdir()
    (tmp_path / "shopfast" / "orders.py").write_text("x = 1\n")
    found_kind, found_display, _ = sandbox_classifier(tmp_path)(filename)
    assert (found_kind, found_display) == (kind, display)


def test_host_workspace_paths_still_classify(tmp_path: Path) -> None:
    (tmp_path / "pkg.py").write_text("x = 1\n")
    kind, display, available = sandbox_classifier(tmp_path)(str(tmp_path / "pkg.py"))
    assert (kind, display, available) == (HotspotLocation.WORKSPACE, "pkg.py", True)


def test_attributed_time_includes_direct_library_calls(tmp_path: Path) -> None:
    f = ["/workspace/m.py", 1, "f"]
    lam = ["/workspace/m.py", 5, "<lambda>"]
    count = ["~", 0, "<method 'count' of 'list' objects>"]
    sort = ["~", 0, "<built-in method builtins.sorted>"]
    entries = [
        [*f, 1, 1, 0.1, 0.65, []],
        [*count, 10, 10, 0.3, 0.3, [[*f, 10, 10, 0.3, 0.3]]],
        [*sort, 1, 1, 0.05, 0.15, [[*f, 1, 1, 0.05, 0.15]]],
        [*lam, 4, 4, 0.1, 0.1, [[*sort, 4, 4, 0.1, 0.1]]],
    ]
    profile = measure_profile(_write(tmp_path / "s.json", entries), tmp_path)
    assert profile.total_profiled_time_ms == pytest.approx(550.0)
    by_name = {h.function_name: h for h in profile.hotspots}
    fn = by_name["f"]
    assert fn.is_user_code
    assert fn.internal_time_ms == pytest.approx(100.0)
    assert fn.attributed_time_ms == pytest.approx(550.0)  # 0.1 self + 0.3 count + 0.15 sorted
    assert fn.attributed_percentage == pytest.approx(100.0)
    assert fn.percentage_of_profiled_time == pytest.approx(100 / 5.5)
    assert by_name["<lambda>"].attributed_time_ms == pytest.approx(100.0)  # user callee: not added
    assert by_name["<method 'count' of 'list' objects>"].attributed_time_ms == pytest.approx(300.0)


def test_attributed_time_is_capped_at_cumulative(tmp_path: Path) -> None:
    f = ["/workspace/m.py", 1, "f"]
    entries = [
        [*f, 1, 1, 0.1, 0.2, []],
        ["~", 0, "x", 1, 1, 0.5, 0.5, [[*f, 1, 1, 0.5, 0.5]]],
    ]
    profile = measure_profile(_write(tmp_path / "s.json", entries), tmp_path)
    assert next(h for h in profile.hotspots if h.function_name == "f").attributed_time_ms == 200.0


@pytest.mark.parametrize(
    "entries",
    [
        [["/workspace/m.py", 1, "f", 1, 1, float("nan"), 0.1, []]],
        [["/workspace/m.py", 1, "f", 1, 1, -1.0, 0.1, []]],
        [["/workspace/m.py", 1, "f", 1, True, 0.1, 0.1, []]],
        [["/workspace/m.py", "1", "f", 1, 1, 0.1, 0.1, []]],
        [["/workspace/m.py", 1, "f", 1, 1, 0.1, 0.1]],
        [["/workspace/m.py", 1, "f" * 5000, 1, 1, 0.1, 0.1, []]],
        [["/workspace/m.py", 1, "f", 1, 1, 0.1, 0.1, [["x", 1, "g", 1, 1]]]],
        ["not-a-list"],
    ],
)
def test_malformed_tables_are_rejected(tmp_path: Path, entries: list[Any]) -> None:
    with pytest.raises(ValidationFailedError):
        load_stats_table(_write(tmp_path / "s.json", entries))


def test_wrong_format_and_size_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(ValidationFailedError, match="not valid JSON"):
        load_stats_table(tmp_path / "bad.json")
    with pytest.raises(ValidationFailedError, match="unexpected format"):
        load_stats_table(_write(tmp_path / "s.json", [], format="other"))
    monkeypatch.setattr(profile_stats, "MAX_ENTRIES", 1)
    row = ["/workspace/m.py", 1, "f", 1, 1, 0.1, 0.1, []]
    with pytest.raises(ValidationFailedError, match="exceed"):
        load_stats_table(_write(tmp_path / "s.json", [row, row]))


def _run_harness(tmp_path: Path, command: dict[str, Any]) -> tuple[int, dict[str, Any], Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / "work.py").write_text(
        "def build(n):\n    return [str(i) * 3 for i in range(n)]\n\n"
        "if __name__ == '__main__':\n    build(20000)\n"
    )
    out = tmp_path / "out"
    out.mkdir()
    request = tmp_path / "req.json"
    request.write_text(
        json.dumps(
            {
                "mode": "command",
                "workspace_root": str(workspace),
                "output_dir": str(out),
                "command": command,
            }
        )
    )
    proc = subprocess.run(
        [sys.executable, "-P", "-s", "-B", str(HARNESS_PATH), str(request), str(out / "r.json")],
        cwd=workspace,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, json.loads((out / "r.json").read_text()), workspace


def test_harness_writes_json_stats_and_traced_memory(tmp_path: Path) -> None:
    code, result, workspace = _run_harness(
        tmp_path,
        {
            "kind": "script",
            "target": "work.py",
            "args": [],
            "trace_memory": True,
            "stats_json": True,
        },
    )
    assert code == 0, result
    assert result["profile"]["stats_file"] == "profile_stats.json"
    assert result["profile"]["tracemalloc_active"] is True
    assert result["memory"]["tracemalloc_peak_bytes"] > 0
    profile = measure_profile(tmp_path / "out" / "profile_stats.json", workspace)
    build = next(h for h in profile.hotspots if h.function_name == "build")
    assert (build.file_path, build.line_number, build.is_user_code) == ("work.py", 1, True)


def test_harness_flags_default_off_and_are_validated(tmp_path: Path) -> None:
    code, result, _ = _run_harness(tmp_path, {"kind": "script", "target": "work.py"})
    assert code == 0
    assert "memory" not in result
    assert "stats_file" not in result["profile"]
    assert not (tmp_path / "out" / "profile_stats.json").exists()
    code, result, _ = _run_harness(
        tmp_path / "again", {"kind": "script", "target": "work.py", "stats_json": "yes"}
    )
    assert code == 1
    assert result["error"]["code"] == "bad_request"
