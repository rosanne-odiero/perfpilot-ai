"""Experiment report renderers, redaction, chart and provenance rules.

The fixture is a real duplicate_counter experiment (profile -> validated Counter patch -> 5-run
A/B benchmark) captured with ``build_optimization_report``; tests derive variants from it.
"""

from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pypdf import PdfReader
from report_fixtures import FAKE_GITHUB_TOKEN

from backend.models.enums import Provenance, ReportFormat
from backend.models.orm.optimization import OptimizationRecommendation
from backend.reporting.experiment_builder import _hypothesis_label, _limitations
from backend.reporting.experiment_charts import samples_box_strip_svg, uses_log_scale
from backend.reporting.experiment_model import (
    EXPERIMENT_SCHEMA_VERSION,
    ExperimentReport,
    SampleRow,
)
from backend.reporting.renderers.experiment import (
    CHECKS_ROLE,
    render_experiment_report,
)
from backend.reporting.renderers.pdf import available_pdf_engine

FIXTURE = Path(__file__).parent / "fixtures" / "experiment_report.json"


@pytest.fixture
def report() -> ExperimentReport:
    return ExperimentReport.model_validate(json.loads(FIXTURE.read_text()))


def _text(report: ExperimentReport, fmt: ReportFormat) -> str:
    return render_experiment_report(report, fmt).primary.data.decode("utf-8")


def _failed_variant(report: ExperimentReport) -> ExperimentReport:
    assert report.validation is not None
    checks = [
        c.model_copy(update={"status": "FAILED", "failures": ["candidate: test_key_order"]})
        if c.name == "functional_tests"
        else c
        for c in report.validation.checks
    ]
    validation = report.validation.model_copy(
        update={"status": "FAILED", "checks": checks, "summary": "FAILED: functional_tests"}
    )
    return report.model_copy(
        update={
            "validation": validation,
            "benchmark": None,
            "benchmark_absent_reason": "No benchmark ran: correctness validation FAILED.",
        }
    )


