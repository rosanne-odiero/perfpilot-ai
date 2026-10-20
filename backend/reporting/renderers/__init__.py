"""Render an ``AuditReport`` to HTML, Markdown, JSON, CSV or PDF, with secret redaction."""

from __future__ import annotations

from backend.core.redaction import redact_text
from backend.models.enums import ReportFormat
from backend.reporting.model import AuditReport
from backend.reporting.redaction import redact_html, redact_report
from backend.reporting.renderers.common import RenderedFile, RenderResult, report_filename
from backend.reporting.renderers.csv_export import (
    render_benchmark_samples_csv,
    render_findings_csv,
)
from backend.reporting.renderers.html import render_html
from backend.reporting.renderers.json_export import render_json
from backend.reporting.renderers.markdown import render_markdown
from backend.reporting.renderers.pdf import html_to_pdf, pdf_html, resolve_engine

__all__ = ["RenderResult", "RenderedFile", "render_report"]


def _merge(kinds: dict[str, int], extra: dict[str, int]) -> None:
    for kind, count in extra.items():
        kinds[kind] = kinds.get(kind, 0) + count


def render_report(
    report: AuditReport, fmt: ReportFormat, *, pdf_engine: str | None = None
) -> RenderResult:
    """Redact the report, render ``fmt`` and redact the output again (see ``redaction.py``)."""
    report, count = redact_report(report)
    kinds: dict[str, int] = {"report_content": count} if count else {}
    files: list[RenderedFile] = []
    engine: str | None = None
    name = report_filename(report, fmt.value)

    if fmt in (ReportFormat.HTML, ReportFormat.MARKDOWN, ReportFormat.PDF):
        if fmt == ReportFormat.HTML:
            text = render_html(report)
        elif fmt == ReportFormat.MARKDOWN:
            text = render_markdown(report)
        else:
            engine = resolve_engine(pdf_engine)
            text = pdf_html(report, engine)
        redacted = redact_text(text) if fmt == ReportFormat.MARKDOWN else redact_html(text)
        count += redacted.count
        _merge(kinds, redacted.kinds)
        data = (
            html_to_pdf(redacted.text, engine)
            if engine is not None
            else redacted.text.encode("utf-8")
        )
        files.append(RenderedFile("primary", name, fmt.media_type, data))
    elif fmt == ReportFormat.JSON:
        text, extra = render_json(report)
        count += extra
        files.append(RenderedFile("primary", name, fmt.media_type, text.encode("utf-8")))
    elif fmt == ReportFormat.CSV:
        text, extra = render_findings_csv(report)
        count += extra
        files.append(
            RenderedFile(
                "primary",
                report_filename(report, "csv", "findings"),
                fmt.media_type,
                text.encode("utf-8"),
            )
        )
        samples = render_benchmark_samples_csv(report)
        if samples is not None:
            sample_text, extra = samples
            count += extra
            files.append(
                RenderedFile(
                    "benchmark_samples",
                    report_filename(report, "csv", "benchmark-samples"),
                    fmt.media_type,
                    sample_text.encode("utf-8"),
                )
            )
    return RenderResult(
        format=fmt, files=files, redaction_count=count, redaction_kinds=kinds, engine=engine
    )
