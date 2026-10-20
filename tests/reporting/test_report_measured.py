"""Measured sections (7, 11, 12) supplied by a report provider.

``FakeRuntimeAuditProvider`` below is a TEST DOUBLE standing in for the future runtime-audit
provider. Its numbers are fixed test inputs, used only to check that measured data is rendered
with the MEASURED label; they are not results of any run.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator

import pytest
from report_fixtures import MEASUREMENT_LIKE, AuditFactory, visible_text
from sqlalchemy.orm import Session

from backend.models.database import session_scope
from backend.models.enums import Provenance, ReportFormat
from backend.reporting.builder import build_audit_report
from backend.reporting.model import (
    NOT_MEASURED_TEXT,
    AuditReport,
    BenchmarkResult,
    BenchmarkSample,
    RuntimeHotspot,
    RuntimeProfile,
    ValidationCheck,
    ValidationOutcome,
)
from backend.reporting.provider_registry import (
    ProviderContext,
    ProviderContribution,
    register_provider,
    registered_providers,
    unregister_provider,
)
from backend.reporting.renderers import render_report

TEST_PROVIDER = "zz-test-double-runtime"


def fake_runtime_provider(_: Session, context: ProviderContext) -> ProviderContribution:
    hot = next(f for f in context.findings if f.id == "F-001")
    return ProviderContribution(
        runtime_profiles=[
            RuntimeProfile(
                profile_id="prof-test-1",
                command=["python", "-m", "pytest", "tests/test_recommendations.py"],
                profiler="cProfile + tracemalloc",
                wall_time_ms=4321.5,
                cpu_time_ms=4100.25,
                peak_rss_mb=187.4,
                hotspots=[
                    RuntimeHotspot(
                        function_name="recommend_for_user",
                        file_path=hot.location.file_path,
                        line=hot.location.line_start,
                        internal_time_ms=1234.56,
                        cumulative_time_ms=2345.67,
                        call_count=812,
                        percentage_of_profiled_time=54.3,
                        finding_ids=[hot.id],
                    )
                ],
                environment={"python": "3.12.10", "image": "perfpilot-sandbox:test"},
            )
        ],
        validation_outcomes=[
            ValidationOutcome(
                validation_id="val-test-1",
                recommendation_id="R-001",
                status="PASSED",
                command=["python", "-m", "pytest", "-q"],
                checks=[ValidationCheck(name="unit tests", status="PASSED", detail="48 passed")],
            )
        ],
        benchmark_results=[
            BenchmarkResult(
                benchmark_id="bench-test-1",
                name="recommend_for_user (10k candidates)",
                recommendation_id="R-001",
                baseline_median_ms=200.0,
                candidate_median_ms=125.0,
                speedup=1.6,
                speedup_ci_low=1.52,
                speedup_ci_high=1.68,
                confidence_level=0.95,
                classification="IMPROVED",
                sample_count=3,
                warmup_runs=1,
                environment={"cpu": "test-cpu"},
                samples=[
                    BenchmarkSample(variant=v, run_index=i, wall_time_ms=t)
                    for v, times in (
                        ("baseline", (201.0, 200.0, 199.5)),
                        ("candidate", (125.5, 125.0, 124.0)),
                    )
                    for i, t in enumerate(times)
                ],
            )
        ],
        limitations=["Runtime data comes from a single approved command."],
    )


@pytest.fixture
def measured_provider() -> Iterator[None]:
    register_provider(TEST_PROVIDER, fake_runtime_provider)
    yield
    unregister_provider(TEST_PROVIDER)


def _report(audit_id: str) -> AuditReport:
    with session_scope() as session:
        return build_audit_report(session, audit_id)


def test_only_the_runtime_audit_provider_ships() -> None:
    assert [name for name, _ in registered_providers()] == ["runtime-audit"]


@pytest.mark.usefixtures("measured_provider")
def test_provider_fills_measured_sections(make_audit: AuditFactory) -> None:
    report = _report(make_audit())
    assert report.metadata.providers_applied == [TEST_PROVIDER]
    assert report.runtime is not None
    assert report.runtime.provenance == Provenance.MEASURED
    assert report.validation is not None
    assert report.validation.outcomes[0].status == "PASSED"
    assert report.benchmarks is not None
    assert report.benchmarks.chart_svg is not None
    assert not report.is_static_only
    assert "Runtime data comes from a single approved command." in report.limitations.limitations
    assert "measured data" in " ".join(report.executive_summary.text)


def test_measured_sections_reject_non_measured_provenance() -> None:
    with pytest.raises(ValueError, match="MEASURED"):
        RuntimeProfile.model_validate(
            {
                "provenance": "STATIC",
                "profile_id": "p",
                "command": ["x"],
                "profiler": "cProfile",
                "wall_time_ms": 1.0,
            }
        )


@pytest.mark.usefixtures("measured_provider")
def test_measured_numbers_render_with_measured_labels(make_audit: AuditFactory) -> None:
    report = _report(make_audit())

    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    assert NOT_MEASURED_TEXT not in page
    for number in (
        "4,321.5 ms",
        "187.4 MB",
        "2,345.67 ms",
        "200.00 ms",
        "125.00 ms",
        "1.60\u00d7",
        "1.52\u20131.68\u00d7",
    ):
        assert number in page
    assert len(MEASUREMENT_LIKE.findall(visible_text(page))) >= 10  # the static check detects these
    badges = re.findall(r'class="badge prov prov-measured"', page)
    assert len(badges) >= 4  # runtime profile, validation, benchmark table + detail card
    runtime_section = page.split('id="s7"')[1].split('id="s8"')[0]
    assert 'class="badge prov prov-measured"' in runtime_section
    assert "PASSED" in page.split('id="s11"')[1].split('id="s12"')[0]

    markdown = render_report(report, ReportFormat.MARKDOWN).primary.data.decode()
    assert "**[MEASURED]** Profile `prof-test-1`" in markdown
    assert "| 200.00 ms | 125.00 ms | 1.60x |" in markdown
    assert NOT_MEASURED_TEXT not in markdown

    data = json.loads(render_report(report, ReportFormat.JSON).primary.data)
    assert data["runtime"]["provenance"] == "MEASURED"
    assert data["benchmarks"]["results"][0]["speedup"] == 1.6


@pytest.mark.usefixtures("measured_provider")
def test_benchmark_samples_csv_is_separate(make_audit: AuditFactory) -> None:
    result = render_report(_report(make_audit()), ReportFormat.CSV)
    assert [f.role for f in result.files] == ["primary", "benchmark_samples"]
    samples = list(csv.DictReader(io.StringIO(result.files[1].data.decode())))
    assert len(samples) == 6
    assert {s["variant"] for s in samples} == {"baseline", "candidate"}
    assert all(s["provenance"] == "MEASURED" for s in samples)
    assert result.files[1].filename.endswith("-benchmark-samples.csv")


def test_providers_are_applied_in_name_order(make_audit: AuditFactory) -> None:
    calls: list[str] = []

    def make(name: str):  # type: ignore[no-untyped-def]
        def provider(_: Session, __: ProviderContext) -> ProviderContribution | None:
            calls.append(name)
            return None

        return provider

    register_provider("b-test", make("b"))
    register_provider("a-test", make("a"))
    try:
        report = _report(make_audit())
    finally:
        unregister_provider("a-test")
        unregister_provider("b-test")
    assert calls == ["a", "b"]
    assert report.metadata.providers_applied == []  # empty contributions are not credited
    assert report.runtime is None
