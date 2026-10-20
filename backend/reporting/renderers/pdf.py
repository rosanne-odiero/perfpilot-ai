"""PDF renderer: converts the report HTML (print stylesheet) with WeasyPrint or xhtml2pdf.

WeasyPrint is preferred (full CSS paged media) but needs Pango/Cairo native libraries; when it
cannot be imported, xhtml2pdf (pure Python, CSS 2.1 subset) is used. Both engines are restricted
to ``data:`` URIs, so rendering never touches the network or the filesystem.
"""

from __future__ import annotations

import importlib
import io
from functools import lru_cache
from typing import Any

from backend.core.exceptions import PerfPilotError, UnavailableError
from backend.core.logging import get_logger
from backend.reporting.model import AuditReport
from backend.reporting.renderers.html import render_html

log = get_logger(__name__)

PDF_ENGINES = ("weasyprint", "xhtml2pdf")


class ReportRenderError(PerfPilotError):
    code = "report_render_failed"


@lru_cache(maxsize=1)
def available_pdf_engine() -> str | None:
    """The first PDF engine whose Python package *and* native libraries load."""
    try:
        weasyprint = importlib.import_module("weasyprint")
        if hasattr(weasyprint, "HTML"):
            return "weasyprint"
    except (ImportError, OSError) as exc:
        log.info("weasyprint unavailable", extra={"reason": type(exc).__name__})
    try:
        importlib.import_module("xhtml2pdf.pisa")
        return "xhtml2pdf"
    except ImportError:
        return None


def _weasyprint_pdf(html: str) -> bytes:
    weasyprint: Any = importlib.import_module("weasyprint")

    def fetcher(url: str, *args: Any, **kwargs: Any) -> Any:
        if not url.startswith("data:"):
            raise ReportRenderError(f"blocked external resource in report: {url[:80]}")
        return weasyprint.default_url_fetcher(url, *args, **kwargs)

    data: bytes = weasyprint.HTML(string=html, url_fetcher=fetcher).write_pdf()
    return data


def _xhtml2pdf_pdf(html: str) -> bytes:
    pisa: Any = importlib.import_module("xhtml2pdf.pisa")

    def link_callback(uri: str, _rel: str) -> str:
        return uri if uri.startswith("data:") else ""

    buffer = io.BytesIO()
    status = pisa.CreatePDF(src=html, dest=buffer, link_callback=link_callback, encoding="utf-8")
    if status.err:
        raise ReportRenderError(f"xhtml2pdf reported {status.err} errors")
    return buffer.getvalue()


def pdf_html(report: AuditReport, engine: str) -> str:
    """The print-variant HTML for ``engine`` with the engine recorded in the report metadata."""
    if engine not in PDF_ENGINES:
        raise ReportRenderError(f"unknown PDF engine {engine!r}")
    metadata = report.metadata.model_copy(update={"pdf_engine": engine})
    return render_html(report.model_copy(update={"metadata": metadata}), pdf_engine=engine)


def html_to_pdf(html: str, engine: str) -> bytes:
    if engine == "weasyprint":
        return _weasyprint_pdf(html)
    if engine == "xhtml2pdf":
        return _xhtml2pdf_pdf(html)
    raise ReportRenderError(f"unknown PDF engine {engine!r}")


def resolve_engine(engine: str | None = None) -> str:
    chosen = engine or available_pdf_engine()
    if chosen is None:
        raise UnavailableError(
            "no PDF engine available: install WeasyPrint's native libraries (Pango) or xhtml2pdf"
        )
    return chosen
