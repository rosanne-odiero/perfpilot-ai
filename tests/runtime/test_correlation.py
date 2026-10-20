"""Pure correlation rules on the real ShopFast code index with hand-built hotspot inputs.

The hotspot numbers below are synthetic *inputs* for the mapping rules; measured correlation on a
real profile is covered in ``test_runtime_profile_job.py``.
"""

from __future__ import annotations

import pytest
from audit_support import SHOPFAST, scan_inventory

from backend.audit.correlation import (
    MEASURED_WEIGHT,
    CorrelationHotspot,
    correlate,
    exercised_path_factor,
    measured_factor,
    reset_to_static,
)
from backend.audit.static_audit import StaticAuditResult, run_static_audit
from backend.core.config import Settings
from backend.models.audit_schemas import Finding


@pytest.fixture(scope="module")
def audit() -> StaticAuditResult:
    return run_static_audit(SHOPFAST, scan_inventory(SHOPFAST), Settings())


def hotspot(
    file_path: str,
    line: int,
    name: str,
    attributed_pct: float,
    *,
    user: bool = True,
    hid: str | None = None,
) -> CorrelationHotspot:
    return CorrelationHotspot(
        id=hid or f"{file_path}:{line}:{name}",
        function_name=name,
        file_path=file_path,
        line_number=line,
        total_calls=3,
        internal_time_ms=attributed_pct * 10,
        cumulative_time_ms=attributed_pct * 20,
        percentage_of_profiled_time=attributed_pct,
        attributed_time_ms=attributed_pct * 10,
        attributed_percentage=attributed_pct,
        is_user_code=user,
    )


def _by_key(findings: list[Finding]) -> dict[tuple[str, str, int], Finding]:
    return {(f.rule_id, f.location.file_path, f.location.line_start): f for f in findings}


def test_measured_factor_formula() -> None:
    assert measured_factor(0.0) == 1.0
    assert measured_factor(0.25) == pytest.approx(1 + MEASURED_WEIGHT * 0.25)
    assert measured_factor(1.0) == 3.0
    assert measured_factor(7.0) == 3.0
    assert measured_factor(-1.0) == 1.0


def test_exact_symbol_match_attaches_evidence_and_reranks(audit: StaticAuditResult) -> None:
    result = correlate(
        "run-1",
        audit.index,
        audit.findings,
        [hotspot("shopfast/inventory_sync.py", 8, "reconcile", 40.0)],
    )
    finding = _by_key(result.findings)[("PP-LOOP-002", "shopfast/inventory_sync.py", 11)]
    static = _by_key(audit.findings)[("PP-LOOP-002", "shopfast/inventory_sync.py", 11)]
    assert finding.provenance == static.provenance == "STATIC"
    assert finding.measured is not None
    assert finding.measured.provenance == "MEASURED"
    assert finding.measured.profile_id == "run-1"
    assert finding.measured.measured_factor == pytest.approx(1.8)
    path_factor = exercised_path_factor(static)
    assert path_factor >= 1.0
    assert finding.priority_factors["exercised_path_factor"] == pytest.approx(path_factor, abs=1e-4)
    assert finding.priority_score == round(static.priority_score * path_factor * 1.8, 2)
    assert finding.priority_factors["static_priority_score"] == static.priority_score
    assert result.findings[0].id == finding.id
    [link] = result.correlations
    assert (link.location.symbol, link.location.line_start, link.location.line_end) == (
        "reconcile",
        8,
        14,
    )
    assert link.location.match == "exact"
    # Every other finding is untouched.
    others = [f for f in result.findings if f.id != finding.id]
    assert all(f.measured is None for f in others)
    static_scores = {f.id: f.priority_score for f in audit.findings}
    assert all(f.priority_score == static_scores[f.id] for f in others)


def test_findings_attach_only_to_their_innermost_function(audit: StaticAuditResult) -> None:
    # Both duplicate_skus findings (line 42) match duplicate_skus, not reconcile next door.
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [hotspot("shopfast/inventory_sync.py", 39, "duplicate_skus", 5.0)],
    )
    assert {(c.rule_id, c.location.symbol) for c in result.correlations} == {
        ("PP-LOOP-001", "duplicate_skus"),
        ("PP-COLL-001", "duplicate_skus"),
    }


