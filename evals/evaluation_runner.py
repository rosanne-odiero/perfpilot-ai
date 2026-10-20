"""Evaluate the advisor on ``evals/recommendation_cases.json`` and write a Markdown report.

Usage (from the repository root)::

    .venv/bin/python evals/evaluation_runner.py            # deterministic advisor only
    .venv/bin/python evals/evaluation_runner.py --llm      # also the configured LLM

``--llm`` uses ``PERFPILOT_LLM_BASE_URL`` / ``PERFPILOT_LLM_MODEL`` / ``PERFPILOT_LLM_API_KEY``
and counts as consent to send the (redacted) case snippets to that endpoint. Each case is written
to a temporary directory under ``evals/``, statically audited (never executed), and advised.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.ai.analysis import AIAnalysisResult, run_ai_analysis  # noqa: E402
from backend.ai.citation_guard import CitationGuard  # noqa: E402
from backend.ai.diffs import DiffApplyError, apply_unified_diff  # noqa: E402
from backend.ai.schemas import Citation  # noqa: E402
from backend.audit.pipeline import STATIC_STAGES, AuditState, run_pipeline  # noqa: E402
from backend.core.config import Settings  # noqa: E402
from backend.models.audit_schemas import RepositoryInventory, SourceFileInfo  # noqa: E402
from backend.models.enums import Provenance  # noqa: E402


@dataclass
class CaseResult:
    case_id: str
    negative: bool
    expected_rules: set[str]
    detected_rules: set[str]
    expected_kinds: set[str]
    produced_kinds: set[str]
    diff_expected: bool
    diff_attached: bool
    diffs_total: int = 0
    diffs_valid: int = 0
    citations_total: int = 0
    citations_valid: int = 0
    checks_failed: list[str] = field(default_factory=list)
    llm: AIAnalysisResult | None = None

    @property
    def rules_ok(self) -> bool:
        return self.expected_rules == self.detected_rules

    @property
    def kinds_ok(self) -> bool:
        return self.expected_kinds == self.produced_kinds

    @property
    def diff_ok(self) -> bool:
        return self.diff_expected == self.diff_attached

    @property
    def passed(self) -> bool:
        return (
            self.rules_ok
            and self.kinds_ok
            and self.diff_ok
            and self.diffs_valid == self.diffs_total
            and self.citations_valid == self.citations_total
            and not self.checks_failed
        )


def _write_case(root: Path, files: dict[str, list[str]]) -> RepositoryInventory:
    infos = []
    for rel, lines in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        infos.append(SourceFileInfo(path=rel, size_bytes=path.stat().st_size, is_python=True))
    return RepositoryInventory(root_name=root.name, files=infos, total_files_seen=len(infos))


def evaluate_case(case: dict[str, Any], settings: Settings, use_llm: bool) -> CaseResult:
    expect = case["expect"]
    with tempfile.TemporaryDirectory(dir=EVALS, prefix=".work-") as tmp:
        root = Path(tmp)
        inventory = _write_case(root, case["files"])
        state = AuditState(root=root, inventory=inventory, settings=settings)
        run_pipeline(state, STATIC_STAGES)
        build = state.require_build()
        sources = {p: m.source for p, m in build.parsed.items()}
        result = run_ai_analysis(
            findings=state.findings,
            index=build.index,
            sources=sources,
            settings=settings,
            use_llm=use_llm,
            llm_consent=use_llm,
            root=root,
        )
        static = [r for r in result.recommendations if r.provenance is Provenance.STATIC]
        outcome = CaseResult(
            case_id=case["id"],
            negative=not expect["rules"],
            expected_rules=set(expect["rules"]),
            detected_rules={f.rule_id for f in state.findings},
            expected_kinds=set(expect["kinds"]),
            produced_kinds={r.kind or "" for r in static},
            diff_expected=bool(expect.get("diff")),
            diff_attached=any(r.suggested_diff for r in static),
            llm=result if use_llm else None,
        )
        guard = CitationGuard(build.index, (f.id for f in state.findings))
        for rec in result.recommendations:
            for c in rec.citations:
                outcome.citations_total += 1
                citation = Citation(
                    file_path=c.file_path,
                    line_start=c.line_start,
                    line_end=c.line_end,
                    symbol=c.symbol,
                )
                outcome.citations_valid += guard.location(citation) is not None
            if rec.suggested_diff:
                outcome.diffs_total += 1
                path = rec.citations[0].file_path
                try:
                    ast.parse(apply_unified_diff(sources[path], rec.suggested_diff))
                    outcome.diffs_valid += 1
                except (DiffApplyError, SyntaxError):
                    pass
        roles = {f.location.symbol: f.code_role for f in state.findings}
        for symbol, role in expect.get("roles", {}).items():
            actual = roles.get(symbol)
            if actual is None or actual.value != role:
                outcome.checks_failed.append(f"role {symbol}: {actual} != {role}")
        titles = {i.title for i in result.insights}
        for title in expect.get("insights", []):
            if title not in titles:
                outcome.checks_failed.append(f"missing insight {title!r}")
        if "recommendations" in expect and len(static) != expect["recommendations"]:
            outcome.checks_failed.append(
                f"{len(static)} recommendations, expected {expect['recommendations']}"
            )
    return outcome


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"


def _metrics(results: list[CaseResult]) -> list[tuple[str, str]]:
    negatives = [r for r in results if r.negative]
    tp = sum(len(r.expected_rules & r.detected_rules) for r in results)
    fp = sum(len(r.detected_rules - r.expected_rules) for r in results)
    fn = sum(len(r.expected_rules - r.detected_rules) for r in results)
    total = len(results)
    return [
        (
            "Detection accuracy (detected rule set == expected)",
            _rate(sum(r.rules_ok for r in results), total),
        ),
        ("Rule-level precision", _rate(tp, tp + fp)),
        ("Rule-level recall", _rate(tp, tp + fn)),
        (
            "Recommendation accuracy (kinds == expected)",
            _rate(sum(r.kinds_ok for r in results), total),
        ),
        (
            "Negative cases with no recommendation",
            _rate(sum(not r.produced_kinds for r in negatives), len(negatives)),
        ),
        ("Suggested-diff expectation met", _rate(sum(r.diff_ok for r in results), total)),
        (
            "Suggested diffs that apply cleanly and parse",
            _rate(sum(r.diffs_valid for r in results), sum(r.diffs_total for r in results)),
        ),
        (
            "Citations that resolve to indexed code",
            _rate(sum(r.citations_valid for r in results), sum(r.citations_total for r in results)),
        ),
    ]


def _llm_metrics(results: list[CaseResult]) -> list[tuple[str, str]]:
    usages = [r.llm.usage for r in results if r.llm]
    kept = sum(
        rec.provenance is Provenance.AI_HYPOTHESIS
        for r in results
        if r.llm
        for rec in r.llm.recommendations
    )
    statuses = Counter(s for u in usages for s in u.stage_status.values())
    tokens = f"{sum(u.input_tokens for u in usages)} / {sum(u.output_tokens for u in usages)}"
    return [
        ("LLM calls", str(sum(u.calls for u in usages))),
        ("LLM input / output tokens", tokens),
        ("AI_HYPOTHESIS recommendations kept", str(kept)),
        ("Rejected citations", str(sum(u.rejected_citations for u in usages))),
        ("Rejected runtime-number claims", str(sum(u.rejected_claims for u in usages))),
        ("Rejected model items", str(sum(u.rejected_items for u in usages))),
        ("Fallbacks to deterministic results", str(sum(u.fallbacks for u in usages))),
        ("Budget exhausted (cases)", str(sum(u.budget_exhausted for u in usages))),
        ("Step statuses", ", ".join(f"{k} x{v}" for k, v in sorted(statuses.items()))),
    ]


def render_report(results: list[CaseResult], use_llm: bool, model: str | None) -> str:
    positives = sum(not r.negative for r in results)
    mode = f"deterministic advisor + LLM ({model})" if use_llm else "deterministic advisor only"
    metrics = _metrics(results)
    metrics += _llm_metrics(results) if use_llm else [("LLM calls / fallbacks", "0 / 0 (no LLM)")]
    lines = [
        "# Advisor evaluation report",
        "",
        "Generated by `evals/evaluation_runner.py` from `evals/recommendation_cases.json`. Every",
        "number below was measured by running the runner; nothing is estimated.",
        "",
        f"- Mode: {mode}",
        f"- Cases: {len(results)} ({positives} positive, {len(results) - positives} negative)",
        f"- Cases passing every check: {_rate(sum(r.passed for r in results), len(results))}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        *(f"| {name} | {value} |" for name, value in metrics),
        "",
        "## Cases",
        "",
        "| Case | Expected rules | Detected rules | Kinds OK | Diff (expected/attached) "
        "| Citations valid | Result |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        notes = f" — {'; '.join(r.checks_failed)}" if r.checks_failed else ""
        lines.append(
            f"| `{r.case_id}` | {', '.join(sorted(r.expected_rules)) or '—'} | "
            f"{', '.join(sorted(r.detected_rules)) or '—'} | {'yes' if r.kinds_ok else 'no'} | "
            f"{'yes' if r.diff_expected else 'no'}/{'yes' if r.diff_attached else 'no'} | "
            f"{r.citations_valid}/{r.citations_total} | {'PASS' if r.passed else 'FAIL'}{notes} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", action="store_true", help="also run the configured LLM")
    parser.add_argument("--cases", type=Path, default=EVALS / "recommendation_cases.json")
    parser.add_argument("--output", type=Path, default=EVALS / "evaluation_report.md")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.llm and not settings.llm_configured:
        parser.error("--llm needs PERFPILOT_LLM_BASE_URL and PERFPILOT_LLM_MODEL")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    results = [evaluate_case(case, settings, args.llm) for case in cases]
    report = render_report(results, args.llm, settings.llm_model if args.llm else None)
    args.output.write_text(report, encoding="utf-8")
    failed = [r.case_id for r in results if not r.passed]
    print(f"{len(results) - len(failed)}/{len(results)} cases passed; report: {args.output}")
    for case_id in failed:
        print(f"  FAIL {case_id}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
