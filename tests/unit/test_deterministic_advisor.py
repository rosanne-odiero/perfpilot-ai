"""Deterministic advisor: grouping, effort, insights, provenance, and diffs that really apply."""

from __future__ import annotations

import ast
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from audit_support import SHOPFAST, scan_inventory

from backend.ai.citation_guard import CitationGuard, contains_runtime_claim
from backend.ai.deterministic_advisor import (
    AdvisorOutput,
    DeterministicAdvisor,
    estimate_effort,
    group_findings,
)
from backend.ai.diffs import DiffApplyError, apply_unified_diff, make_unified_diff
from backend.ai.schemas import Citation
from backend.audit.pipeline import STATIC_STAGES, AuditState, run_pipeline
from backend.core.config import Settings
from backend.models.audit_schemas import Finding, Recommendation
from backend.models.enums import CodeRole, EffortEstimate, Provenance

ADVISOR_REPO = Path(__file__).parents[1] / "fixtures" / "repos" / "advisor"


def _advise(root: Path) -> tuple[AdvisorOutput, AuditState]:
    state = AuditState(root=root, inventory=scan_inventory(root), settings=Settings())
    run_pipeline(state, STATIC_STAGES)
    build = state.require_build()
    sources = {p: m.source for p, m in build.parsed.items()}
    return DeterministicAdvisor(build.index, sources, root).advise(state.findings), state


@pytest.fixture(scope="module")
def advised() -> tuple[AdvisorOutput, AuditState]:
    return _advise(ADVISOR_REPO)


def _by_function(output: AdvisorOutput) -> dict[str, Recommendation]:
    found: dict[str, Recommendation] = {}
    for rec in output.recommendations:
        for citation in rec.citations:
            found.setdefault(citation.symbol or citation.file_path, rec)
    return found


def test_every_finding_is_covered_once_with_real_citations(
    advised: tuple[AdvisorOutput, AuditState],
) -> None:
    output, state = advised
    covered = [fid for rec in output.recommendations for fid in rec.finding_ids]
    assert sorted(covered) == sorted(f.id for f in state.findings)
    guard = CitationGuard(state.require_build().index, (f.id for f in state.findings))
    for rec in output.recommendations:
        assert rec.provenance is Provenance.STATIC
        assert rec.generated_by == "deterministic"
        assert rec.kind
        assert rec.citations
        for c in rec.citations:
            cited = Citation(
                file_path=c.file_path, line_start=c.line_start, line_end=c.line_end, symbol=c.symbol
            )
            assert guard.location(cited) is not None
        for text in (rec.expected_effect, rec.summary, rec.proposed_strategy, rec.uncertainty):
            assert not contains_runtime_claim(text), text
    assert [r.priority_rank for r in output.recommendations] == list(
        range(1, len(output.recommendations) + 1)
    )


def test_n_plus_one_in_a_router_becomes_one_recommendation(
    advised: tuple[AdvisorOutput, AuditState],
) -> None:
    output, _ = advised
    router = [
        r
        for r in output.recommendations
        if r.kind == "batch-db-queries" and r.citations[0].file_path == "app/routes.py"
    ]
    assert len(router) == 1
    rec = router[0]
    assert {c.symbol for c in rec.citations} == {"list_orders", "list_customers"}
    assert rec.effort is EffortEstimate.MEDIUM
    assert rec.priority_rank == 1
    assert "request handler" in rec.rationale
    assert rec.suggested_diff is None


def test_seed_code_is_ranked_low_and_qualified(advised: tuple[AdvisorOutput, AuditState]) -> None:
    output, state = advised
    seed = next(r for r in output.recommendations if r.citations[0].file_path == "seed/seed.py")
    router = next(r for r in output.recommendations if r.citations[0].file_path == "app/routes.py")
    assert seed.priority_rank > router.priority_rank
    assert "seed, fixture or migration code" in seed.expected_effect
    seed_finding = next(f for f in state.findings if f.location.file_path == "seed/seed.py")
    assert seed_finding.code_role is CodeRole.SEED_MIGRATION


