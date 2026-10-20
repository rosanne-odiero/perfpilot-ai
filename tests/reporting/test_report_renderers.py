from __future__ import annotations

import csv
import html
import io
import json
import time
from typing import Any

import pytest
from pypdf import PdfReader
from report_fixtures import (
    COMMIT_SHA,
    FAKE_CREDENTIALS_SECRET,
    FAKE_DB_PASSWORD,
    FAKE_GITHUB_TOKEN,
    MEASUREMENT_LIKE,
    AuditFactory,
    build_findings,
    visible_text,
)

from backend.models.audit_schemas import CodeLocation, Finding
from backend.models.database import session_scope
from backend.models.enums import Confidence, FindingCategory, ReportFormat, Severity
from backend.reporting.builder import build_audit_report
from backend.reporting.model import NOT_MEASURED_TEXT, SCHEMA_VERSION, SECTION_TITLES, AuditReport
from backend.reporting.redaction import redact_html
from backend.reporting.renderers import render_report
from backend.reporting.renderers.csv_export import FINDING_COLUMNS
from backend.reporting.renderers.html import render_html
from backend.reporting.renderers.markdown import md, render_markdown
from backend.reporting.renderers.pdf import available_pdf_engine

needs_pdf_engine = pytest.mark.skipif(
    available_pdf_engine() is None, reason="no PDF engine (WeasyPrint or xhtml2pdf) available"
)

MEASURED_BADGE_HTML = 'class="badge prov prov-measured"'
MEASURED_BADGE_MD = "[MEASURED]"
SECRETS = (FAKE_GITHUB_TOKEN, FAKE_DB_PASSWORD, FAKE_CREDENTIALS_SECRET)


def _report(audit_id: str) -> AuditReport:
    with session_scope() as session:
        return build_audit_report(session, audit_id)