def test_json_is_the_model_dump_with_schema_version(report: ExperimentReport) -> None:
    data = json.loads(_text(report, ReportFormat.JSON))
    assert data["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    assert ExperimentReport.model_validate(data).benchmark == report.benchmark
    assert data["benchmark"]["provenance"] == "MEASURED"
    assert data["validation"]["provenance"] == "MEASURED"


def test_markdown_is_pr_ready(report: ExperimentReport) -> None:
    text = _text(report, ReportFormat.MARKDOWN)
    assert text.startswith("# Optimization experiment: Duplicate counter")
    assert "### Measured vs hypothesis" in text
    assert "```diff\n--- a/workload.py" in text
    assert "> **Verdict:** Confirmed by measurement" in text
    assert "Small sample:" in text
    assert "scripts/optimize\\_workload.py" not in text  # commands stay in code fences
    assert "scripts/optimize_workload.py duplicate_counter --approve" in text
    # Hypotheses are labeled, never MEASURED.
    hypothesis_rows = text.split("| Hypothesis (not measured) |")[1].split("> **Verdict")[0]
    assert "[MEASURED]" not in hypothesis_rows
    assert "[STATIC]" in hypothesis_rows


def test_html_has_inline_box_strip_plot_and_no_network(report: ExperimentReport) -> None:
    page = _text(report, ReportFormat.HTML)
    assert page.count("<svg") == 1
    assert "Raw wall-time samples" in page
    assert 'id="measured-vs-hypothesis"' in page
    assert "<script" not in page
    for marker in ("http://", "https://"):
        assert marker not in page.replace('xmlns="http://www.w3.org/2000/svg"', "")


def test_csv_exports_raw_samples_and_validation_checks(report: ExperimentReport) -> None:
    result = render_experiment_report(report, ReportFormat.CSV)
    assert [f.role for f in result.files] == ["primary", CHECKS_ROLE]
    samples = list(csv.DictReader(io.StringIO(result.files[0].data.decode())))
    assert report.benchmark is not None
    assert len(samples) == len(report.benchmark.samples)
    timing = [r for r in samples if r["kind"] == "timing"]
    assert {r["variant"] for r in timing} == {"baseline", "candidate"}
    assert all(float(r["wall_ms"]) > 0 for r in timing)
    checks = list(csv.DictReader(io.StringIO(result.files[1].data.decode())))
    assert report.validation is not None
    assert [r["check"] for r in checks] == [c.name for c in report.validation.checks]


def test_failed_validation_reports_why_and_has_no_benchmark_numbers(
    report: ExperimentReport,
) -> None:
    failed = _failed_variant(report)
    markdown = _text(failed, ReportFormat.MARKDOWN)
    assert "**FAILED**" in markdown
    assert "`candidate: test_key_order`" in markdown
    assert "No benchmark ran" in markdown
    assert "Per-variant statistics" not in markdown
    assert "Median speedup" not in markdown
    page = _text(failed, ReportFormat.HTML)
    assert "No benchmark ran" in page
    assert "<svg" not in page
    samples = render_experiment_report(failed, ReportFormat.CSV).files[0].data.decode()
    assert samples.strip().count("\n") == 0  # header only: nothing was measured


def test_stored_text_is_escaped(report: ExperimentReport) -> None:
    assert report.patch is not None
    assert report.recommendation is not None
    hostile = "<script>alert(1)</script> | **bold** [x](javascript:alert(1))"
    patch = report.patch.model_copy(
        update={"summary": hostile, "unified_diff": report.patch.unified_diff + f"+{hostile}\n"}
    )
    tampered = report.model_copy(update={"patch": patch})
    page = _text(tampered, ReportFormat.HTML)
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    markdown = _text(tampered, ReportFormat.MARKDOWN)
    assert "<script>" not in markdown.split("```diff")[0]
    assert "\\*\\*bold\\*\\*" in markdown
    assert "\\[x\\]" in markdown


def test_secrets_are_redacted_in_every_format(report: ExperimentReport) -> None:
    assert report.patch is not None
    patch = report.patch.model_copy(update={"notes": [f"token={FAKE_GITHUB_TOKEN}"]})
    leaky = report.model_copy(update={"patch": patch})
    for fmt in (ReportFormat.HTML, ReportFormat.MARKDOWN, ReportFormat.JSON, ReportFormat.CSV):
        result = render_experiment_report(leaky, fmt)
        for rendered in result.files:
            assert FAKE_GITHUB_TOKEN.encode() not in rendered.data
        assert result.redaction_count >= (1 if fmt != ReportFormat.CSV else 0)
    data = json.loads(_text(leaky, ReportFormat.JSON))
    assert data["metadata"]["redactions_applied"] >= 1


def test_csv_cells_cannot_inject_formulas(report: ExperimentReport) -> None:
    assert report.benchmark is not None
    sample = report.benchmark.samples[0].model_copy(update={"error": "=HYPERLINK(1)"})
    bench = report.benchmark.model_copy(update={"samples": [sample]})
    data = render_experiment_report(
        report.model_copy(update={"benchmark": bench}), ReportFormat.CSV
    )
    row = next(csv.DictReader(io.StringIO(data.files[0].data.decode())))
    assert row["error"] == "'=HYPERLINK(1)"


@pytest.mark.skipif(available_pdf_engine() is None, reason="no PDF engine installed")
def test_pdf_uses_the_print_variant(report: ExperimentReport) -> None:
    result = render_experiment_report(report, ReportFormat.PDF)
    assert result.engine in {"weasyprint", "xhtml2pdf"}
    assert result.primary.data.startswith(b"%PDF")
    text = " ".join(
        p.extract_text() or "" for p in PdfReader(io.BytesIO(result.primary.data)).pages
    )
    assert "Measured vs hypothesis" in text
    assert "Correctness validation" in text


def _samples(baseline: list[float], candidate: list[float]) -> list[SampleRow]:
    rows = []
    for variant, values in (("baseline", baseline), ("candidate", candidate)):
        rows += [
            SampleRow(
                variant=variant,
                kind="timing",
                pair_index=i,
                position_in_pair=0,
                wall_ms=v,
                cpu_ms=v,
                tracemalloc_peak_bytes=None,
                rss_peak_bytes=None,
                ru_maxrss_bytes=None,
                ok=True,
            )
            for i, v in enumerate(values)
        ]
    return rows


def test_box_strip_plot_scales_and_is_valid_svg() -> None:
    close = _samples([10.0, 11.0, 12.5], [9.0, 9.5, 10.2])
    far = _samples([150.0, 160.0, 171.0], [0.4, 0.43, 0.6])
    assert not uses_log_scale({"a": [9.0, 12.5]})
    for rows, scale in ((close, "linear scale"), (far, "log scale")):
        svg = samples_box_strip_svg(rows)
        assert svg is not None
        assert scale in svg
        root = ET.fromstring(svg)
        circles = root.findall(".//{http://www.w3.org/2000/svg}circle")
        assert len(circles) == 6
    memory_only = [s.model_copy(update={"kind": "memory", "wall_ms": None}) for s in close]
    assert samples_box_strip_svg(memory_only) is None


def test_hypotheses_are_never_labeled_measured() -> None:
    # Expected effects are predictions: rule templates record them as AI_HYPOTHESIS too, and the
    # report uses the same label as the recommendations API.
    deterministic = OptimizationRecommendation(
        generated_by="deterministic", provenance={"expected_effect": "AI_HYPOTHESIS"}
    )
    assert _hypothesis_label(deterministic, "expected_effect") == Provenance.AI_HYPOTHESIS
    unrecorded = OptimizationRecommendation(generated_by="deterministic", provenance={})
    assert _hypothesis_label(unrecorded, "expected_effect") == Provenance.AI_HYPOTHESIS
    model = OptimizationRecommendation(
        generated_by="some-model", provenance={"expected_effect": "MEASURED"}
    )
    assert _hypothesis_label(model, "expected_effect") == Provenance.AI_HYPOTHESIS
    unlabeled = OptimizationRecommendation(generated_by="some-model", provenance={})
    assert _hypothesis_label(unlabeled, "estimated_impact") == Provenance.AI_HYPOTHESIS


def test_small_sample_limitation(report: ExperimentReport) -> None:
    assert report.benchmark is not None
    assert any("timed runs per variant" in item for item in _limitations(report.benchmark))
    big = report.benchmark.model_copy(
        update={"settings": report.benchmark.settings.model_copy(update={"runs": 50})}
    )
    assert not any("timed runs per variant" in item for item in _limitations(big))