def test_insights(advised: tuple[AdvisorOutput, AuditState]) -> None:
    output, _ = advised
    insights = {i.title: i for i in output.insights}
    client = insights["Introduce a shared HTTP client"]
    assert set(client.modules) == {"app.clients_a", "app.clients_b"}
    boundary = insights["Synchronous database access inside async code"]
    assert "2 coroutines (2 of them request handlers)" in boundary.description
    assert {c.symbol for c in boundary.citations} == {"list_orders_async", "list_customers_async"}
    assert "Findings outside request paths were deprioritized" in insights
    for insight in output.insights:
        assert insight.provenance is Provenance.STATIC
        assert insight.citations
    async_rec = next(r for r in output.recommendations if r.kind == "async-db-boundary")
    assert "async/sync boundary" in async_rec.proposed_strategy


@pytest.mark.parametrize(
    ("base", "functions", "files", "expected"),
    [
        (EffortEstimate.SMALL, 1, 1, EffortEstimate.SMALL),
        (EffortEstimate.SMALL, 3, 1, EffortEstimate.MEDIUM),
        (EffortEstimate.SMALL, 3, 3, EffortEstimate.LARGE),
        (EffortEstimate.MEDIUM, 3, 1, EffortEstimate.LARGE),
        (EffortEstimate.LARGE, 5, 5, EffortEstimate.LARGE),
    ],
)
def test_effort(base: EffortEstimate, functions: int, files: int, expected: EffortEstimate) -> None:
    assert estimate_effort(base, functions, files) is expected


def test_grouping_keeps_same_rule_in_different_functions_apart(
    advised: tuple[AdvisorOutput, AuditState],
) -> None:
    _, state = advised
    text_findings = [f for f in state.findings if f.location.file_path == "app/text.py"]
    groups = group_findings(text_findings)
    assert len(groups) == len(text_findings)
    db = [f for f in state.findings if f.rule_id == "PP-DB-001"]
    assert len(group_findings(db)) == 2  # router + seed script


# ------------------------------------------------------------------------------- diffs

DIFF_FUNCTIONS = {"render", "extract_ids", "keep_known", "drop_blocked", "paths"}
NO_DIFF_FUNCTIONS = {"merge_unique", "render_with_header"}
BEHAVIOR: dict[str, tuple[Any, ...]] = {
    "render": (["a", "b", "c"],),
    "extract_ids": (["id-12 x", "none", "ID-7"],),
    "keep_known": (["AA", "ZZ", "GG", "AA"],),
    "drop_blocked": (["x1", "ok", "x7", "fine"],),
    "paths": (15,),
}


def _diff_for(output: AdvisorOutput, symbol: str) -> str | None:
    rec = _by_function(output)[symbol]
    return rec.suggested_diff


def _load_functions(text: str) -> dict[str, Callable[..., Any]]:
    namespace: dict[str, Any] = {}
    exec(compile(text, "<fixture>", "exec"), namespace)  # trusted fixture code
    return namespace