def provenance_values(data: Any) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "provenance" and isinstance(value, str):
                found.append(value)
            found.extend(provenance_values(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(provenance_values(item))
    return found


def _text(result_data: bytes) -> str:
    return result_data.decode("utf-8")


def test_every_section_is_present_in_html_and_markdown(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    page = render_html(report)
    markdown = render_markdown(report)
    for number, title in SECTION_TITLES.items():
        assert f'id="s{number}"' in page
        assert html.escape(title) in page
        assert f"## {number}. {title}" in markdown
        assert f'href="#s{number}"' in page  # table of contents
    for legend in ("MEASURED", "STATIC", "AI_HYPOTHESIS"):
        assert f"legend-{legend.lower()}" in page
        assert f"`{legend}`" in markdown
    assert "<svg" in page
    assert "Module dependency graph" in page
    assert "```mermaid" in markdown


def test_static_only_report_has_no_measurements_anywhere(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    outputs = {fmt: render_report(report, fmt) for fmt in ReportFormat if fmt != ReportFormat.PDF}

    page = _text(outputs[ReportFormat.HTML].primary.data)
    assert MEASURED_BADGE_HTML not in page
    assert page.count(NOT_MEASURED_TEXT) == 3  # sections 7, 11 and 12
    assert not MEASUREMENT_LIKE.findall(visible_text(page))

    markdown = _text(outputs[ReportFormat.MARKDOWN].primary.data)
    assert MEASURED_BADGE_MD not in markdown
    assert markdown.count(NOT_MEASURED_TEXT) == 3
    assert not MEASUREMENT_LIKE.findall(markdown)

    data = json.loads(outputs[ReportFormat.JSON].primary.data)
    assert "MEASURED" not in provenance_values(data)
    assert data["runtime"] is None
    assert data["validation"] is None
    assert data["benchmarks"] is None
    assert all(f["measured"] is None for f in data["findings"]["entries"])
    assert not MEASUREMENT_LIKE.findall(json.dumps(data["executive_summary"]))

    rows = list(csv.DictReader(io.StringIO(_text(outputs[ReportFormat.CSV].primary.data))))
    assert all(row["provenance"] != "MEASURED" for row in rows)
    assert all(row["measured_cumulative_time_ms"] == "" for row in rows)
    assert len(outputs[ReportFormat.CSV].files) == 1  # no benchmark samples file


def test_html_escapes_repository_content(make_audit: AuditFactory) -> None:
    page = _text(render_report(_report(make_audit()), ReportFormat.HTML).primary.data)
    assert "<script" not in page.lower()
    assert "&lt;script&gt;alert(" in page
    assert "onerror=" not in page


def test_markdown_neutralizes_injection() -> None:
    assert md("<img src=x onerror=alert(1)>") == "&lt;img src=x onerror=alert(1)&gt;"
    assert md("[click](javascript:alert(1))").startswith("\\[click\\]")
    assert md("# not a heading") == "\\# not a heading"
    assert md("- not a list") == "\\- not a list"
    assert md("a | b\nc") == "a \\| b c"


def test_hostile_finding_text_is_escaped_in_markdown(make_audit: AuditFactory) -> None:
    hostile = build_findings()[0].model_copy(
        update={"title": "<img src=x onerror=alert(1)> [x](javascript:alert(1))"}
    )
    report = _report(make_audit(findings=[hostile], recommendations=[]))
    markdown = render_markdown(report)
    assert "<img" not in markdown
    assert "[x](javascript:" not in markdown  # brackets escaped -> no link is formed
    assert "\\[x\\](javascript:" in markdown


def test_secrets_are_redacted_in_every_format(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    formats = [f for f in ReportFormat if f != ReportFormat.PDF]
    for fmt in formats:
        result = render_report(report, fmt)
        blob = b"".join(f.data for f in result.files).decode("utf-8")
        for secret in SECRETS:
            assert secret not in blob, f"secret leaked into {fmt}"
        assert result.redaction_count >= 2
        if fmt != ReportFormat.CSV:  # the secret-bearing snippet is not a CSV column
            assert "[REDACTED]" in blob
    page = _text(render_report(report, ReportFormat.HTML).primary.data)
    assert "postgresql://[REDACTED]@db/shop" in visible_text(page)


def test_html_output_pass_redacts_text_nodes_without_breaking_markup() -> None:
    token = "ghp_" + "Ab1" * 12
    document = (
        "<style>.a > .b { color: red; }</style>"
        f"<td>credentials.json:1-2</td><td>key = &#34;{token}&#34;</td>"
        '<td>password = &#34;Sup3r$ecretValue&#34;</td><a href="https://x/y">ok</a>'
    )
    result = redact_html(document)
    assert token not in result.text
    assert "Sup3r$ecretValue" not in result.text
    assert result.count == 2
    assert "<td>credentials.json:1-2</td>" in result.text  # no markup swallowed
    assert '<a href="https://x/y">ok</a>' in result.text


def test_csv_parses_back_and_guards_formulas(make_audit: AuditFactory) -> None:
    findings = build_findings()
    findings[0] = findings[0].model_copy(update={"title": '=HYPERLINK("http://evil","x")'})
    report = _report(make_audit(findings=findings))
    result = render_report(report, ReportFormat.CSV)
    assert result.primary.filename == f"perfpilot-acme-shopfront-{COMMIT_SHA[:7]}-findings.csv"
    reader = csv.DictReader(io.StringIO(_text(result.primary.data)))
    assert reader.fieldnames == FINDING_COLUMNS
    rows = list(reader)
    assert len(rows) == len(findings)
    by_id = {f.id: f for f in findings}
    for row in rows:
        loc = by_id[row["id"]].location
        assert row["file_path"] == loc.file_path
        assert int(row["line_start"]) == loc.line_start
    hostile = next(r for r in rows if r["id"] == "F-001")
    assert hostile["title"].startswith("'=HYPERLINK")


def test_json_round_trips_through_the_model(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    result = render_report(report, ReportFormat.JSON)
    data = json.loads(result.primary.data)
    assert data["schema_version"] == SCHEMA_VERSION
    parsed = AuditReport.model_validate(data)
    assert parsed.findings.total == report.findings.total
    assert parsed.metadata.redactions_applied >= 2
    assert result.primary.media_type == "application/json"


def test_file_line_references_in_outputs_match_findings(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    page = _text(render_report(report, ReportFormat.HTML).primary.data)
    markdown = _text(render_report(report, ReportFormat.MARKDOWN).primary.data)
    for f in build_findings():
        loc = f.location
        ref = (
            f"{loc.file_path}:{loc.line_start}"
            if loc.line_end == loc.line_start
            else f"{loc.file_path}:{loc.line_start}-{loc.line_end}"
        )
        assert ref in page
        assert f"`{ref}`" in markdown
    assert (
        f'href="https://github.com/acme/shopfront/blob/{COMMIT_SHA}/shopfront/api/orders.py'
        '#L88-L91"' in page
    )


@needs_pdf_engine
def test_pdf_is_generated_from_the_same_report(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    result = render_report(report, ReportFormat.PDF)
    data = result.primary.data
    assert data.startswith(b"%PDF")
    assert len(data) > 20_000
    assert result.engine in {"weasyprint", "xhtml2pdf"}
    assert result.primary.filename.endswith("-audit.pdf")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 5
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Executive summary" in text
    assert "Analysis limitations" in text
    assert f"PDF engine: {result.engine}" in " ".join(text.split())
    for secret in SECRETS:
        assert secret not in text
    assert not MEASUREMENT_LIKE.findall(text.replace(NOT_MEASURED_TEXT, ""))


def test_large_report_renders_quickly(make_audit: AuditFactory) -> None:
    """Performance smoke test (non-authoritative): 500 findings render well under the limit."""
    base = build_findings()[0]
    findings = [
        Finding(
            **{
                **base.model_dump(),
                "id": f"F-{i:04d}",
                "title": f"Synthetic finding {i}",
                "severity": list(Severity)[i % 5],
                "confidence": Confidence.MEDIUM,
                "category": list(FindingCategory)[i % len(FindingCategory)],
                "location": CodeLocation(
                    file_path=f"pkg/module_{i % 40}.py", line_start=10 + i, line_end=13 + i
                ),
                "priority_score": round(1 - i / 1000, 4),
            }
        )
        for i in range(500)
    ]
    audit_id = make_audit(findings=findings)
    started = time.perf_counter()
    report = _report(audit_id)
    result = render_report(report, ReportFormat.HTML)
    elapsed = time.perf_counter() - started
    assert report.findings.total == 500
    assert result.primary.data.count(b'class="card finding"') == 500
    assert elapsed < 20.0, f"500-finding HTML report took {elapsed:.1f}s"


def test_pdf_break_splits_long_tokens_safely() -> None:
    from backend.reporting.renderers.html import pdf_break

    path = "pkg/very_long_directory_name/another_long_directory/module_name.py:120-140"
    broken = str(pdf_break(path, 30))
    assert broken.replace("<br/>", "") == path
    assert all(len(part) <= 30 for part in broken.split("<br/>"))
    assert str(pdf_break("<b>" + "x" * 60, 20)).count("&lt;b&gt;") == 1
    assert str(pdf_break("short words stay intact")) == "short words stay intact"
