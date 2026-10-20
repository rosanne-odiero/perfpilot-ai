from __future__ import annotations

from pathlib import Path

from backend.models.audit_schemas import CodeLocation, Finding
from backend.models.enums import Confidence, FindingCategory, Severity
from backend.models.orm.workloads import Workload
from backend.models.workloads import HotspotLocation, WorkloadSpec
from backend.optimization.recommendations import build_recommendation, match_hotspot
from backend.profiling.hotspots import HotspotRecord
from backend.services.workloads import build_spec, load_manifest

ROOT = Path(__file__).resolve().parents[2] / "examples" / "duplicate_counter"
PROFILE = {
    "id": "p1",
    "status": "SUCCEEDED",
    "size": "small",
    "input_sha256": "abc",
    "total_profiled_time_ms": 10.0,
    "wall_time_ms": 11.0,
}


def _spec() -> WorkloadSpec:
    manifest = load_manifest(ROOT)
    return build_spec(
        Workload(
            id="w",
            project_id="p",
            slug="duplicate_counter",
            name=manifest.name,
            description="",
            workspace_path=str(ROOT),
            manifest=manifest.model_dump(mode="json"),
        )
    )


def _finding(rule_id: str = "PP-COLL-001", symbol: str = "count_duplicates") -> Finding:
    return Finding(
        id="f1",
        rule_id=rule_id,
        title="Linear list search/removal inside a loop",
        category=FindingCategory.COLLECTIONS,
        description="d",
        severity=Severity.MEDIUM,
        confidence=Confidence.MEDIUM,
        location=CodeLocation(file_path="workload.py", line_start=13, line_end=13, symbol=symbol),
        snippet="items.count(item)",
        suggestion="s",
        tradeoffs="t",
    )


def _hotspot(name: str, cumulative_pct: float, user: bool = True) -> HotspotRecord:
    return HotspotRecord(
        function_name=name,
        file_path="workload.py" if user else "<built-in>",
        location_kind=HotspotLocation.WORKSPACE if user else HotspotLocation.BUILTIN,
        line_number=10 if user else 0,
        primitive_calls=1,
        total_calls=1,
        internal_time_ms=0.5,
        cumulative_time_ms=cumulative_pct / 10,
        percentage_of_profiled_time=5.0,
        cumulative_percentage=cumulative_pct,
        source_available=user,
        is_user_code=user,
    )


def test_measured_evidence_quotes_the_profile() -> None:
    hotspots = [_hotspot("count_duplicates", 97.0), _hotspot("<method 'count'>", 90.0, False)]
    draft = build_recommendation(
        _finding(), spec=_spec(), profile=PROFILE, hotspots=hotspots, file_sha256="h"
    )
    evidence = draft.profiler_evidence
    assert evidence["provenance"] == "MEASURED"
    assert evidence["target_function"]["cumulative_percentage"] == 97.0
    assert evidence["largest_non_workspace_functions"][0]["function_name"] == "<method 'count'>"
    assert "97.0% of profiled time" in draft.observed_bottleneck
    assert draft.estimated_impact["level"] == "high"
    assert draft.estimated_impact["provenance"] == "AI_HYPOTHESIS"
    assert draft.confidence == Confidence.HIGH
    assert draft.transform == "list_count_to_counter"
    assert draft.target_file_sha256 == "h"
    assert any("sizes ['small', 'medium']" in step for step in draft.validation_plan)


def test_unmeasured_findings_stay_static_hypotheses() -> None:
    draft = build_recommendation(
        _finding(), spec=_spec(), profile=PROFILE, hotspots=[], file_sha256=None
    )
    assert draft.profiler_evidence["target_function"] is None
    assert "not observed" in draft.observed_bottleneck
    assert draft.estimated_impact["level"] == "unknown"
    assert draft.confidence == Confidence.LOW
    assert draft.provenance["observed_bottleneck"] == "STATIC"


def test_impact_levels_are_qualitative() -> None:
    spec = _spec()
    for share, level in ((60.0, "high"), (20.0, "moderate"), (6.0, "low")):
        draft = build_recommendation(
            _finding(),
            spec=spec,
            profile=PROFILE,
            hotspots=[_hotspot("count_duplicates", share)],
            file_sha256=None,
        )
        assert draft.estimated_impact["level"] == level
    regex = build_recommendation(
        _finding("PP-REGEX-001"),
        spec=spec,
        profile=PROFILE,
        hotspots=[_hotspot("count_duplicates", 99.0)],
        file_sha256=None,
    )
    assert regex.estimated_impact["level"] == "uncertain"


def test_hotspot_matching_requires_same_file_and_function() -> None:
    assert match_hotspot(_finding(), [_hotspot("other", 99.0)]) is None
    assert match_hotspot(_finding(symbol="C.count_duplicates"), [_hotspot("count_duplicates", 9)])
    unknown = build_recommendation(
        _finding("PP-LOOP-004"), spec=_spec(), profile=PROFILE, hotspots=[], file_sha256=None
    )
    assert unknown.transform is None
    assert unknown.title == "Linear list search/removal inside a loop"
