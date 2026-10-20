"""Section 7 of the audit report from real runtime runs (runtime-audit report provider).

Runs use the ``SubprocessHarnessBackend`` test double (real harness output on ShopFast).
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi.testclient import TestClient
from runtime_support import (
    WORKLOAD_ARGV,
    BackendSetter,
    SubprocessHarnessBackend,
    UnavailableBackend,
    approve,
    command_by_argv,
    drain,
    shopfast_snapshot,
    start_runtime_audit,
)

from backend.core.config import Settings
from backend.models.database import session_scope
from backend.models.enums import ReportFormat
from backend.reporting.builder import build_audit_report
from backend.reporting.model import NOT_MEASURED_TEXT, AuditReport
from backend.reporting.renderers import render_report

MEASURED_BADGE = 'class="badge prov prov-measured"'
STATIC_BADGE = 'class="badge prov prov-static"'
MEASUREMENT_LIKE = re.compile(r"\d[\d,]*(?:\.\d+)?[ \u00a0]?(?:(?:ms|s|MB|KB|GB)\b|%)")


def _report(audit_id: str) -> AuditReport:
    with session_scope() as session:
        return build_audit_report(session, audit_id)


def _section(page: str, number: int) -> str:
    return page.split(f'id="s{number}"')[1].split(f'id="s{number + 1}"')[0]


def _measured_audit(client: TestClient, settings: Settings) -> tuple[str, str]:
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    accepted = approve(client, audit_id, command_by_argv(client, audit_id, WORKLOAD_ARGV))
    drain(settings)
    return audit_id, str(accepted["subject_id"])


def test_measured_run_fills_section_7_and_marks_findings(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    audit_id, run_id = _measured_audit(client, settings)
    report = _report(audit_id)

    assert report.metadata.providers_applied == ["runtime-audit"]
    assert report.runtime is not None
    [profile] = report.runtime.profiles
    assert profile.provenance == "MEASURED"
    assert profile.profile_id == run_id
    assert profile.command == WORKLOAD_ARGV
    assert profile.profiler == "cProfile + tracemalloc (concurrent)"
    assert profile.wall_time_ms > 0
    assert profile.peak_rss_mb is not None
    assert profile.peak_traced_memory_mb is not None
    assert profile.peak_rss_mb > profile.peak_traced_memory_mb > 0
    first = next(h for h in profile.hotspots if (h.file_path or "").startswith("shopfast/"))
    assert (first.function_name, first.file_path, first.line) == (
        "reconcile",
        "shopfast/inventory_sync.py",
        8,
    )
    nested = next(
        f
        for f in report.findings.entries
        if (f.rule_id, f.file_path, f.line_start)
        == ("PP-LOOP-002", "shopfast/inventory_sync.py", 11)
    )
    assert nested.id in first.finding_ids
    assert nested.provenance == "STATIC"
    assert nested.measured is not None
    assert nested.measured.provenance == "MEASURED"
    assert report.findings.entries[0].id == nested.id
    env = profile.environment
    assert env["network"] == "none"
    assert env["memory_mb"] == str(settings.sandbox_memory_mb)
    assert env["approved_by"] == "tester"
    assert env["status"] == "SUCCEEDED"
    notes = " ".join(profile.notes)
    assert "Peak RSS (resident memory)" in notes
    assert "Peak traced memory (tracemalloc)" in notes
    assert "measured_factor = 1 + 2 x attributed share" in notes
    assert "Runtime-only hotspots" in notes
    assert any("cover only the approved command" in x for x in report.limitations.limitations)

    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    runtime_section = _section(page, 7)
    assert MEASURED_BADGE in runtime_section
    assert NOT_MEASURED_TEXT not in runtime_section
    assert "python benchmarks/run_workload.py" in runtime_section
    assert "peak RSS" in runtime_section
    assert "peak traced memory" in runtime_section
    card = page.split('class="card finding" id="f-1"')[1].split('class="card finding"')[0]
    assert STATIC_BADGE in card
    assert 'class="measured-evidence"' in card
    assert MEASURED_BADGE in card

    markdown = render_report(report, ReportFormat.MARKDOWN).primary.data.decode()
    assert f"**[MEASURED]** Profile `{run_id}`" in markdown
    assert "**[MEASURED]** `reconcile`" in markdown

    data: dict[str, Any] = json.loads(render_report(report, ReportFormat.JSON).primary.data)
    assert data["runtime"]["provenance"] == "MEASURED"
    entry = data["findings"]["entries"][0]
    assert entry["provenance"] == "STATIC"
    assert entry["measured"]["provenance"] == "MEASURED"
    assert entry["measured"]["measured_factor"] > 1


def test_failed_run_only_adds_a_limitation(
    client: TestClient, settings: Settings, harness_backend: SubprocessHarnessBackend
) -> None:
    snapshot = shopfast_snapshot(
        settings, extra_files={"tests/test_zz_fail.py": "def test_x():\n    assert False\n"}
    )
    audit_id = start_runtime_audit(client, settings, snapshot)
    approve(client, audit_id, command_by_argv(client, audit_id, ["python", "-m", "pytest", "-q"]))
    drain(settings)
    report = _report(audit_id)

    assert report.runtime is None
    assert report.is_static_only
    assert report.metadata.providers_applied == ["runtime-audit"]
    assert any(
        "produced no measurements (FAILED)" in x and "test failures" in x
        for x in report.limitations.limitations
    )
    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    assert NOT_MEASURED_TEXT in _section(page, 7)
    assert MEASURED_BADGE not in page


def test_runtime_audit_without_sandbox_has_no_measured_numbers(
    client: TestClient, settings: Settings, use_backend: BackendSetter
) -> None:
    use_backend(UnavailableBackend())
    audit_id = start_runtime_audit(client, settings, shopfast_snapshot(settings))
    report = _report(audit_id)

    assert report.runtime is None
    assert report.is_static_only
    assert report.metadata.providers_applied == []
    assert any("Runtime profiling is unavailable" in x for x in report.limitations.limitations)
    page = render_report(report, ReportFormat.HTML).primary.data.decode()
    assert MEASURED_BADGE not in page
    assert NOT_MEASURED_TEXT in _section(page, 7)
    assert not MEASUREMENT_LIKE.findall(_section(page, 7))
    data = json.loads(render_report(report, ReportFormat.JSON).primary.data)
    assert "MEASURED" not in json.dumps(data["findings"])
    assert data["runtime"] is None
