"""Hierarchical LLM pipeline against a fake endpoint: happy path, fallbacks, budgets, cache."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from audit_support import scan_inventory
from llm_support import (
    EMPTY_OUTPUT,
    FakeLLM,
    client_factory,
    finding_ids_in,
    llm_settings,
    modules_in,
    units_in,
)

from backend.ai.analysis import AIAnalysisResult, run_ai_analysis
from backend.audit.pipeline import STATIC_STAGES, AuditState, run_pipeline
from backend.core.config import Settings
from backend.models.enums import Provenance

ADVISOR_REPO = Path(__file__).parents[1] / "fixtures" / "repos" / "advisor"


@pytest.fixture(scope="module")
def audited() -> AuditState:
    state = AuditState(
        root=ADVISOR_REPO, inventory=scan_inventory(ADVISOR_REPO), settings=Settings()
    )
    run_pipeline(state, STATIC_STAGES)
    return state


def _run(state: AuditState, cfg: Settings, fake: FakeLLM) -> AIAnalysisResult:
    build = state.require_build()
    return run_ai_analysis(
        findings=state.findings,
        index=build.index,
        sources={p: m.source for p, m in build.parsed.items()},
        settings=cfg,
        use_llm=True,
        llm_consent=True,
        root=state.root,
        client_factory=client_factory(fake),
    )


def valid_responder(step: str, user: str) -> Any:
    if step == "unit_summaries":
        return {
            "summaries": [
                {"unit_id": u, "summary": f"Summary of {u}.", "performance_notes": "Loops."}
                for u in units_in(user)
            ]
            + [{"unit_id": "app.ghost:nothing", "summary": "Invented unit."}]
        }
    if step == "dependency_context":
        return {
            "modules": [{"module": m, "role": "Routes.", "concerns": ""} for m in modules_in(user)]
        }
    if step == "hot_paths":
        return {
            "paths": [
                {
                    "units": ["app.routes:list_orders"],
                    "rationale": "Runs once per request and queries per order.",
                    "citations": [{"file_path": "app/routes.py", "line_start": 9, "line_end": 13}],
                }
            ]
        }
    if step == "correlation":
        return {
            "insights": [
                {
                    "title": "Data access without batch helpers",
                    "description": "Routes query per item; a bulk loader would help.",
                    "modules": ["app.routes", "app.invented"],
                    "citations": [{"file_path": "app/routes.py", "line_start": 12, "line_end": 12}],
                }
            ]
        }
    if step == "recommendations":
        ids = finding_ids_in(user, "app/routes.py")
        recs = []
        if ids:
            recs.append(
                {
                    "title": "Load orders in one query",
                    "summary": "list_orders queries once per id.",
                    "rationale": "PP-DB-001 flags the query in the loop.",
                    "proposed_strategy": "Query all ids with one IN clause before the loop.",
                    "finding_ids": ids[:1],
                    "citations": [
                        {
                            "file_path": "app/routes.py",
                            "line_start": 11,
                            "line_end": 12,
                            "symbol": "list_orders",
                        }
                    ],
                    "effort": "SMALL",
                    "uncertainty": "List sizes are unknown.",
                    "expected_effect": "One database round trip instead of one per id.",
                    "correctness_risks": "Ordering of results may change.",
                }
            )
        return {"recommendations": recs}
    return EMPTY_OUTPUT.get(step, {})


def test_pipeline_happy_path(audited: AuditState, settings: Settings) -> None:
    fake = FakeLLM(valid_responder)
    cfg = llm_settings(settings, llm_input_cost_per_1k=0.5, llm_output_cost_per_1k=1.5)
    result = _run(audited, cfg, fake)
    assert result.mode == "deterministic+llm"
    assert set(fake.steps) == {
        "unit_summaries",
        "dependency_context",
        "hot_paths",
        "correlation",
        "recommendations",
    }
    usage = result.usage
    assert usage.model == "fake-model"
    assert usage.calls == fake.calls
    assert usage.input_tokens == 100 * fake.calls
    assert usage.estimated_cost_usd == pytest.approx(
        fake.calls * (100 / 1000 * 0.5 + 50 / 1000 * 1.5)
    )
    assert set(usage.stage_status.values()) == {"ok"}
    assert usage.prompt_version
    assert "system@1" in usage.prompt_version
    assert usage.rejected_citations >= 2  # invented unit + invented module
    assert not usage.budget_exhausted
    ai_recs = [r for r in result.recommendations if r.provenance is Provenance.AI_HYPOTHESIS]
    assert len(ai_recs) == 1
    assert ai_recs[0].generated_by == "fake-model"
    assert ai_recs[0].citations[0].symbol == "list_orders"
    static = [r for r in result.recommendations if r.provenance is Provenance.STATIC]
    assert static
    ranks = [r.priority_rank for r in result.recommendations]
    assert ranks == list(range(1, len(ranks) + 1))
    ai_insights = [i for i in result.insights if i.provenance is Provenance.AI_HYPOTHESIS]
    titles = {i.title for i in ai_insights}
    assert "Data access without batch helpers" in titles
    assert any(t.startswith("Likely hot path") for t in titles)
    correlation = next(i for i in ai_insights if i.title == "Data access without batch helpers")
    assert correlation.modules == ["app.routes"]
    for body in fake.bodies:
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] <= cfg.llm_max_tokens_per_call


def test_malformed_output_retries_then_falls_back(audited: AuditState, settings: Settings) -> None:
    def responder(step: str, user: str) -> Any:
        return "this is not json" if step == "recommendations" else EMPTY_OUTPUT[step]

    fake = FakeLLM(responder)
    result = _run(audited, llm_settings(settings), fake)
    assert fake.steps.count("recommendations") == 2 * 2  # two batches, one retry each
    assert result.usage.stage_status["recommendations"] == "fallback"
    assert result.usage.fallbacks == 2
    assert all(r.provenance is Provenance.STATIC for r in result.recommendations)
    assert result.recommendations
    retry_prompt = fake.bodies[-1]["messages"][-1]["content"]
    assert "previous reply was rejected" in retry_prompt


def test_corrective_retry_recovers(audited: AuditState, settings: Settings) -> None:
    seen: dict[str, int] = {}

    def responder(step: str, user: str) -> Any:
        seen[step] = seen.get(step, 0) + 1
        if step == "correlation" and seen[step] == 1:
            return {"insights": [{"title": "missing description"}]}
        return valid_responder(step, user)

    fake = FakeLLM(responder)
    result = _run(audited, llm_settings(settings), fake)
    assert seen["correlation"] == 2
    assert result.usage.stage_status["correlation"] == "ok"
    assert result.usage.fallbacks == 0


def test_budget_exhaustion_stops_gracefully(audited: AuditState, settings: Settings) -> None:
    fake = FakeLLM(usage=(3_000, 500))
    cfg = llm_settings(settings, llm_max_tokens_per_audit=6_000)
    result = _run(audited, cfg, fake)
    usage = result.usage
    assert usage.budget_exhausted
    assert "budget_exhausted" in usage.stage_status.values()
    assert "skipped" in usage.stage_status.values()
    assert usage.input_tokens + usage.output_tokens <= 6_000 + 3_500
    assert any("budget was exhausted" in w for w in result.warnings)
    assert result.recommendations
    assert all(r.provenance is Provenance.STATIC for r in result.recommendations)


def test_unavailable_endpoint_falls_back(audited: AuditState, settings: Settings) -> None:
    fake = FakeLLM(lambda step, user: httpx.Response(503))
    result = _run(audited, llm_settings(settings, llm_max_retries=1), fake)
    assert fake.calls == 2  # first step only: one attempt + one retry, then everything stops
    status = result.usage.stage_status
    assert status["unit_summaries"] == "unavailable"
    assert status["recommendations"] == "skipped"
    assert result.usage.fallbacks == 1
    assert any("AI analysis stopped" in w for w in result.warnings)
    assert result.recommendations


def test_unit_summaries_are_cached_by_file_hash(audited: AuditState, settings: Settings) -> None:
    cfg = llm_settings(settings)
    first = FakeLLM(valid_responder)
    _run(audited, cfg, first)
    second = FakeLLM(valid_responder)
    result = _run(audited, cfg, second)
    assert result.usage.cache_hits > 0
    assert "unit_summaries" not in second.steps
    assert first.steps.count("unit_summaries") >= 1
