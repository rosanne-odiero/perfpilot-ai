from __future__ import annotations

import pytest
from report_fixtures import (
    COMMIT_SHA,
    REPO_URL,
    AuditFactory,
    build_findings,
    build_recommendations,
)

from backend.core.exceptions import ConflictError, NotFoundError
from backend.models.database import session_scope
from backend.models.enums import (
    AuditStatus,
    EffortEstimate,
    Provenance,
    RepositorySourceType,
    Severity,
)
from backend.reporting.builder import build_audit_report
from backend.reporting.model import SECTION_TITLES, AuditReport
from backend.reporting.roadmap import build_roadmap
from backend.reporting.sources import SENSITIVE_WITHHELD, github_base, github_permalink


def _build(audit_id: str) -> AuditReport:
    with session_scope() as session:
        return build_audit_report(session, audit_id)


def test_report_from_realistic_rows_covers_every_section(make_audit: AuditFactory) -> None:
    report = _build(make_audit())

    assert len(SECTION_TITLES) == 15
    assert report.metadata.title == "Performance audit — acme/shopfront"
    assert [e.label for e in report.legend] == list(Provenance)

    counts = report.executive_summary.counts
    assert counts.findings_total == 9
    assert counts.recommendations_total == 7
    assert counts.modules_indexed == 12
    assert counts.python_files_analyzed == 13
    assert counts.findings_by_severity[Severity.CRITICAL] == 1
    assert counts.findings_by_provenance[Provenance.AI_HYPOTHESIS] == 1
    assert report.executive_summary.top_risks[0].finding_id == "F-002"
    summary = " ".join(report.executive_summary.text)
    assert "9 findings" in summary
    assert "7 recommendations" in summary
    assert "No repository code was executed" in summary

    repo = report.repository
    assert repo.commit_sha == COMMIT_SHA
    assert repo.short_sha == COMMIT_SHA[:7]
    assert repo.source_display == REPO_URL
    assert repo.branch == "main"
    assert repo.llm_model == "none"

    files = report.files
    assert files.skipped_total == 5
    assert files.skipped_by_reason == {
        "binary": 1,
        "excluded_directory": 1,
        "file_too_large": 1,
        "sensitive_file": 2,
    }
    assert files.sensitive_files_withheld == 2
    assert [e.file_path for e in files.parse_errors] == ["shopfront/legacy/py2_compat.py"]

    arch = report.architecture
    assert arch.node_count == 12
    assert arch.edge_count == 16
    assert {e.module_name for e in arch.entrypoints} == {"shopfront.main", "scripts.backfill"}
    assert arch.cycles == [["shopfront.services.pricing", "shopfront.services.inventory"]]
    assert arch.top_fan_in[0].fan_in == 3
    assert arch.graph is not None
    assert arch.graph.svg.startswith("<svg")
    assert any(e.in_cycle for e in arch.key_edges)
    assert {t.name for t in arch.technologies} >= {"FastAPI", "SQLAlchemy"}

    assert report.runtime is None
    assert report.validation is None
    assert report.benchmarks is None
    assert report.is_static_only
    assert len(report.complexity.entries) == 3
    assert [s.id for s in report.strategies.entries][:5] == [
        "R-001",
        "R-002",
        "R-003",
        "R-004",
        "R-005",
    ]
    assert report.strategies.entries[-1].id == "R-006"  # unranked last
    assert report.limitations.reproducibility.commit_sha == COMMIT_SHA
    assert any("git -C repo checkout" in c for c in report.limitations.reproducibility.commands)


def test_findings_are_prioritized_and_locations_match_input(make_audit: AuditFactory) -> None:
    report = _build(make_audit())
    source = {f.id: f for f in build_findings()}
    entries = report.findings.entries
    assert [e.rank for e in entries] == list(range(1, len(entries) + 1))
    scores = [e.priority_score for e in entries]
    assert scores == sorted(scores, reverse=True)
    for entry in entries:
        loc = source[entry.id].location
        assert (entry.file_path, entry.line_start, entry.line_end) == (
            loc.file_path,
            loc.line_start,
            loc.line_end,
        )
        expected = (
            f"{loc.file_path}:{loc.line_start}"
            if loc.line_end == loc.line_start
            else f"{loc.file_path}:{loc.line_start}-{loc.line_end}"
        )
        assert entry.location == expected


def test_evidence_has_numbered_snippets_and_commit_permalinks(make_audit: AuditFactory) -> None:
    report = _build(make_audit())
    evidence = {e.finding_id: e for e in report.evidence.entries}
    loop = evidence["F-001"]
    assert [line.number for line in loop.lines] == [42, 43, 44, 45]
    assert loop.lines[1].text == "    if item.sku in purchased_skus:"
    assert loop.permalink == (
        f"{REPO_URL}/blob/{COMMIT_SHA}/shopfront/services/recommendations.py#L42-L45"
    )
    assert evidence["F-003"].permalink is not None
    assert evidence["F-003"].permalink.endswith("#L31")
    assert report.evidence.permalinks_available


