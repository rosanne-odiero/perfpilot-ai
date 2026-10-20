"""Render an ``ExperimentReport`` to HTML, Markdown, JSON, CSV or PDF, with secret redaction.

Mirrors ``render_report`` for audits: the model is redacted before rendering and every output is
redacted again (structure-aware), using the same helpers and PDF engines.
"""

from __future__ import annotations

import json

from backend.models.enums import ReportFormat
from backend.reporting.experiment_model import ExperimentReport
from backend.reporting.redaction import redact_html, redact_report_text, redact_value
from backend.reporting.renderers.common import RenderedFile, RenderResult, slug
from backend.reporting.renderers.csv_export import _write
from backend.reporting.renderers.experiment_html import render_experiment_html
from backend.reporting.renderers.experiment_markdown import render_experiment_markdown
from backend.reporting.renderers.pdf import html_to_pdf, resolve_engine

CHECKS_ROLE = "validation_checks"

SAMPLE_COLUMNS = [
    "experiment_kind",
    "subject_id",
    "benchmark_id",
    "provenance",
    "variant",
    "kind",
    "pair_index",
    "position_in_pair",
    "wall_ms",
    "cpu_ms",
    "tracemalloc_peak_bytes",
    "rss_peak_bytes",
    "ru_maxrss_bytes",
    "ok",
    "error",
]

CHECK_COLUMNS = [
    "subject_id",
    "validation_id",
    "provenance",
    "check",
    "required",
    "status",
    "detail",
    "duration_seconds",
    "failures",
]


def experiment_filename(report: ExperimentReport, extension: str, suffix: str = "") -> str:
    """``perfpilot-<workload>-<kind>-<id8>[-suffix].<ext>``"""
    name = f"perfpilot-{slug(report.workload.slug)}-{report.kind.value.replace('_', '-')}"
    name += f"-{slug(report.metadata.subject_id, 8)}"
    return f"{name}-{suffix}.{extension}" if suffix else f"{name}.{extension}"


def redact_experiment(report: ExperimentReport) -> tuple[ExperimentReport, int]:
    data, count = redact_value(report.model_dump(mode="json"))
    redacted = ExperimentReport.model_validate(data)
    redacted.metadata.redactions_applied = report.metadata.redactions_applied + count
    return redacted, count


def render_experiment_json(report: ExperimentReport) -> tuple[str, int]:
    data, count = redact_value(report.model_dump(mode="json"))
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n", count


def render_samples_csv(report: ExperimentReport) -> tuple[str, int]:
    """Every raw benchmark sample (preflight, timing, memory). Header only if none ran."""
    bench = report.benchmark
    rows: list[list[object]] = (
        [
            [
                report.kind.value,
                report.metadata.subject_id,
                bench.benchmark_id,
                bench.provenance.value,
                s.variant,
                s.kind,
                s.pair_index,
                s.position_in_pair,
                s.wall_ms,
                s.cpu_ms,
                s.tracemalloc_peak_bytes,
                s.rss_peak_bytes,
                s.ru_maxrss_bytes,
                s.ok,
                s.error,
            ]
            for s in bench.samples
        ]
        if bench is not None
        else []
    )
    return _write(SAMPLE_COLUMNS, rows)


def render_checks_csv(report: ExperimentReport) -> tuple[str, int]:
    validation = report.validation
    rows: list[list[object]] = (
        [
            [
                report.metadata.subject_id,
                validation.validation_id,
                validation.provenance.value,
                c.name,
                c.required,
                c.status,
                c.detail,
                c.duration_seconds,
                " | ".join(c.failures),
            ]
            for c in validation.checks
        ]
        if validation is not None
        else []
    )
    return _write(CHECK_COLUMNS, rows)


def render_experiment_report(
    report: ExperimentReport, fmt: ReportFormat, *, pdf_engine: str | None = None
) -> RenderResult:
    report, count = redact_experiment(report)
    kinds: dict[str, int] = {"report_content": count} if count else {}
    files: list[RenderedFile] = []
    engine: str | None = None

    if fmt in (ReportFormat.HTML, ReportFormat.MARKDOWN, ReportFormat.PDF):
        if fmt == ReportFormat.HTML:
            text = render_experiment_html(report)
        elif fmt == ReportFormat.MARKDOWN:
            text = render_experiment_markdown(report)
        else:
            engine = resolve_engine(pdf_engine)
            metadata = report.metadata.model_copy(update={"pdf_engine": engine})
            text = render_experiment_html(
                report.model_copy(update={"metadata": metadata}), pdf_engine=engine
            )
        redacted = redact_report_text(text) if fmt == ReportFormat.MARKDOWN else redact_html(text)
        count += redacted.count
        for kind, n in redacted.kinds.items():
            kinds[kind] = kinds.get(kind, 0) + n
        data = (
            html_to_pdf(redacted.text, engine)
            if engine is not None
            else redacted.text.encode("utf-8")
        )
        files.append(
            RenderedFile("primary", experiment_filename(report, fmt.value), fmt.media_type, data)
        )
    elif fmt == ReportFormat.JSON:
        text, extra = render_experiment_json(report)
        count += extra
        files.append(
            RenderedFile(
                "primary", experiment_filename(report, "json"), fmt.media_type, text.encode()
            )
        )
    elif fmt == ReportFormat.CSV:
        samples, extra = render_samples_csv(report)
        checks, extra_checks = render_checks_csv(report)
        count += extra + extra_checks
        files.append(
            RenderedFile(
                "primary",
                experiment_filename(report, "csv", "benchmark-samples"),
                fmt.media_type,
                samples.encode("utf-8"),
            )
        )
        files.append(
            RenderedFile(
                CHECKS_ROLE,
                experiment_filename(report, "csv", "validation-checks"),
                fmt.media_type,
                checks.encode("utf-8"),
            )
        )
    return RenderResult(
        format=fmt, files=files, redaction_count=count, redaction_kinds=kinds, engine=engine
    )
