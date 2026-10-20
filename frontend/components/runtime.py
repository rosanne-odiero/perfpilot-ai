"""Runtime audit helpers (pure): approval request body, tables of commands, runs and hotspots."""

from __future__ import annotations

from typing import Any

import pandas as pd

from frontend.formatting import fmt_dt

LIMIT_FIELDS: tuple[tuple[str, str, float], ...] = (
    ("cpus", "CPUs", 0.5),
    ("memory_mb", "Memory (MB)", 64),
    ("pids", "Processes", 8),
    ("timeout_seconds", "Timeout (s)", 30),
    ("tmpfs_mb", "Scratch tmpfs (MB)", 16),
    ("output_max_bytes", "Output cap (bytes)", 1024 * 1024),
)


class LooserLimitError(ValueError):
    """A requested limit exceeds the server default (the API would answer 422)."""


def limits_override(chosen: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any] | None:
    """Only the limits that differ from the defaults; each must be tighter (smaller)."""
    override: dict[str, Any] = {}
    for name, _, _ in LIMIT_FIELDS:
        if name not in chosen or name not in defaults:
            continue
        value, default = chosen[name], defaults[name]
        if value > default:
            raise LooserLimitError(f"{name}={value} is looser than the default {default}")
        if value != default:
            override[name] = float(value) if isinstance(default, float) else int(value)
    return override or None


def approval_body(
    command: dict[str, Any],
    *,
    approved_by: str,
    limits: dict[str, Any],
    defaults: dict[str, Any],
    allow_dependency_install: bool,
    allow_sdist_builds: bool,
    trace_memory: bool,
) -> dict[str, Any]:
    """``ApprovalRequest`` for the API. ``argv`` is copied verbatim: the approval pins it."""
    body: dict[str, Any] = {
        "command_id": command["id"],
        "argv": list(command["argv"]),
        "approved_by": approved_by,
        "allow_dependency_install": allow_dependency_install,
        "allow_sdist_builds": allow_dependency_install and allow_sdist_builds,
        "trace_memory": trace_memory,
    }
    override = limits_override(limits, defaults)
    if override:
        body["limits_override"] = override
    return body


def commands_frame(commands: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Kind": c.get("kind", ""),
                "Command": " ".join(map(str, c.get("argv") or [])),
                "Working dir": c.get("workdir_rel", ""),
                "Risk": c.get("estimated_risk", ""),
                "Needs dependencies": bool(c.get("requires_dependencies")),
                "Runnable": bool(c.get("runnable")),
                "Why not runnable": c.get("reason_if_not_runnable") or "",
                "Evidence": ", ".join(c.get("evidence") or []),
            }
            for c in commands
        ]
    )


def runs_frame(runs: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Started": fmt_dt(r.get("created_at")),
                "Command": " ".join(r.get("argv") or []),
                "Status": r["status"],
                "Exit (cmd/container)": f"{r.get('command_exit_code')}/{r.get('exit_code')}",
                "Timed out": bool(r.get("timed_out")),
                "OOM": bool(r.get("oom_killed")),
                "Wall ms": r.get("wall_time_ms"),
                "CPU ms": r.get("cpu_time_ms"),
                "Approved by": r.get("approved_by", ""),
            }
            for r in runs
        ]
    )


def _location(path: str, line: int | None) -> str:
    return f"{path}:{line}" if line else path


def hotspots_frame(hotspots: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "#": h["rank"],
                "Function": h["function_name"],
                "Location": _location(h["file_path"], h.get("line_number")),
                "Repository code": h["is_user_code"],
                "Calls": h["total_calls"],
                "Exclusive ms": round(h["internal_time_ms"], 3),
                "Cumulative ms": round(h["cumulative_time_ms"], 3),
                "Attributed ms": round(h["attributed_time_ms"], 3),
                "Attributed %": h["attributed_percentage"],
            }
            for h in hotspots
        ],
        columns=[
            "#",
            "Function",
            "Location",
            "Repository code",
            "Calls",
            "Exclusive ms",
            "Cumulative ms",
            "Attributed ms",
            "Attributed %",
        ],
    )


def runtime_only_frame(hotspots: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Function": h["function_name"],
                "Location": _location(h["location"]["file_path"], h["location"]["line_start"]),
                "Attributed ms": round(h["attributed_time_ms"], 3),
                "Attributed %": round(h["attributed_percentage"], 2),
                "Exclusive ms": round(h["internal_time_ms"], 3),
                "Cumulative ms": round(h["cumulative_time_ms"], 3),
                "Calls": h["call_count"],
                "Test code": h["is_test_code"],
            }
            for h in hotspots
        ]
    )
