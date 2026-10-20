"""CSV renderers: the findings table, and raw benchmark samples when measured data exists.

Cells are redacted individually and text cells that a spreadsheet would treat as a formula are
prefixed with an apostrophe (CSV/formula injection).
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from backend.reporting.model import AuditReport
from backend.reporting.redaction import redact_report_text

FINDING_COLUMNS = [
    "rank",
    "id",
    "rule_id",
    "severity",
    "confidence",
    "provenance",
    "category",
    "title",
    "file_path",
    "line_start",
    "line_end",
    "symbol",
    "priority_score",
    "permalink",
    "measured_cumulative_time_ms",
    "measured_call_count",
]

SAMPLE_COLUMNS = [
    "benchmark_id",
    "benchmark_name",
    "provenance",
    "variant",
    "run_index",
    "wall_time_ms",
    "peak_memory_mb",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> tuple[object, int]:
    if value is None:
        return "", 0
    if isinstance(value, str):
        result = redact_report_text(value)
        text = result.text
        if text.startswith(_FORMULA_PREFIXES):
            text = "'" + text
        return text, result.count
    return value, 0


def _write(header: list[str], rows: Iterable[list[object]]) -> tuple[str, int]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    total = 0
    for row in rows:
        cells = []
        for value in row:
            cell, count = _cell(value)
            cells.append(cell)
            total += count
        writer.writerow(cells)
    return buffer.getvalue(), total


def render_findings_csv(report: AuditReport) -> tuple[str, int]:
    permalinks = {e.finding_id: e.permalink for e in report.evidence.entries}
    return _write(
        FINDING_COLUMNS,
        (
            [
                f.rank,
                f.id,
                f.rule_id,
                f.severity.value,
                f.confidence.value,
                f.provenance.value,
                f.category.value,
                f.title,
                f.file_path,
                f.line_start,
                f.line_end,
                f.symbol,
                f.priority_score,
                permalinks.get(f.id),
                f.measured.cumulative_time_ms if f.measured else None,
                f.measured.call_count if f.measured else None,
            ]
            for f in report.findings.entries
        ),
    )


def render_benchmark_samples_csv(report: AuditReport) -> tuple[str, int] | None:
    if report.benchmarks is None:
        return None
    rows: list[list[object]] = [
        [
            result.benchmark_id,
            result.name,
            result.provenance.value,
            sample.variant,
            sample.run_index,
            sample.wall_time_ms,
            sample.peak_memory_mb,
        ]
        for result in report.benchmarks.results
        for sample in result.samples
    ]
    if not rows:
        return None
    return _write(SAMPLE_COLUMNS, rows)
