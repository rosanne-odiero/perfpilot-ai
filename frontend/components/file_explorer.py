"""File explorer: inventory joined with per-file finding counts, filters and a source viewer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from frontend.formatting import max_severity

SENSITIVE = "sensitive"
COLUMNS = [
    "Path",
    "Folder",
    "Language",
    "Size",
    "Findings",
    "Max severity",
    "Max priority",
    "Status",
]


def folder_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else "."


def folders(paths: list[str]) -> list[str]:
    """Every folder (and ancestor folder) that contains a file, sorted."""
    found: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        found.update("/".join(parts[: i + 1]) for i in range(len(parts)))
    return sorted(found)


def files_frame(files: list[dict[str, Any]], counts: list[dict[str, Any]]) -> pd.DataFrame:
    by_path = {c["file_path"]: c for c in counts}
    rows = []
    for info in files:
        if info["path"].endswith("/"):
            continue
        count = by_path.get(info["path"], {})
        rows.append(
            {
                "Path": info["path"],
                "Folder": folder_of(info["path"]),
                "Language": info.get("language") or "",
                "Size": int(info.get("size_bytes") or 0),
                "Findings": int(count.get("total", 0)),
                "Max severity": max_severity(count.get("by_severity") or {}) or "",
                "Max priority": float(count.get("max_priority", 0.0)),
                "Status": f"skipped: {info.get('skip_reason') or 'unknown'}"
                if info.get("skipped")
                else "analyzed",
            }
        )
    frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame.sort_values(["Findings", "Path"], ascending=[False, True]).reset_index(drop=True)


@dataclass(frozen=True)
class FileFilters:
    query: str = ""
    languages: tuple[str, ...] = ()
    folder: str | None = None
    only_with_findings: bool = False
    include_skipped: bool = True


def filter_files(frame: pd.DataFrame, filters: FileFilters) -> pd.DataFrame:
    mask = pd.Series(True, index=frame.index)
    if filters.query:
        mask &= frame["Path"].str.contains(filters.query, case=False, regex=False)
    if filters.languages:
        mask &= frame["Language"].isin(filters.languages)
    if filters.folder:
        mask &= (frame["Folder"] == filters.folder) | frame["Folder"].str.startswith(
            filters.folder + "/"
        )
    if filters.only_with_findings:
        mask &= frame["Findings"] > 0
    if not filters.include_skipped:
        mask &= frame["Status"] == "analyzed"
    return frame[mask].reset_index(drop=True)


def finding_lines(findings: list[dict[str, Any]], path: str) -> set[int]:
    lines: set[int] = set()
    for finding in findings:
        loc = finding["location"]
        if loc["file_path"] == path:
            lines.update(range(loc["line_start"], loc["line_end"] + 1))
    return lines