def test_wrong_line_or_name_does_not_match(audit: StaticAuditResult) -> None:
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [
            hotspot("shopfast/inventory_sync.py", 9, "reconcile", 30.0),  # not the def line
            hotspot("shopfast/inventory_sync.py", 8, "other_name", 30.0),
            hotspot("not/indexed.py", 8, "reconcile", 30.0),
        ],
    )
    assert result.correlations == []
    assert result.runtime_only == []
    assert result.unmapped_hotspots == 3
    assert all(f.measured is None for f in result.findings)


def test_library_hotspots_are_never_correlated(audit: StaticAuditResult) -> None:
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [hotspot("shopfast/inventory_sync.py", 8, "reconcile", 50.0, user=False)],
    )
    assert result.correlations == []
    assert result.unmapped_hotspots == 0


def test_comprehensions_count_for_the_enclosing_function(audit: StaticAuditResult) -> None:
    # top_rated (catalog.py:62) sorts with a lambda on line 66.
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [
            hotspot("shopfast/catalog.py", 62, "top_rated", 2.0, hid="fn"),
            hotspot("shopfast/catalog.py", 66, "<lambda>", 1.5, hid="lambda"),
        ],
    )
    [link] = [c for c in result.correlations if c.rule_id == "PP-LOOP-004"]
    assert link.hotspot_id == "fn"
    assert link.attributed_share == pytest.approx(0.035)


def test_module_code_matches_only_module_level_findings(audit: StaticAuditResult) -> None:
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [hotspot("shopfast/pricing.py", 1, "<module>", 4.0)],
    )
    assert {(c.rule_id, c.location.match) for c in result.correlations} == {
        ("PP-ARCH-002", "module")
    }
    assert result.runtime_only == []


def test_runtime_only_hotspots(audit: StaticAuditResult) -> None:
    result = correlate(
        "r",
        audit.index,
        audit.findings,
        [
            hotspot("shopfast/catalog.py", 21, "build_catalog", 8.0),
            hotspot("shopfast/pricing.py", 30, "price_with_tax", 0.5),  # under the threshold
            hotspot("benchmarks/run_workload.py", 1, "<module>", 12.0),
        ],
    )
    assert [r.function_name for r in result.runtime_only] == ["build_catalog"]
    only = result.runtime_only[0]
    assert (only.location.line_start, only.location.line_end) == (21, 35)
    assert only.is_test_code is False
    assert result.correlations == []


def test_correlation_is_idempotent_and_resettable(audit: StaticAuditResult) -> None:
    hotspots = [hotspot("shopfast/inventory_sync.py", 8, "reconcile", 40.0)]
    first = correlate("r1", audit.index, audit.findings, hotspots)
    second = correlate("r2", audit.index, first.findings, hotspots)
    assert [(f.id, f.priority_score) for f in first.findings] == [
        (f.id, f.priority_score) for f in second.findings
    ]
    # A later run that no longer exercises reconcile drops the old evidence.
    third = correlate("r3", audit.index, second.findings, [])
    static_scores = {f.id: f.priority_score for f in audit.findings}
    assert all(f.measured is None for f in third.findings)
    assert all(f.priority_score == static_scores[f.id] for f in third.findings)
    restored = reset_to_static(first.findings[0])
    assert restored.measured is None
    assert "measured_factor" not in restored.priority_factors


def test_exercised_path_factor_lifts_static_path_guess_to_floor() -> None:
    from backend.audit.correlation import EXERCISED_PATH_FLOOR

    def _with(reach: float, role: float) -> Finding:
        return Finding.model_validate(
            {
                "id": "f",
                "rule_id": "R",
                "title": "t",
                "category": "NESTED_LOOPS",
                "description": "d",
                "severity": "HIGH",
                "confidence": "HIGH",
                "location": {"file_path": "a.py", "line_start": 1, "line_end": 1},
                "snippet": "x",
                "suggestion": "s",
                "tradeoffs": "t",
                "priority_factors": {"entrypoint_reachability": reach, "path_role": role},
            }
        )

    assert exercised_path_factor(_with(0.5, 0.5)) == pytest.approx(EXERCISED_PATH_FLOOR / 0.25)
    assert exercised_path_factor(_with(1.0, 1.0)) == 1.0
    assert exercised_path_factor(_with(1.0, 0.9)) == 1.0