def test_safe_rewrites_produce_diffs_that_apply(
    advised: tuple[AdvisorOutput, AuditState], tmp_path: Path
) -> None:
    output, _ = advised
    original = (ADVISOR_REPO / "app" / "text.py").read_text()
    for symbol in NO_DIFF_FUNCTIONS:
        assert _diff_for(output, symbol) is None, symbol
    patched = original
    for symbol in sorted(DIFF_FUNCTIONS):
        diff = _diff_for(output, symbol)
        assert diff is not None, symbol
        assert diff.startswith("--- a/app/text.py\n+++ b/app/text.py\n")
        new_text = apply_unified_diff(original, diff)
        ast.parse(new_text)
        removed = [
            line[1:]
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        context = [line[1:] for line in diff.splitlines() if line.startswith(" ")]
        for line in removed + context:
            assert line in original.split("\n"), f"diff line not from the real file: {line!r}"
        before = _load_functions(original)[symbol]
        after = _load_functions(new_text)[symbol]
        assert before(*BEHAVIOR[symbol]) == after(*BEHAVIOR[symbol]), symbol
        if symbol == "drop_blocked":
            patched = new_text
    assert '{"x1", "x2", "x3", "x4", "x5", "x6", "x7"}' in patched


def test_regex_and_cache_rewrites_shape(advised: tuple[AdvisorOutput, AuditState]) -> None:
    output, _ = advised
    regex = _diff_for(output, "extract_ids") or ""
    assert '+_EXTRACT_IDS_PATTERN = re.compile(r"ID-(\\d+)", re.IGNORECASE)' in regex
    assert "+        pattern = _EXTRACT_IDS_PATTERN" in regex
    cache = _diff_for(output, "paths") or ""
    assert "+import functools" in cache
    assert "+@functools.lru_cache(maxsize=None)" in cache
    join = _diff_for(output, "render") or ""
    assert "+    out_parts = []" in join
    assert '+        out_parts.append(row.upper() + "\\n")' in join
    assert '+    out = "".join(out_parts)' in join


@pytest.mark.skipif(shutil.which("patch") is None, reason="the `patch` tool is not installed")
def test_diffs_apply_with_patch_tool(
    advised: tuple[AdvisorOutput, AuditState], tmp_path: Path
) -> None:
    output, _ = advised
    for symbol in sorted(DIFF_FUNCTIONS):
        work = tmp_path / symbol
        shutil.copytree(ADVISOR_REPO, work)
        diff = _diff_for(output, symbol)
        assert diff is not None
        (work / "change.diff").write_text(diff)
        result = subprocess.run(
            ["patch", "-p1", "--forward", "--input", "change.diff"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        expected = apply_unified_diff((ADVISOR_REPO / "app" / "text.py").read_text(), diff)
        assert (work / "app" / "text.py").read_text() == expected


def test_no_diff_when_disk_content_differs(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    shutil.copytree(ADVISOR_REPO, work)
    output, state = _advise(work)
    assert _diff_for(output, "render") is not None
    (work / "app" / "text.py").write_text((work / "app" / "text.py").read_text() + "\n# edited\n")
    build = state.require_build()
    sources = {p: m.source for p, m in build.parsed.items()}
    stale = DeterministicAdvisor(build.index, sources, work).advise(state.findings)
    assert all(r.suggested_diff is None for r in stale.recommendations)


def test_unified_diff_round_trip_and_mismatch() -> None:
    old = "a\nb\nc"
    new = "a\nB\nc\nd"
    diff = make_unified_diff("x.py", old, new)
    assert "\\ No newline at end of file" in diff
    assert apply_unified_diff(old, diff) == new
    with pytest.raises(DiffApplyError):
        apply_unified_diff("a\nX\nc", diff)
    with pytest.raises(DiffApplyError):
        apply_unified_diff(old, "--- a/x.py\n+++ b/x.py\n")


def test_shopfast_advice() -> None:
    output, state = _advise(SHOPFAST)
    assert output.recommendations
    assert output.recommendations[0].kind == "unblock-event-loop"
    assert {c.symbol for c in output.recommendations[0].citations} == {
        "get_product",
        "fetch_exchange_rate",
    }
    findings: list[Finding] = state.findings
    assert sum(len(r.finding_ids) for r in output.recommendations) == len(findings)
    diffs = [r for r in output.recommendations if r.suggested_diff]
    assert {r.kind for r in diffs} == {"precompile-regex", "memoize-recursion"}
    for rec in diffs:
        path = rec.citations[0].file_path
        assert apply_unified_diff((SHOPFAST / path).read_text(), rec.suggested_diff or "")