def test_sensitive_files_are_never_quoted(make_audit: AuditFactory) -> None:
    report = _build(make_audit())
    credentials = next(e for e in report.evidence.entries if e.finding_id == "F-009")
    assert credentials.lines == []
    assert credentials.withheld_reason == SENSITIVE_WITHHELD
    diff = next(d for d in report.code_changes.entries if d.recommendation_id == "R-007")
    assert diff.diff is None
    assert diff.withheld_reason == SENSITIVE_WITHHELD
    assert "client_secret" not in report.model_dump_json()


def test_local_source_has_no_permalinks(make_audit: AuditFactory) -> None:
    audit_id = make_audit(
        source_type=RepositorySourceType.LOCAL_DIRECTORY, locator="/srv/allowed/shopfront"
    )
    report = _build(audit_id)
    assert all(e.permalink is None for e in report.evidence.entries)
    assert not report.evidence.permalinks_available
    commands = report.limitations.reproducibility.commands
    # The host path is never written into a shareable report; only the folder name is.
    assert any('"local_path": "<path to shopfront>"' in c for c in commands)
    assert report.repository.source_display == "local directory (shopfront)"
    assert "/srv/allowed" not in report.model_dump_json()


def test_missing_index_and_partial_audit_are_reported(make_audit: AuditFactory) -> None:
    audit_id = make_audit(
        with_index=False,
        status=AuditStatus.PARTIAL,
        limits_hit=["max_python_files=5000 reached; 1200 files not analyzed"],
    )
    report = _build(audit_id)
    assert not report.architecture.index_available
    assert report.architecture.graph is None
    assert report.files.modules_indexed == 0
    limitations = " ".join(report.limitations.limitations)
    assert "PARTIAL" in limitations
    assert "max_python_files=5000 reached" in limitations
    assert "No code index was stored" in limitations
    assert "partial" in " ".join(report.executive_summary.text)


def test_empty_audit_still_has_every_section(make_audit: AuditFactory) -> None:
    report = _build(make_audit(findings=[], recommendations=[]))
    assert report.findings.total == 0
    assert report.code_changes.note == "No code changes were generated for this audit."
    assert all(not phase.items for phase in report.roadmap.phases)
    assert "No performance findings" in " ".join(report.executive_summary.text)


def test_unknown_and_unfinished_audits_are_rejected(make_audit: AuditFactory) -> None:
    with session_scope() as session, pytest.raises(NotFoundError):
        build_audit_report(session, "missing")
    running = make_audit(status=AuditStatus.RUNNING)
    with session_scope() as session, pytest.raises(ConflictError):
        build_audit_report(session, running)


def test_roadmap_rules_are_deterministic() -> None:
    findings = {f.id: f for f in build_findings()}
    roadmap = build_roadmap(build_recommendations(), findings)
    phases = {p.name: [i.recommendation_id for i in p.items] for p in roadmap.phases}
    # SMALL + linked HIGH/MEDIUM -> quick win; LARGE -> strategic; rest -> next.
    assert phases["Quick wins"] == ["R-001", "R-003", "R-004"]
    assert phases["Strategic"] == ["R-005"]
    assert phases["Next"] == ["R-002", "R-007", "R-006"]
    assert all(
        i.effort == EffortEstimate.LARGE
        for p in roadmap.phases
        if p.name == "Strategic"
        for i in p.items
    )
    assert len(roadmap.rules) == 4


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("https://github.com/acme/shopfront", "https://github.com/acme/shopfront"),
        ("https://github.com/acme/shopfront.git", "https://github.com/acme/shopfront"),
        ("https://github.com/acme/shop.front/tree/dev", "https://github.com/acme/shop.front"),
        ("https://evil.example/acme/shopfront", None),
        ("https://github.com/acme/shopfront/../../x", None),
        ("http://github.com/acme/shopfront", None),
    ],
)
def test_github_base_validation(locator: str, expected: str | None) -> None:
    assert github_base(RepositorySourceType.GITHUB_PUBLIC, locator) == expected


def test_permalink_requires_commit_sha_and_safe_path() -> None:
    from backend.models.audit_schemas import CodeLocation

    base = "https://github.com/acme/shopfront"
    loc = CodeLocation(file_path="a b/c.py", line_start=3, line_end=3)
    assert github_permalink(base, COMMIT_SHA, loc) == f"{base}/blob/{COMMIT_SHA}/a%20b/c.py#L3"
    assert github_permalink(base, "main", loc) is None
    assert github_permalink(base, None, loc) is None
    bad = CodeLocation(file_path="../etc/passwd", line_start=1, line_end=1)
    assert github_permalink(base, COMMIT_SHA, bad) is None
