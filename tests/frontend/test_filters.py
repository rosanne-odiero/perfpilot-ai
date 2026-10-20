from __future__ import annotations

from typing import Any

import pandas as pd

from frontend.components.file_explorer import (
    FileFilters,
    files_frame,
    filter_files,
    finding_lines,
    folders,
)
from frontend.components.findings_table import (
    FindingFilters,
    apply_filters,
    filter_options,
    findings_frame,
    location_label,
    sort_findings,
)


def test_no_filters_keeps_everything_sorted_by_priority(findings: list[dict[str, Any]]) -> None:
    shown = apply_filters(findings, FindingFilters())
    assert len(shown) == len(findings)
    priorities = [f["priority_score"] for f in shown]
    assert priorities == sorted(priorities, reverse=True)
    assert not FindingFilters().active


def test_filters_combine(findings: list[dict[str, Any]]) -> None:
    filters = FindingFilters(severities=frozenset({"HIGH"}), path_query="ORDERS")
    shown = apply_filters(findings, filters)
    assert shown
    assert all(f["severity"] == "HIGH" for f in shown)
    assert all("orders" in f["location"]["file_path"] for f in shown)
    assert filters.active


def test_rule_category_provenance_priority_and_module_filters(
    findings: list[dict[str, Any]],
) -> None:
    rule = findings[0]["rule_id"]
    assert {f["rule_id"] for f in apply_filters(findings, FindingFilters(rule_id=rule))} == {rule}
    cat = findings[0]["category"]
    by_cat = apply_filters(findings, FindingFilters(categories=frozenset({cat})))
    assert {f["category"] for f in by_cat} == {cat}
    assert apply_filters(findings, FindingFilters(provenance="MEASURED")) == []
    assert len(apply_filters(findings, FindingFilters(provenance="STATIC"))) == len(findings)
    top = max(f["priority_score"] for f in findings)
    assert all(
        f["priority_score"] >= top
        for f in apply_filters(findings, FindingFilters(min_priority=top))
    )
    path = findings[0]["location"]["file_path"]
    module = apply_filters(findings, FindingFilters(file_paths=frozenset({path})))
    assert module
    assert {f["location"]["file_path"] for f in module} == {path}


def test_filter_options_follow_severity_order(findings: list[dict[str, Any]]) -> None:
    options = filter_options(findings)
    order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    assert options["severities"] == [s for s in order if s in options["severities"]]
    assert options["provenances"] == ["STATIC"]
    assert len(options["rules"]) == len({f["rule_id"] for f in findings})


def test_findings_frame_and_labels(findings: list[dict[str, Any]]) -> None:
    frame = findings_frame(findings)
    assert list(frame.columns)[:3] == ["Priority", "Severity", "Finding"]
    assert len(frame) == len(findings)
    multi = next(f for f in findings if f["location"]["line_end"] > f["location"]["line_start"])
    loc = multi["location"]
    assert location_label(multi) == f"{loc['file_path']}:{loc['line_start']}-{loc['line_end']}"
    assert findings_frame([]).empty


def test_sort_is_deterministic_on_ties() -> None:
    base = {"severity": "LOW", "priority_score": 1.0}
    a = {**base, "location": {"file_path": "b.py", "line_start": 1}}
    b = {**base, "location": {"file_path": "a.py", "line_start": 9}}
    c = {**base, "severity": "HIGH", "location": {"file_path": "z.py", "line_start": 1}}
    assert sort_findings([a, b, c]) == [c, b, a]


FILES = [
    {"path": "pkg/", "size_bytes": 0, "skipped": False},
    {"path": "pkg/a.py", "size_bytes": 10, "language": "Python", "skipped": False},
    {"path": "pkg/sub/b.py", "size_bytes": 20, "language": "Python", "skipped": False},
    {"path": ".env", "size_bytes": 5, "skipped": True, "skip_reason": "sensitive"},
    {"path": "README.md", "size_bytes": 7, "language": "Markdown", "skipped": False},
]
COUNTS = [
    {
        "file_path": "pkg/sub/b.py",
        "total": 3,
        "by_severity": {"MEDIUM": 2, "HIGH": 1},
        "max_priority": 9.5,
    }
]


def test_files_frame_joins_counts_and_drops_directories() -> None:
    frame = files_frame(FILES, COUNTS)
    assert frame["Path"].tolist()[0] == "pkg/sub/b.py"
    assert "pkg/" not in frame["Path"].tolist()
    row = frame.iloc[0]
    assert (row["Findings"], row["Max severity"], row["Max priority"]) == (3, "HIGH", 9.5)
    env = frame[frame["Path"] == ".env"].iloc[0]
    assert env["Status"] == "skipped: sensitive"
    assert folders(frame["Path"].tolist()) == ["pkg", "pkg/sub"]


def test_file_filters() -> None:
    frame = files_frame(FILES, COUNTS)

    def paths(filters: FileFilters) -> list[str]:
        return sorted(filter_files(frame, filters)["Path"].tolist())

    assert paths(FileFilters(folder="pkg")) == ["pkg/a.py", "pkg/sub/b.py"]
    assert paths(FileFilters(folder="pkg/sub")) == ["pkg/sub/b.py"]
    assert paths(FileFilters(only_with_findings=True)) == ["pkg/sub/b.py"]
    assert ".env" not in paths(FileFilters(include_skipped=False))
    assert paths(FileFilters(languages=("Markdown",))) == ["README.md"]
    assert paths(FileFilters(query="A.PY")) == ["pkg/a.py"]
    assert isinstance(filter_files(frame, FileFilters()), pd.DataFrame)


def test_finding_lines_cover_ranges(findings: list[dict[str, Any]]) -> None:
    multi = next(f for f in findings if f["location"]["line_end"] > f["location"]["line_start"])
    loc = multi["location"]
    lines = finding_lines(findings, loc["file_path"])
    assert set(range(loc["line_start"], loc["line_end"] + 1)) <= lines
    assert finding_lines(findings, "no/such/file.py") == set()
