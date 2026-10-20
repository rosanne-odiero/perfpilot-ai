"""PDF engine selection and WeasyPrint glue.

WeasyPrint's native libraries do not load on every development machine, so the WeasyPrint path is
exercised with a fake module (a test double) that records how PerfPilot calls it.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from report_fixtures import AuditFactory

from backend.core.exceptions import UnavailableError
from backend.models.database import session_scope
from backend.models.enums import ReportFormat
from backend.reporting.builder import build_audit_report
from backend.reporting.renderers import pdf as pdf_module
from backend.reporting.renderers import render_report


class FakeURLFetcher:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _install_fake_weasyprint(monkeypatch: pytest.MonkeyPatch, *, modern: bool) -> list[Any]:
    calls: list[Any] = []

    class FakeHTML:
        def __init__(self, *, string: str, url_fetcher: Any) -> None:
            calls.append((string, url_fetcher))

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7 fake"

    weasyprint = types.ModuleType("weasyprint")
    weasyprint.HTML = FakeHTML  # type: ignore[attr-defined]
    weasyprint.default_url_fetcher = lambda url, *a, **k: {"string": url}  # type: ignore[attr-defined]
    urls = types.ModuleType("weasyprint.urls")
    if modern:
        urls.URLFetcher = FakeURLFetcher  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weasyprint", weasyprint)
    monkeypatch.setitem(sys.modules, "weasyprint.urls", urls)
    return calls


def test_weasyprint_gets_data_only_fetcher(
    monkeypatch: pytest.MonkeyPatch, make_audit: AuditFactory
) -> None:
    calls = _install_fake_weasyprint(monkeypatch, modern=True)
    with session_scope() as session:
        report = build_audit_report(session, make_audit())
    result = render_report(report, ReportFormat.PDF, pdf_engine="weasyprint")
    assert result.engine == "weasyprint"
    assert result.primary.data.startswith(b"%PDF")
    html, fetcher = calls[0]
    assert isinstance(fetcher, FakeURLFetcher)
    assert fetcher.kwargs["allowed_protocols"] == {"data"}
    assert fetcher.kwargs["allow_redirects"] is False
    assert "@bottom-right" in html  # WeasyPrint page stylesheet
    assert "PDF engine: weasyprint" in html


def test_legacy_weasyprint_fetcher_blocks_non_data_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_weasyprint(monkeypatch, modern=False)
    fetcher = pdf_module._weasyprint_fetcher(sys.modules["weasyprint"])
    assert fetcher("data:text/plain,ok") == {"string": "data:text/plain,ok"}
    with pytest.raises(ValueError, match="blocked"):
        fetcher("https://example.com/x.png")


def test_engine_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "available_pdf_engine", lambda: None)
    with pytest.raises(UnavailableError):
        pdf_module.resolve_engine()
    assert pdf_module.resolve_engine("xhtml2pdf") == "xhtml2pdf"
    with pytest.raises(pdf_module.ReportRenderError):
        pdf_module.html_to_pdf("<p>x</p>", "chromium")
