"""End-to-end static audit of the ShopFast fixture repository (no database)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from audit_support import SHOPFAST, scan_inventory

from backend.audit.static_audit import StaticAuditResult, run_static_audit
from backend.core.config import Settings
from backend.indexing.source import decode_source
from backend.models.enums import AuditStage, Provenance


@pytest.fixture(scope="module")
def result() -> StaticAuditResult:
    return run_static_audit(SHOPFAST, scan_inventory(SHOPFAST), Settings())


def _expected() -> set[tuple[str, str, int]]:
    data = json.loads((SHOPFAST / "EXPECTED_FINDINGS.json").read_text())
    return {(f["rule_id"], f["file_path"], f["line_start"]) for f in data["findings"]}


def test_finds_exactly_the_expected_findings(result: StaticAuditResult) -> None:
    found = {(f.rule_id, f.location.file_path, f.location.line_start) for f in result.findings}
    assert found == _expected()


def test_every_snippet_is_real_source(result: StaticAuditResult) -> None:
    for finding in result.findings:
        text, _ = decode_source((SHOPFAST / finding.location.file_path).read_bytes())
        lines = text.split("\n")
        snippet_lines = finding.snippet.split("\n")
        assert 1 <= len(snippet_lines) <= 12
        start = next(
            i
            for i in range(len(lines))
            if lines[i : i + len(snippet_lines)] == snippet_lines
            and i + 1 <= finding.location.line_start <= i + len(snippet_lines)
        )
        offending = lines[finding.location.line_start - 1]
        assert offending in snippet_lines, finding.rule_id
        assert start + 1 <= finding.location.line_start
        assert finding.location.line_end <= len(lines)


def test_findings_are_static_prioritized_and_explained(result: StaticAuditResult) -> None:
    scores = [f.priority_score for f in result.findings]
    assert scores == sorted(scores, reverse=True)
    assert len({f.id for f in result.findings}) == len(result.findings)
    for finding in result.findings:
        assert finding.provenance is Provenance.STATIC
        assert finding.measured is None
        assert finding.suggestion
        assert finding.tradeoffs
        assert finding.description
        assert set(finding.priority_factors) >= {
            "severity_weight",
            "confidence_weight",
            "entrypoint_reachability",
            "path_role",
            "path_loop",
            "loop_factor",
            "fan_in_factor",
            "path_criticality",
        }
        if finding.complexity_note:
            assert finding.complexity_note.startswith("estimate")


def test_route_handler_findings_rank_as_entrypoints(result: StaticAuditResult) -> None:
    blocking = next(f for f in result.findings if f.location.symbol == "get_product")
    assert blocking.rule_id == "PP-ASYNC-001"
    assert blocking.priority_factors["entrypoint_reachability"] == 1.0
    assert blocking.priority_factors["path_role"] == 1.0
    assert blocking.code_role is not None
    assert blocking.execution_path == ["shopfast.api:get_product"]


def test_index_and_stats(result: StaticAuditResult) -> None:
    stats = result.stats
    assert stats.parse_errors == 0
    assert stats.files_analyzed == stats.modules == len(result.index.modules)
    assert stats.findings_total == len(result.findings) == sum(stats.by_rule.values())
    assert set(stats.stage_seconds) == {
        AuditStage.INDEXING.value,
        AuditStage.GRAPHING.value,
        AuditStage.STATIC_RULES.value,
        AuditStage.PRIORITIZING.value,
    }
    names = {m.module_name for m in result.index.modules}
    assert {"shopfast.api", "shopfast.app", "shopfast.orders", "benchmarks.run_workload"} <= names
    app = next(m for m in result.index.modules if m.module_name == "shopfast.app")
    assert app.is_entrypoint
    assert any("console script 'shopfast'" in r for r in app.entrypoint_reasons)
    edges = {(e.source, e.target) for e in result.index.graph.edges}
    assert ("shopfast.api", "shopfast.app") in edges
    assert ("shopfast.inventory_sync", "shopfast.catalog") in edges
    graph_counts = {n.module_name: n.finding_count for n in result.index.graph.nodes}
    assert graph_counts["shopfast.inventory_sync"] == 5
    assert not result.is_partial


def test_sensitive_files_are_never_read(result: StaticAuditResult) -> None:
    secret = (SHOPFAST / ".env").read_text().split("=", 1)[1].strip()
    assert secret.startswith("fake-")
    assert secret not in result.model_dump_json()


def test_progress_reports_every_stage() -> None:
    seen: list[tuple[str, float]] = []
    run_static_audit(
        SHOPFAST,
        scan_inventory(SHOPFAST),
        Settings(),
        progress=lambda stage, fraction, _msg: seen.append((stage.value, fraction)),
    )
    stages = [s for s, _ in seen]
    assert stages.index("INDEXING") < stages.index("GRAPHING") < stages.index("STATIC_RULES")
    assert all(0.0 <= f <= 1.0 for _, f in seen)
    fractions = [f for _, f in seen]
    assert fractions == sorted(fractions)


def test_max_python_files_limit_marks_partial(tmp_path: Path) -> None:
    settings = Settings(max_python_files=3)
    limited = run_static_audit(SHOPFAST, scan_inventory(SHOPFAST), settings)
    assert limited.stats.modules == 3
    assert limited.is_partial
    assert any("max_python_files" in limit for limit in limited.limits_hit)
    # source files are analyzed before tests when the limit is hit
    assert all(not m.file_path.startswith("tests/") for m in limited.index.modules)
